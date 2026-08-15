"""Офлайн-прогін повного циклу вікторини без жодного мережевого виклику.

Підміняє Bot заглушкою, накидає синтетичні заявки з реакціями і проганяє всі
раунди до фіналу. Перевіряє те, що на живому каналі ловилося б лише через
дев'ять годин: переходи станів, підрахунок скорів, накопичення картинок у чарті,
захист від подвійного зарахування переможця й гасіння стану наприкінці.

    python simulate.py
"""
import json
import shutil
import sys
from datetime import timedelta
from pathlib import Path

from votebot import config as cfg_mod

# Пісочниця: справжній state.json і media/ не чіпаємо
SANDBOX = Path(__file__).resolve().parent / "sandbox"
cfg_mod.STATE_PATH = SANDBOX / "state.json"
cfg_mod.RUNTIME_DIR = SANDBOX / "runtime"
cfg_mod.MEDIA_DIR = SANDBOX / "media"
cfg_mod.OFFSET_PATH = cfg_mod.RUNTIME_DIR / "offset.json"
cfg_mod.LOCK_PATH = cfg_mod.RUNTIME_DIR / "tick.lock"

from votebot import state as st  # noqa: E402  (після підміни шляхів)
from votebot.rounds import (  # noqa: E402
    build_final_caption, close_round, next_caption_time, open_round,
    update_countdown, validate_templates,
)
from votebot.updates import score_entry  # noqa: E402
from votebot.util import display_name, from_iso, now_utc, to_iso, username_of  # noqa: E402
from preview import make_placeholder  # noqa: E402

VOTERS = [901, 902, 903, 904, 905, 906, 907]


class FakeBot:
    """Заглушка Bot: рахує виклики, нікуди не ходить."""

    def __init__(self):
        self.message_id = 5000
        self.sent = []
        self.deleted = []
        self.captions = []
        self.fail_next_send = False

    def send_media(self, chat_id, path, kind, caption=None):
        if self.fail_next_send:
            self.fail_next_send = False
            raise RuntimeError("імітація збою публікації")
        self.message_id += 1
        self.sent.append({"message_id": self.message_id, "path": Path(path), "kind": kind, "caption": caption})
        return {"message_id": self.message_id}

    def edit_message_caption(self, chat_id, message_id, caption):
        self.captions.append((message_id, caption))
        return {"message_id": message_id}

    def delete_message(self, chat_id, message_id):
        self.deleted.append(message_id)
        return True

    def download_file(self, file_id, dest):
        """Замість завантаження — копія заглушки, щоб чарт справді малювався."""
        source = make_placeholder(int(file_id.split("-")[-1]))
        dest = Path(dest).with_suffix(".jpg")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
        return dest


def make_entries(round_index: int, count: int = 4) -> dict:
    """count заявок; чим більший індекс — тим більше плюсів, останній ловить мінуси."""
    entries = {}
    for i in range(count):
        message_id = 7000 + round_index * 100 + i
        reactions = {}
        for voter in VOTERS[: i + 1]:
            reactions[str(voter)] = ["👍"]
        if i == count - 1:
            # Найпопулярнішій заявці ще й два дизлайки — перевіряємо різницю
            for voter in VOTERS[-2:]:
                reactions[str(voter)] = ["👎"]
        # Юзернейм навмисно «секретний»: перевіряємо, що він не витікає
        # у публічні підписи, а лишається тільки в стані й архіві
        user = {
            "first_name": f"Учасник{round_index + 1}",
            "last_name": f"Тестовий{i + 1}",
            "username": f"secret_handle_{round_index}_{i}",
        }
        entries[str(message_id)] = {
            "message_id": message_id,
            "author_id": 100 + round_index * 10 + i,
            "author": display_name(user),
            "username": username_of(user),
            "file_id": f"fake-{round_index * 4 + i}",
            "date": 1_700_000_000 + message_id,
            "reactions": reactions,
        }
    return entries


def check(label: str, condition: bool, detail: str = "") -> bool:
    print(f"  {'OK  ' if condition else 'ЗБІЙ'}  {label}{(' — ' + detail) if detail else ''}")
    return condition


def main() -> int:
    if SANDBOX.exists():
        shutil.rmtree(SANDBOX)
    SANDBOX.mkdir(parents=True)

    cfg = cfg_mod.load_config()
    cfg["quiz"]["round_minutes"] = 60
    validate_templates(cfg)

    quiz = cfg["quiz"]
    rows, cols = len(quiz["y_axis"]["values"]), len(quiz["x_axis"]["values"])
    order = cfg_mod.resolve_cell_order(quiz["cell_order"], rows, cols)

    state = {
        "active": True,
        "version": st.STATE_VERSION,
        "quiz_id": "simulate",
        "created_at": to_iso(now_utc()),
        "finished_at": None,
        "config": cfg,
        "channel_id": -100123,
        "discussion_chat_id": -100456,
        "rows": rows,
        "cols": cols,
        "cell_order": order,
        "round_index": 0,
        "round": None,
        "winners": [],
    }

    bot = FakeBot()
    failures = 0

    print(f"\nПрогін {len(order)} раундів\n")
    open_round(bot, state, 0)
    first_post = state["round"]["post_message_id"]

    # --- Відлік ---
    print("Відлік у підписі:")
    rnd = state["round"]
    rnd["next_caption_at"] = to_iso(now_utc() - timedelta(seconds=1))
    failures += not check("editMessageCaption викликано", update_countdown(bot, state))
    failures += not check(
        "наступний слот у майбутньому", from_iso(state["round"]["next_caption_at"]) > now_utc()
    )
    failures += not check("у підписі є залишок часу", "Залишилось" in bot.captions[-1][1])

    # --- Раунди ---
    print("\nРаунди:")
    for index in range(len(order)):
        rnd = state["round"]
        rnd["entries"] = make_entries(index)
        rnd["ends_at"] = to_iso(now_utc() - timedelta(seconds=1))

        # На третьому раунді ламаємо публікацію наступного поста: наступний тік
        # мусить не задублювати переможця, а просто відкрити раунд заново.
        if index == 2:
            before = len(state["winners"])
            bot.fail_next_send = True
            try:
                close_round(bot, state)
            except RuntimeError:
                pass
            failures += not check(
                "збій публікації: переможця зараховано рівно раз",
                len(state["winners"]) == before + 1,
                f"було {before}, стало {len(state['winners'])}",
            )
            failures += not check("раунд позначено закритим", state["round"].get("closed") is True)
            close_round(bot, state)  # повтор, як зробив би наступний тік
            failures += not check(
                "повторний виклик не задублював переможця",
                len(state["winners"]) == before + 1,
                f"переможців: {len(state['winners'])}",
            )
            continue

        close_round(bot, state)

    # --- Підсумки ---
    print("\nПідсумки:")
    final = st.load()
    failures += not check("стан погашено", not st.is_active(final))
    failures += not check(
        "переможців рівно за кількістю клітинок",
        len(state["winners"]) == len(order),
        f"{len(state['winners'])}/{len(order)}",
    )

    archive_path = cfg_mod.RUNTIME_DIR / "archive" / "simulate.json"
    failures += not check("архів записано", archive_path.exists())
    if archive_path.exists():
        archived = json.loads(archive_path.read_text(encoding="utf-8"))
        failures += not check(
            "в архіві збережені імена авторів",
            all(w.get("author") for w in archived["winners"]),
        )

    # Скор: 4 заявки, остання має 4 плюси і 2 мінуси → перемагає третя з 3 плюсами
    sample = make_entries(0)
    scores = {e["author"]: score_entry(e, cfg["voting"]) for e in sample.values()}
    failures += not check(
        "різниця позитив−негатив рахується правильно",
        sorted(scores.values()) == [1, 2, 2, 3],
        str(sorted(scores.values())),
    )
    failures += not check(
        "переможець першого раунду — не той, кого залайкали найбільше",
        state["winners"][0]["score"] == 3,
        f"скор {state['winners'][0]['score']}",
    )

    posts = len(bot.sent)
    failures += not check("постів = раундів + фінальний", posts == len(order) + 1, str(posts))
    failures += not check(
        "видалено всі пости, крім останнього",
        len(bot.deleted) == len(order) and first_post in bot.deleted,
        f"видалено {len(bot.deleted)}",
    )

    final_media = bot.sent[-1]["path"]
    failures += not check("фінальний чарт відрендерено", final_media.exists(),
                          f"{final_media.name}, {final_media.stat().st_size / 1024:.0f} КБ")

    # --- Приватність у публічних текстах ---
    print("\nПриватність:")
    final_caption = build_final_caption(state)
    failures += not check(
        "у списку переможців імена, а не юзернейми",
        "secret_handle" not in final_caption and "@" not in final_caption,
    )
    failures += not check(
        "кожен переможець названий на ім'я",
        all(w["author"] in final_caption for w in state["winners"]),
    )
    failures += not check(
        "юзернейм при цьому збережено в архіві",
        bool(archive_path.exists() and all(w.get("username") for w in archived["winners"])),
    )
    failures += not check(
        "підпис під клітинкою теж без юзернейма",
        all("@" not in w["author"] for w in state["winners"]),
    )

    # --- Емодзі ---
    print("\nПідрахунок реакцій:")
    pools = {
        "positive": ["👍", "❤️", "😍"], "negative": ["👎", "💩"],
        "vote_weight": "reaction", "allow_self_votes": True,
    }
    # Telegram може прислати серце і з селектором варіації, і без нього
    variation = {"author_id": 1, "reactions": {"5": ["❤"], "6": ["❤️"]}}
    failures += not check(
        "❤ і ❤️ рахуються однаково",
        score_entry(variation, pools) == 2,
        f"скор {score_entry(variation, pools)}, очікували 2",
    )

    mixed = {"author_id": 1, "reactions": {"5": ["😍", "👍"], "6": ["💩"], "7": ["🤔"]}}
    failures += not check(
        "різниця по пулах, стороннє емодзі не рахується",
        score_entry(mixed, pools) == 1,
        f"скор {score_entry(mixed, pools)}, очікували 1",
    )
    failures += not check(
        "vote_weight=user обмежує людину одним голосом",
        score_entry(mixed, {**pools, "vote_weight": "user"}) == 0,
        f"скор {score_entry(mixed, {**pools, 'vote_weight': 'user'})}, очікували 0",
    )

    # --- Розклад оновлення відліку ---
    print("\nРозклад відліку (раунд 60 хв, крок 5 хв, фінальне за 60 с):")
    quiz = {"caption_update_minutes": 5, "caption_update_seconds": None, "final_notice_seconds": 60}
    start = now_utc()
    probe = {"started_at": to_iso(start), "ends_at": to_iso(start + timedelta(minutes=60))}
    # Відлік ведемо від обрізаного до секунд started_at — саме з ним працює код
    base = from_iso(probe["started_at"])
    slots, cursor = [], base
    for _ in range(30):
        cursor = next_caption_time(quiz, probe, cursor)
        if cursor >= from_iso(probe["ends_at"]):
            break
        slots.append(round((cursor - base).total_seconds() / 60))
    failures += not check(
        "слоти йдуть по сітці й закінчуються фінальним",
        slots == [5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 59],
        f"хв: {slots}",
    )

    print(f"\n{'Усе зелене' if not failures else f'ЗБОЇВ: {failures}'}\n")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
