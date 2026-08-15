"""Стан активної вікторини: атомарне читання/запис + блокування тіка.

state.json — єдине джерело правди для крону. Порожній або active=false файл
означає «роботи немає»: тік одразу виходить, нічого не питаючи в Telegram
(крім зливу черги апдейтів, щоб вона не протухала).
"""
import json
import logging
import os
import time
from pathlib import Path

from . import config as cfg_mod

log = logging.getLogger(__name__)

STATE_VERSION = 1


def _write_atomic(path: Path, payload: dict) -> None:
    """Пише через тимчасовий файл + os.replace: обірваний тік не лишить недописаний стан."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def load(path: Path | None = None) -> dict:
    path = Path(path or cfg_mod.STATE_PATH)
    if not path.exists():
        return {"active": False}
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {"active": False}
    except json.JSONDecodeError as exc:
        log.error("state.json пошкоджено (%s) — вважаю, що активної вікторини немає", exc)
        return {"active": False}


def save(state: dict, path: Path | None = None) -> None:
    _write_atomic(Path(path or cfg_mod.STATE_PATH), state)


def is_active(state: dict) -> bool:
    return bool(state.get("active"))


def clear(path: Path | None = None) -> None:
    """Гасить вікторину. Крон після цього не робить нічого до наступного init."""
    save({"active": False, "version": STATE_VERSION}, path)


# --- Офсет getUpdates -------------------------------------------------------
# Живе окремо від state.json: має переживати завершення вікторини, інакше після
# ресету бот перечитає стару чергу і вважатиме давні коментарі новими заявками.

def load_offset() -> int:
    path = cfg_mod.OFFSET_PATH
    if not path.exists():
        return 0
    try:
        return int(json.loads(path.read_text(encoding="utf-8")).get("offset", 0))
    except (json.JSONDecodeError, ValueError, AttributeError):
        log.warning("offset.json пошкоджено — починаю з нуля")
        return 0


def save_offset(offset: int) -> None:
    _write_atomic(cfg_mod.OFFSET_PATH, {"offset": int(offset)})


# --- Блокування -------------------------------------------------------------

class TickLock:
    """Не даємо двом тікам працювати одночасно.

    Крон раз на хвилину може наздогнати попередній запуск, який довго вантажив
    відео. Замок — файл із PID; протухлий (старший за stale_after) забираємо,
    інакше впалий процес заблокував би вікторину назавжди.
    """

    def __init__(self, path: Path | None = None, stale_after: int = 600, wait_seconds: float = 0):
        self.path = Path(path or cfg_mod.LOCK_PATH)
        self.stale_after = stale_after
        self.wait_seconds = wait_seconds
        self.acquired = False

    def _try_acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            age = time.time() - self.path.stat().st_mtime
            if age < self.stale_after:
                self.acquired = False
                return
            log.warning("Забираю протухлий замок (вік %.0f с)", age)
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:  # інший тік випередив нас
                self.acquired = False
                return

        with os.fdopen(fd, "w") as handle:
            handle.write(str(os.getpid()))
        self.acquired = True

    def __enter__(self) -> "TickLock":
        # wait_seconds > 0 потрібен init_quiz: він теж ходить у getUpdates, а два
        # опитувачі одного бота дають 409 Conflict
        deadline = time.monotonic() + self.wait_seconds
        while True:
            self._try_acquire()
            if self.acquired or time.monotonic() >= deadline:
                return self
            time.sleep(1)

    def __exit__(self, *exc_info) -> None:
        if self.acquired:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
