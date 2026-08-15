"""Злив черги getUpdates: накопичення заявок і реакцій у стані раунду.

Чому накопичення, а не разовий підрахунок наприкінці: Bot API не має методу
«віддай лічильники реакцій на повідомлення». Є лише пуш-апдейти message_reaction,
кожен з яких містить ПОВНИЙ набір реакцій одного користувача на одне
повідомлення. Тому ми тримаємо мапу {повідомлення → {користувач → [емодзі]}} і
просто перезаписуємо її — обробка ідемпотентна, повторний злив тієї ж пачки
нічого не зіпсує.

Черга на боці Telegram живе 24 години, а тік ходить щохвилини, тож навіть
кількагодинний простій крону не втратить голоси.
"""
import logging

from . import state as st
from .util import display_name, username_of

log = logging.getLogger(__name__)

MAX_BATCHES = 50      # запобіжник від нескінченного циклу
MAX_ENTRIES = 2000    # стеля на розмір стану


def _author(message: dict) -> tuple[int | None, dict]:
    """Автор повідомлення. Пишучі «від імені каналу» мають sender_chat замість from."""
    sender_chat = message.get("sender_chat")
    if sender_chat:
        # Мінус, щоб id каналу не зіштовхнувся з id користувача
        return -int(sender_chat["id"]), {"first_name": sender_chat.get("title", "канал")}
    user = message.get("from") or {}
    return (user.get("id"), user)


def _actor_id(payload: dict) -> int | None:
    """Хто поставив реакцію: користувач або канал/анонімний адмін."""
    user = payload.get("user")
    if user:
        return user.get("id")
    actor_chat = payload.get("actor_chat")
    if actor_chat:
        return -int(actor_chat["id"])
    return None


def _image_file_id(message: dict) -> str | None:
    """file_id картинки, якщо повідомлення взагалі є заявкою."""
    photos = message.get("photo")
    if photos:
        # Останній розмір — найбільший
        return max(photos, key=lambda p: (p.get("file_size") or 0, p.get("width") or 0))["file_id"]

    document = message.get("document") or {}
    if str(document.get("mime_type", "")).startswith("image/"):
        return document.get("file_id")

    return None


def _handle_message(message: dict, state: dict, stats: dict) -> None:
    rnd = state.get("round")
    if not rnd or message.get("chat", {}).get("id") != state.get("discussion_chat_id"):
        return

    # Автофорвард поста каналу в групу — це корінь треду коментарів.
    if message.get("is_automatic_forward"):
        origin = message.get("forward_origin") or {}
        origin_id = origin.get("message_id") or message.get("forward_from_message_id")
        if origin_id == rnd.get("post_message_id"):
            rnd["thread_root_id"] = message["message_id"]
            log.info("Тред коментарів раунду: %s", message["message_id"])
        return

    root = rnd.get("thread_root_id")
    if root is None:
        return

    thread_id = message.get("message_thread_id")
    if thread_id is None:
        thread_id = (message.get("reply_to_message") or {}).get("message_id")
    if thread_id != root:
        return

    file_id = _image_file_id(message)
    if not file_id:
        stats["skipped"] += 1
        return

    author_id, user = _author(message)
    if author_id is None or user.get("is_bot"):
        return

    entries = rnd.setdefault("entries", {})
    key = str(message["message_id"])
    if key in entries:
        return

    voting = state["config"]["voting"]
    if voting.get("one_entry_per_user", True):
        if any(e.get("author_id") == author_id for e in entries.values()):
            stats["duplicates"] += 1
            log.info("Друга заявка від %s у цьому раунді — ігнорую", author_id)
            return

    if len(entries) >= MAX_ENTRIES:
        log.warning("Досягнуто стелю в %s заявок — нові не приймаю", MAX_ENTRIES)
        return

    entries[key] = {
        "message_id": message["message_id"],
        "author_id": author_id,
        "author": display_name(user),
        "username": username_of(user),
        "file_id": file_id,
        "date": message.get("date", 0),
        "reactions": {},
    }
    stats["entries"] += 1


def _handle_reaction(payload: dict, state: dict, stats: dict) -> None:
    rnd = state.get("round")
    if not rnd or payload.get("chat", {}).get("id") != state.get("discussion_chat_id"):
        return

    entry = (rnd.get("entries") or {}).get(str(payload.get("message_id")))
    if not entry:
        return  # реакція на коментар без картинки — не заявка

    actor = _actor_id(payload)
    if actor is None:
        return

    # new_reaction — повний поточний набір цього користувача, тому перезаписуємо.
    emojis = [r.get("emoji") for r in payload.get("new_reaction", []) if r.get("type") == "emoji"]
    if emojis:
        entry.setdefault("reactions", {})[str(actor)] = emojis
    else:
        entry.get("reactions", {}).pop(str(actor), None)
    stats["reactions"] += 1


def drain(bot, state: dict) -> dict:
    """Вичитує всю чергу апдейтів, оновлюючи state на місці."""
    stats = {"updates": 0, "entries": 0, "reactions": 0, "skipped": 0, "duplicates": 0}
    offset = st.load_offset()

    for _ in range(MAX_BATCHES):
        batch = bot.get_updates(offset)
        if not batch:
            break

        for update in batch:
            offset = update["update_id"] + 1
            stats["updates"] += 1
            try:
                if "message" in update:
                    _handle_message(update["message"], state, stats)
                elif "message_reaction" in update:
                    _handle_reaction(update["message_reaction"], state, stats)
            except Exception:
                # Один кривий апдейт не має назавжди застопорити чергу
                log.exception("Апдейт %s не оброблено", update.get("update_id"))

        st.save_offset(offset)
        if len(batch) < 100:
            break

    return stats


VARIATION_SELECTOR = chr(0xFE0F)


def _normalize_emoji(emoji: str) -> str:
    """Прибирає селектор варіації U+FE0F.

    Telegram може прислати ❤ без нього, тоді як у config.yaml майже напевно
    стоїть ❤️ із ним — без нормалізації рядки не збігаються і голос тихо
    не зараховується. Складені емодзі це не ламає: ❤️‍🔥 лишається відмінним
    від ❤ завдяки ZWJ.
    """
    return (emoji or "").replace(VARIATION_SELECTOR, "")


def score_entry(entry: dict, voting: dict) -> int:
    """Скор заявки: позитивні реакції мінус негативні."""
    positive = {_normalize_emoji(e) for e in (voting.get("positive") or [])}
    negative = {_normalize_emoji(e) for e in (voting.get("negative") or [])}
    per_user = voting.get("vote_weight", "user") == "user"
    author_id = entry.get("author_id")

    total = 0
    for actor, emojis in (entry.get("reactions") or {}).items():
        if not voting.get("allow_self_votes", True) and str(actor) == str(author_id):
            continue
        normalized = [_normalize_emoji(e) for e in emojis]
        plus = sum(1 for e in normalized if e in positive)
        minus = sum(1 for e in normalized if e in negative)
        if per_user:
            # Одна людина — один голос, скільки б емодзі з пулу вона не поставила
            total += (1 if plus else 0) - (1 if minus else 0)
        else:
            total += plus - minus
    return total


def pick_winner(rnd: dict, voting: dict, *, exclude_author_id=None) -> tuple[dict | None, list[dict]]:
    """Повертає (переможець, усі заявки зі скорами), відсортовані від кращої.

    exclude_author_id — автор, якому цього разу перемагати не можна
    (коли voting.allow_repeat_winner вимкнено).
    """
    entries = list((rnd.get("entries") or {}).values())
    if not entries:
        return None, []

    for entry in entries:
        entry["score"] = score_entry(entry, voting)

    latest_first = voting.get("tie_breaker") == "latest"
    ranked = sorted(
        entries,
        key=lambda e: (-e["score"], -e["date"] if latest_first else e["date"], e["message_id"]),
    )

    minimum = int(voting.get("min_votes", 0) or 0)
    for entry in ranked:
        if exclude_author_id is not None and entry["author_id"] == exclude_author_id:
            continue
        if entry["score"] < minimum:
            break  # далі скори лише менші
        return entry, ranked
    return None, ranked
