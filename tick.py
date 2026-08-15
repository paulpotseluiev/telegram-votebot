"""Точка входу крону. Один запуск = один прохід.

Ідемпотентна і дешева: якщо активної вікторини немає, тік лише зливає чергу
апдейтів і виходить. Ставити раз на хвилину:

    * * * * * cd /opt/votebot && /opt/votebot/.venv/bin/python tick.py >> runtime/tick.log 2>&1
"""
import logging
import sys

from votebot import config as cfg_mod
from votebot import state as st
from votebot.api import Bot
from votebot.rounds import tick


def main() -> int:
    cfg_mod.setup_logging()
    cfg_mod.ensure_dirs()
    log = logging.getLogger("tick")

    with st.TickLock() as lock:
        if not lock.acquired:
            log.info("Попередній тік ще працює — пропускаю цей запуск")
            return 0
        try:
            tick(Bot())
        except Exception:
            log.exception("Тік завершився з помилкою")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
