"""Дрібні утиліти: час, українські числівники, безпечні імена."""
import re
import unicodedata
from datetime import datetime, timedelta, timezone


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def from_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    # Стан пишемо завжди з таймзоною, але старі/ручні правки могли її загубити
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def plural_uk(n: int, one: str, few: str, many: str) -> str:
    """1 хвилина / 2 хвилини / 5 хвилин."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not (12 <= n % 100 <= 14):
        return few
    return many


def human_left(delta: timedelta) -> str:
    """Людський залишок часу для підпису під постом."""
    seconds = int(delta.total_seconds())
    if seconds <= 0:
        return "лічені секунди"
    if seconds < 60:
        # Актуально лише для коротких тестових раундів: у бойовому режимі
        # відлік оновлюється раз на 5 хв і сюди не доходить
        return f"{seconds} {plural_uk(seconds, 'секунда', 'секунди', 'секунд')}"

    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} {plural_uk(minutes, 'хвилина', 'хвилини', 'хвилин')}"

    hours, rest = divmod(minutes, 60)
    head = f"{hours} {plural_uk(hours, 'година', 'години', 'годин')}"
    if not rest:
        return head
    return f"{head} {rest} {plural_uk(rest, 'хвилина', 'хвилини', 'хвилин')}"


def display_name(user: dict) -> str:
    """Ім'я автора для підпису в чарті: ім'я та прізвище.

    Свідомо БЕЗ @username. Підпис бачить уся аудиторія каналу, а юзернейм —
    це клікабельне посилання на профіль, тобто зовсім інший рівень публічності,
    на який учасник не підписувався. Сам юзернейм зберігається окремо в стані
    й архіві — щоб власник каналу міг знайти автора.
    """
    if not user:
        return "невідомий"
    first = (user.get("first_name") or "").strip()
    last = (user.get("last_name") or "").strip()
    full = " ".join(part for part in (first, last) if part)
    if full:
        return full
    # Ім'я в Telegram обов'язкове, тож сюди потрапляють хіба що екзотичні випадки
    username = (user.get("username") or "").strip()
    return f"@{username}" if username else "невідомий"


def username_of(user: dict) -> str | None:
    """@username автора — лише для архіву, не для публічного підпису."""
    username = ((user or {}).get("username") or "").strip()
    return f"@{username}" if username else None


_UNSAFE = re.compile(r"[^\w.-]+", re.UNICODE)


def safe_filename(value: str, fallback: str = "file") -> str:
    value = unicodedata.normalize("NFKC", str(value)).strip()
    value = _UNSAFE.sub("_", value).strip("._")
    return value[:80] or fallback
