"""Життєвий цикл вікторини: старт, відлік, підбиття підсумків раунду, фінал.

Один раунд — один пост у каналі. Наприкінці раунду публікується наступний, а
попередній видаляється: разом із ним Telegram прибирає і його гілку коментарів,
тож окремо чистити реплаї не доводиться.
"""
import html
import json
import logging
from datetime import timedelta

from . import config as cfg_mod
from . import state as st
from .api import TelegramError
from .render import build_round_media
from .updates import drain, pick_winner
from .util import from_iso, human_entries, human_left, now_utc, to_iso

log = logging.getLogger(__name__)


# --- Довідкові дрібниці -----------------------------------------------------

def cell_name(state: dict, cell) -> str:
    """Людська назва клітинки: «красиве + миттєва смерть».

    Підписи осей можуть містити перенос рядка для чарта — у тексті поста він
    перетворюється на пробіл.
    """
    row, col = cell
    quiz = state["config"]["quiz"]
    x = " ".join(quiz["x_axis"]["values"][col].split())
    y = " ".join(quiz["y_axis"]["values"][row].split())
    return f"{x} + {y}"


def filled_map(state: dict) -> dict:
    """Заповнені клітинки у форматі, який очікує рендер."""
    return {
        tuple(w["cell"]): {"image": w["image"], "author": w["author"]}
        for w in state.get("winners", [])
        if w.get("image")
    }


def build_caption(state: dict, left: timedelta, rnd: dict | None = None) -> str:
    rnd = rnd or state["round"]
    quiz = state["config"]["quiz"]
    row, col = rnd["cell"]
    return state["config"]["templates"]["round_caption"].format(
        title=quiz.get("title", ""),
        hashtag=quiz.get("hashtag", ""),
        round=rnd["index"] + 1,
        total=len(state["cell_order"]),
        cell=cell_name(state, rnd["cell"]),
        x_value=quiz["x_axis"]["values"][col],
        y_value=quiz["y_axis"]["values"][row],
        left=human_left(left),
        left_min=max(0, int(left.total_seconds() // 60)),
        # Лічильник оновлюється разом із відліком, тобто відстає від
        # реальності не більше ніж на caption_update_*
        entries=len(rnd.get("entries") or {}),
        entries_human=human_entries(len(rnd.get("entries") or {})),
    ).strip()


def build_final_caption(state: dict) -> str:
    templates = state["config"]["templates"]
    quiz = state["config"]["quiz"]

    lines = []
    for winner in state.get("winners", []):
        lines.append(
            templates["winner_line"].format(
                round=winner["round"],
                cell=cell_name(state, winner["cell"]),
                author=html.escape(winner["author"]),
                score=winner.get("score", 0),
            )
        )

    return templates["finished_caption"].format(
        title=quiz.get("title", ""),
        hashtag=quiz.get("hashtag", ""),
        total=len(state["cell_order"]),
        winners="\n".join(lines) if lines else "Жодної клітинки не заповнено.",
    ).strip()


def next_caption_time(quiz: dict, rnd: dict, after):
    """Найближчий момент оновлення підпису після `after`.

    Регулярна сітка рахується від старту раунду, а не накопиченням від
    попереднього значення — інакше час виконання кожного тіка потроху зсував би
    відлік. Окремо додається фінальне попередження за N секунд до кінця: воно
    поза сіткою, тож остання правка підпису завжди припадає на фініш.
    """
    started = from_iso(rnd["started_at"])
    step = cfg_mod.caption_seconds(quiz)
    elapsed = max(0.0, (after - started).total_seconds())
    candidates = [started + timedelta(seconds=step * (int(elapsed // step) + 1))]

    notice = cfg_mod.final_notice_seconds(quiz)
    if notice:
        final_at = from_iso(rnd["ends_at"]) - timedelta(seconds=notice)
        if final_at > after:
            candidates.append(final_at)

    return min(candidates)


def validate_templates(cfg: dict) -> None:
    """Перевіряє шаблони до старту: KeyError посеред вікторини нам не потрібен."""
    probe = {
        "title": "x", "hashtag": "#x", "round": 1, "total": 9, "cell": "x + y",
        "x_value": "x", "y_value": "y", "left": "1 хвилина", "left_min": 1,
        "winners": "x", "author": "x", "score": 0,
        "entries": 0, "entries_human": "поки жодної заявки",
    }
    for name, template in cfg["templates"].items():
        try:
            template.format(**probe)
        except KeyError as exc:
            raise SystemExit(f"templates.{name}: невідоме поле {exc}. Доступні: {', '.join(sorted(probe))}")
        except (IndexError, ValueError) as exc:
            raise SystemExit(f"templates.{name}: помилка у фігурних дужках ({exc})")


# --- Раунди -----------------------------------------------------------------

def open_round(bot, state: dict, index: int) -> None:
    """Рендерить чарт, публікує пост раунду і прибирає попередній."""
    cfg = state["config"]
    cell = state["cell_order"][index]
    previous_post = (state.get("round") or {}).get("post_message_id")

    stem = cfg_mod.MEDIA_DIR / f"{state['quiz_id']}_round{index + 1:02d}"
    path, kind = build_round_media(cfg, filled_map(state), tuple(cell), stem)
    log.info("Раунд %s: %s (%s, %.0f КБ)", index + 1, cell_name(state, cell), kind, path.stat().st_size / 1024)

    started = now_utc()
    ends = started + timedelta(seconds=cfg_mod.round_seconds(cfg["quiz"]))
    new_round = {
        "index": index,
        "cell": cell,
        "started_at": to_iso(started),
        "ends_at": to_iso(ends),
        "next_caption_at": None,  # заповнюється нижче, коли відомі started_at/ends_at
        "post_message_id": None,
        "thread_root_id": None,
        "media": str(path),
        "media_kind": kind,
        "last_caption": None,
        "entries": {},
    }
    new_round["next_caption_at"] = to_iso(next_caption_time(cfg["quiz"], new_round, started))
    caption = build_caption(state, ends - started, rnd=new_round)

    # Публікуємо ДО того, як чіпати стан. Якщо відправка впаде, у стані лишиться
    # попередній (уже закритий) раунд — і наступний тік просто повторить спробу,
    # замість того щоб закрити недоношений раунд із порожнім списком заявок і
    # мовчки пропустити клітинку.
    message = bot.send_media(state["channel_id"], path, kind, caption)

    new_round["post_message_id"] = message["message_id"]
    new_round["last_caption"] = caption
    state["round"] = new_round
    state["round_index"] = index
    st.save(state)

    # Попередній пост зносимо ЛИШЕ після успішної публікації нового — інакше
    # збій публікації лишив би канал зовсім без поста вікторини.
    if previous_post and cfg["telegram"].get("delete_previous_post", True):
        bot.delete_message(state["channel_id"], previous_post)


def update_countdown(bot, state: dict) -> bool:
    """Оновлює «Залишилось …», якщо настав час. Повертає True, якщо редагували."""
    rnd = state["round"]
    now = now_utc()
    if now < from_iso(rnd["next_caption_at"]):
        return False

    ends = from_iso(rnd["ends_at"])
    caption = build_caption(state, ends - now)
    edited = False

    if caption != rnd.get("last_caption"):
        try:
            bot.edit_message_caption(state["channel_id"], rnd["post_message_id"], caption)
            rnd["last_caption"] = caption
            edited = True
        except TelegramError as exc:
            # «message is not modified» — нормальна ситуація, решта варта уваги
            if "not modified" not in str(exc):
                log.warning("Не вдалося оновити відлік: %s", exc)

    rnd["next_caption_at"] = to_iso(next_caption_time(state["config"]["quiz"], rnd, now))
    return edited


def close_round(bot, state: dict) -> None:
    """Визначає переможця, зберігає картинку і запускає наступний раунд або фінал."""
    rnd = state["round"]

    # Захист від подвійного зарахування: якщо публікація наступного раунду впала
    # вже ПІСЛЯ підбиття підсумків, наступний тік зайде сюди ще раз — і без цього
    # прапорця дописав би того самого переможця вдруге.
    if rnd.get("closed"):
        log.info("Раунд %s уже підбито — одразу відкриваю наступний", rnd["index"] + 1)
        _advance(bot, state)
        return

    cfg = state["config"]
    voting = cfg["voting"]

    exclude = None
    if not voting.get("allow_repeat_winner", True) and state.get("winners"):
        exclude = state["winners"][-1]["author_id"]

    winner, ranked = pick_winner(rnd, voting, exclude_author_id=exclude)
    log.info("Раунд %s закрито: заявок %s, переможець %s",
             rnd["index"] + 1, len(ranked), winner["author"] if winner else "—")

    if winner:
        stem = cfg_mod.MEDIA_DIR / f"{state['quiz_id']}_win{rnd['index'] + 1:02d}"
        try:
            image_path = bot.download_file(winner["file_id"], stem)
        except Exception:
            log.exception("Не вдалося завантажити картинку переможця — клітинка лишиться порожньою")
            image_path = None

        if image_path:
            state.setdefault("winners", []).append({
                "round": rnd["index"] + 1,
                "cell": rnd["cell"],
                "author": winner["author"],
                "username": winner.get("username"),
                "author_id": winner["author_id"],
                "score": winner["score"],
                "message_id": winner["message_id"],
                "image": str(image_path),
                "entries_total": len(ranked),
            })
    else:
        log.info("Заявок немає або не набрано мінімум — клітинка лишається порожньою")

    _record_round(state, rnd, ranked, winner)
    rnd["closed"] = True
    st.save(state)
    _advance(bot, state)


def _record_round(state: dict, rnd: dict, ranked: list, winner: dict | None) -> None:
    """Складає підсумок раунду в state["rounds"].

    Без цього дані про заявки зникають назавжди: відкриття наступного раунду
    затирає rnd["entries"], і в архіві лишаються самі переможці. Тоді питання
    на кшталт «чи програють пізні заявки» доводиться відновлювати по логах
    systemd, які не вічні.

    Особи тих, хто голосував, свідомо НЕ зберігаємо — лише кількість і розклад
    по емодзі. Для аналізу формату цього досить, а досьє на читачів нам не треба.
    """
    started_ts = from_iso(rnd["started_at"]).timestamp()
    voters: set[str] = set()
    entries = []

    for rank, entry in enumerate(ranked, 1):
        reactions = entry.get("reactions") or {}
        voters.update(reactions)
        emoji_counts: dict[str, int] = {}
        for emojis in reactions.values():
            for emoji in emojis:
                emoji_counts[emoji] = emoji_counts.get(emoji, 0) + 1

        submitted = entry.get("date", 0)
        entries.append({
            "rank": rank,
            "message_id": entry["message_id"],
            "author": entry["author"],
            "author_id": entry["author_id"],
            "username": entry.get("username"),
            "submitted_at": submitted,
            "seconds_after_start": max(0, int(submitted - started_ts)) if submitted else None,
            "score": entry.get("score", 0),
            "voters": len(reactions),
            "emoji": emoji_counts,
        })

    state.setdefault("rounds", []).append({
        "round": rnd["index"] + 1,
        "cell": rnd["cell"],
        "started_at": rnd["started_at"],
        "ends_at": rnd["ends_at"],
        "closed_at": to_iso(now_utc()),
        "post_message_id": rnd.get("post_message_id"),
        "entries_total": len(ranked),
        "unique_voters": len(voters),
        "winner_message_id": winner["message_id"] if winner else None,
        "entries": entries,
    })


def _advance(bot, state: dict) -> None:
    """Наступний раунд або фінал."""
    next_index = state["round"]["index"] + 1
    if next_index < len(state["cell_order"]):
        open_round(bot, state, next_index)
    else:
        finish_quiz(bot, state)


def finish_quiz(bot, state: dict) -> None:
    """Публікує заповнений чарт, архівує результати і гасить стан."""
    cfg = state["config"]
    last_post = (state.get("round") or {}).get("post_message_id")

    stem = cfg_mod.MEDIA_DIR / f"{state['quiz_id']}_final"
    path, kind = build_round_media(cfg, filled_map(state), None, stem)
    caption = build_final_caption(state)

    message = bot.send_media(state["channel_id"], path, kind, caption)
    state["final_post_message_id"] = message["message_id"]

    if last_post and cfg["telegram"].get("delete_previous_post", True):
        bot.delete_message(state["channel_id"], last_post)

    state["active"] = False
    state["finished_at"] = to_iso(now_utc())
    state["round"] = None

    archive = cfg_mod.RUNTIME_DIR / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    (archive / f"{state['quiz_id']}.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    st.clear()
    log.info("Вікторину завершено. Переможців: %s. Архів: %s",
             len(state.get("winners", [])), archive / f"{state['quiz_id']}.json")


# --- Точка входу крону ------------------------------------------------------

def tick(bot) -> None:
    """Один прохід крону. Ідемпотентний: безпечно викликати як завгодно часто."""
    state = st.load()

    if not st.is_active(state):
        # Чергу зливаємо навіть без активної вікторини: інакше вона протухне за
        # 24 години і перший тік наступного забігу захлинеться старими апдейтами.
        drain(bot, state)
        log.debug("Активної вікторини немає")
        return

    stats = drain(bot, state)
    if stats["updates"]:
        log.info("Апдейтів: %(updates)s, заявок: +%(entries)s, реакцій: %(reactions)s, "
                 "без картинки: %(skipped)s, повторних: %(duplicates)s", stats)

    rnd = state["round"]
    if now_utc() >= from_iso(rnd["ends_at"]):
        close_round(bot, state)
    else:
        update_countdown(bot, state)

    # Після фіналу стан уже погашено через st.clear() — не воскрешаємо його
    if st.is_active(state):
        st.save(state)
