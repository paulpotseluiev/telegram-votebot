"""Конфіг вікторини: параметри з config.yaml, секрети з .env / оточення."""
import copy
import logging
import os
import random
from pathlib import Path

import yaml

BASE_DIR = Path(__file__).resolve().parent.parent

try:
    from dotenv import load_dotenv
except ImportError:  # python-dotenv не обов'язковий — читаємо .env самі
    load_dotenv = None


def _load_env_file(path: Path) -> None:
    """Мінімальний парсер .env, щоб не тягнути залежність на голу машину."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        # setdefault: справжнє оточення має пріоритет над файлом
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


if load_dotenv:
    load_dotenv(BASE_DIR / ".env")
else:
    _load_env_file(BASE_DIR / ".env")

# --- Секрети й шляхи ---
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

CONFIG_PATH = Path(os.environ.get("VOTEBOT_CONFIG") or BASE_DIR / "config.yaml")
STATE_PATH = Path(os.environ.get("VOTEBOT_STATE") or BASE_DIR / "state.json")
RUNTIME_DIR = Path(os.environ.get("VOTEBOT_RUNTIME") or BASE_DIR / "runtime")
MEDIA_DIR = Path(os.environ.get("VOTEBOT_MEDIA") or BASE_DIR / "media")

# Офсет getUpdates живе окремо від state.json: він має переживати
# завершення вікторини, інакше після ресету бот перечитає стару чергу.
OFFSET_PATH = RUNTIME_DIR / "offset.json"
LOCK_PATH = RUNTIME_DIR / "tick.lock"

DEFAULTS = {
    "quiz": {
        "title": "",
        "hashtag": "",
        "x_axis": {"title": "", "values": []},
        "y_axis": {"title": "", "values": []},
        "round_minutes": 60,
        "caption_update_minutes": 5,
        # Секундні варіанти мають пріоритет над хвилинними. Потрібні для
        # тестових забігів, де раунд коротший за хвилину.
        "round_seconds": None,
        "caption_update_seconds": None,
        # Окреме оновлення підпису перед самим кінцем раунду, поза регулярною
        # сіткою. 0 = вимкнено.
        "final_notice_seconds": 60,
        "cell_order": "row_major",
    },
    "voting": {
        "positive": ["👍"],
        "negative": ["👎"],
        "one_entry_per_user": True,
        "allow_self_votes": True,
        "allow_repeat_winner": True,
        "min_votes": 0,
        "tie_breaker": "earliest",
        "vote_weight": "user",
    },
    "telegram": {
        "channel_id": 0,
        "delete_previous_post": True,
    },
    "render": {
        "width": 1200,
        "format": "auto",
        "fps": 12,
        "loop_seconds": 2.2,
        "gif_max_width": 800,
        "snake_length": 0.28,
        "square_tolerance": 0.15,
        "letterbox": "blur",
        "font": None,
        "font_bold": None,
        "theme": {
            "background": "#ffffff",
            "grid": "#111111",
            "text": "#111111",
            "muted": "#8a8a8a",
            "empty_cell": "#f4f4f4",
            "highlight": "#ff5c00",
            "caption_bg": "#000000",
            "caption_text": "#ffffff",
        },
    },
    "templates": {
        "round_caption": "{title} — раунд {round}/{total}\n\nКлітинка: {cell}\n\n⏳ Залишилось {left}\n\n{hashtag}",
        "finished_caption": "{title} — чарт заповнено!\n\n{winners}\n\n{hashtag}",
        "winner_line": "{cell} — {author}",
        "cell_credit": "{author}",
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Рекурсивно накладає override на копію base. Списки замінюються цілком."""
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: Path | str | None = None) -> dict:
    """Читає config.yaml і накладає його поверх DEFAULTS."""
    path = Path(path or CONFIG_PATH)
    if not path.exists():
        example = path.parent / "config.example.yaml"
        hint = f"\nСкопіюй шаблон:  cp {example.name} {path.name}" if example.exists() else ""
        raise SystemExit(f"Не знайдено конфіг: {path}{hint}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return _deep_merge(DEFAULTS, raw)


def round_seconds(quiz: dict) -> float:
    """Тривалість раунду в секундах."""
    if quiz.get("round_seconds"):
        return float(quiz["round_seconds"])
    return float(quiz["round_minutes"]) * 60


def caption_seconds(quiz: dict) -> float:
    """Інтервал оновлення відліку в секундах."""
    if quiz.get("caption_update_seconds"):
        return float(quiz["caption_update_seconds"])
    return float(quiz["caption_update_minutes"]) * 60


def final_notice_seconds(quiz: dict) -> float:
    """За скільки секунд до кінця раунду зробити останнє оновлення підпису."""
    return float(quiz.get("final_notice_seconds") or 0)


def human_duration(seconds: float) -> str:
    """Коротке представлення тривалості для довідкового виводу."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} с"
    minutes, rest = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} хв {rest} с" if rest else f"{minutes} хв"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} год {minutes} хв" if minutes else f"{hours} год"


def validate(cfg: dict, *, require_token: bool = True) -> None:
    """Падає з людською помилкою, якщо конфіг непридатний до запуску."""
    problems = []

    if require_token and not BOT_TOKEN:
        problems.append("не задано BOT_TOKEN (.env або змінна оточення)")

    quiz = cfg["quiz"]
    x_values = quiz["x_axis"]["values"]
    y_values = quiz["y_axis"]["values"]
    if len(x_values) < 2 or len(y_values) < 2:
        problems.append("quiz.x_axis.values і quiz.y_axis.values мають містити щонайменше 2 значення")

    try:
        round_s, caption_s = round_seconds(quiz), caption_seconds(quiz)
        if round_s <= 0:
            problems.append("тривалість раунду має бути > 0")
        if caption_s <= 0:
            problems.append("інтервал оновлення відліку має бути > 0")
        if 0 < round_s <= caption_s:
            logging.getLogger(__name__).warning(
                "Інтервал оновлення відліку (%s) не менший за сам раунд (%s) — "
                "регулярний відлік жодного разу не оновиться.",
                human_duration(caption_s), human_duration(round_s),
            )
        notice_s = final_notice_seconds(quiz)
        if notice_s < 0:
            problems.append("quiz.final_notice_seconds не може бути відʼємним")
        elif notice_s >= round_s > 0:
            logging.getLogger(__name__).warning(
                "final_notice_seconds (%s) не менший за раунд (%s) — "
                "фінальне попередження не спрацює.",
                human_duration(notice_s), human_duration(round_s),
            )
    except (TypeError, ValueError):
        problems.append("тривалість раунду та інтервал відліку мають бути числами")

    channel_id = cfg["telegram"]["channel_id"]
    if not isinstance(channel_id, int) or channel_id == 0:
        problems.append("telegram.channel_id має бути числом (для каналів — з префіксом -100)")

    voting = cfg["voting"]
    if not voting["positive"] and not voting["negative"]:
        problems.append("voting.positive і voting.negative не можуть бути порожні одночасно")
    overlap = set(voting["positive"]) & set(voting["negative"])
    if overlap:
        problems.append(f"емодзі одночасно в позитивному і негативному пулі: {' '.join(sorted(overlap))}")
    if voting["tie_breaker"] not in ("earliest", "latest"):
        problems.append("voting.tie_breaker має бути earliest або latest")
    if voting["vote_weight"] not in ("user", "reaction"):
        problems.append("voting.vote_weight має бути user або reaction")

    if cfg["render"]["format"] not in ("auto", "mp4", "gif", "png"):
        problems.append("render.format має бути auto | mp4 | gif | png")

    if problems:
        raise SystemExit("Помилки конфігурації:\n  - " + "\n  - ".join(problems))


def resolve_cell_order(spec, rows: int, cols: int) -> list[list[int]]:
    """Перетворює quiz.cell_order на конкретний список пар [рядок, колонка].

    Результат зберігається у state.json, тож 'random' розігрується рівно раз.
    """
    if isinstance(spec, str):
        name = spec.strip().lower()
        if name == "row_major":
            order = [[r, c] for r in range(rows) for c in range(cols)]
        elif name == "column_major":
            order = [[r, c] for c in range(cols) for r in range(rows)]
        elif name == "diagonal":
            order = sorted(
                ([r, c] for r in range(rows) for c in range(cols)),
                key=lambda rc: (rc[0] + rc[1], rc[0]),
            )
        elif name == "random":
            order = [[r, c] for r in range(rows) for c in range(cols)]
            random.shuffle(order)
        else:
            raise SystemExit(
                f"Невідомий quiz.cell_order: {spec!r}. "
                "Очікується row_major | column_major | diagonal | random або явний список пар."
            )
        return order

    # Явний список пар — перевіряємо, що це саме перестановка всіх клітинок
    try:
        order = [[int(r), int(c)] for r, c in spec]
    except (TypeError, ValueError):
        raise SystemExit("quiz.cell_order: явний список має складатися з пар [рядок, колонка]")

    expected = {(r, c) for r in range(rows) for c in range(cols)}
    got = {(r, c) for r, c in order}
    if got != expected or len(order) != len(expected):
        missing = sorted(expected - got)
        extra = sorted(got - expected)
        raise SystemExit(
            "quiz.cell_order має містити кожну клітинку рівно один раз. "
            f"Бракує: {missing or '—'}; зайві/дублі: {extra or '—'}"
        )
    return order


def setup_logging(level: str | None = None) -> None:
    logging.basicConfig(
        level=getattr(logging, (level or LOG_LEVEL), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def ensure_dirs() -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
