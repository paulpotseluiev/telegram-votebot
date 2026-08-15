"""Запуск вікторини: перевірка прав, створення стану, публікація першого раунду.

    python init_quiz.py                        # звичайний старт
    python init_quiz.py --dry-run              # відрендерити й показати текст, нічого не публікуючи
    python init_quiz.py --abort                # погасити активну вікторину

    # тестовий забіг: раунд 1 хв, відлік кожні 30 с, кілька заявок від одного юзера
    python init_quiz.py --round-seconds 60 --caption-seconds 30 --multi-entry
"""
import argparse
import logging
import sys
import time

from votebot import config as cfg_mod
from votebot import state as st
from votebot.api import ALLOWED_UPDATES, Bot, TelegramError
from votebot.rounds import build_caption, cell_name, filled_map, open_round, validate_templates
from votebot.render import build_round_media
from votebot.updates import drain
from votebot.util import now_utc, to_iso

log = logging.getLogger("init")


def verify_polling(bot: Bot) -> list[str]:
    """Перевіряє, що бот справді здатний ОТРИМУВАТИ апдейти.

    Прав адміністратора для цього замало. Чужий webhook, успадкований фільтр
    allowed_updates або другий опитувач того самого токена роблять збір
    коментарів і реакцій неможливим — причому МОВЧКИ: getUpdates просто
    повертає порожньо, вікторина крутить раунди й не набирає жодного голосу.
    """
    problems: list[str] = []

    hook = bot.call("getWebhookInfo")
    if hook.get("url"):
        problems.append(
            f"на боті налаштований webhook ({hook['url']}) — getUpdates із ним несумісний"
        )
        return problems

    inherited = hook.get("allowed_updates") or []
    if inherited and [u for u in ALLOWED_UPDATES if u not in inherited]:
        log.warning(
            "Успадкований фільтр allowed_updates=%s не пропускає потрібні нам типи — скидаю",
            inherited,
        )
        bot.call("deleteWebhook")

    conflicts = 0
    for _ in range(3):
        try:
            bot.get_updates(0)
        except TelegramError as exc:
            if "Conflict" not in str(exc):
                raise
            conflicts += 1
        time.sleep(1)

    if conflicts:
        problems.append(
            f"цим самим токеном апдейти читає ще хтось ({conflicts} з 3 спроб getUpdates "
            "дали 409 Conflict). Telegram дозволяє рівно один опитувач на бота — "
            "заведи для вікторини окремого бота в BotFather"
        )
        return problems

    settled = bot.call("getWebhookInfo").get("allowed_updates") or []
    lost = [u for u in ALLOWED_UPDATES if u not in settled]
    if lost:
        problems.append(
            f"Telegram не тримає наш allowed_updates (бракує: {', '.join(lost)}) — "
            "найпевніше, цим ботом керує інший застосунок і перевиставляє фільтр під себе"
        )
    return problems


def preflight(bot: Bot, cfg: dict) -> int:
    """Перевіряє все, що зробить вікторину непрацездатною. Повертає id групи обговорень."""
    channel_id = cfg["telegram"]["channel_id"]
    me = bot.get_me()
    problems = []

    try:
        channel = bot.get_chat(channel_id)
    except TelegramError as exc:
        raise SystemExit(f"Канал {channel_id} недоступний: {exc}")

    group_id = channel.get("linked_chat_id")
    if not group_id:
        problems.append(
            f"до каналу «{channel.get('title')}» не прив'язана група обговорень — "
            "коментарів не буде взагалі"
        )

    try:
        rights = bot.get_chat_member(channel_id, me["id"])
    except TelegramError as exc:
        raise SystemExit(f"Не вдалося прочитати права бота в каналі: {exc}")

    if rights.get("status") != "administrator":
        problems.append("бот не адміністратор каналу")
    else:
        if not rights.get("can_post_messages"):
            problems.append("боту бракує права публікувати повідомлення в каналі")
        if not rights.get("can_edit_messages"):
            problems.append("боту бракує права редагувати повідомлення (потрібне для відліку)")
        if cfg["telegram"].get("delete_previous_post", True) and not rights.get("can_delete_messages"):
            problems.append(
                "боту бракує права видаляти повідомлення, а telegram.delete_previous_post=true"
            )

    if group_id:
        try:
            group_rights = bot.get_chat_member(group_id, me["id"])
        except TelegramError as exc:
            problems.append(f"група обговорень {group_id} недоступна боту: {exc}")
        else:
            if group_rights.get("status") != "administrator":
                # Не косметика: message_reaction прилітає ЛИШЕ адміністраторам чату
                problems.append(
                    "бот не адміністратор групи обговорень — Telegram не надсилатиме "
                    "апдейти про реакції, і голоси рахувати буде нічим"
                )

    if not me.get("can_read_all_group_messages"):
        log.warning(
            "У бота ввімкнений privacy mode. Заявки він побачить як адмін, але надійніше "
            "вимкнути: BotFather → /setprivacy → Disable."
        )

    problems.extend(verify_polling(bot))

    if problems:
        raise SystemExit("Не можу стартувати:\n  - " + "\n  - ".join(problems))

    log.info("Канал «%s» ↔ група %s, права на місці", channel.get("title"), group_id)
    return group_id


def wait_for_thread(bot: Bot, state: dict, timeout: int = 40) -> bool:
    """Чекає автофорвард поста в групу — без нього коментарі нікуди прив'язати."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        drain(bot, state)
        if state["round"].get("thread_root_id"):
            return True
        time.sleep(3)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Запуск вікторини-чарта")
    parser.add_argument("--dry-run", action="store_true", help="відрендерити й показати текст, нічого не публікуючи")
    parser.add_argument("--force", action="store_true", help="почати заново, навіть якщо вікторина активна")
    parser.add_argument("--abort", action="store_true", help="погасити активну вікторину і вийти")
    parser.add_argument("--check", action="store_true",
                        help="перевірити конфіг, права бота й доступ до апдейтів — нічого не запускаючи")
    parser.add_argument("--round-minutes", type=float, help="перекрити тривалість раунду (хвилини)")
    parser.add_argument("--caption-minutes", type=float, help="перекрити інтервал оновлення відліку (хвилини)")
    parser.add_argument("--round-seconds", type=float, help="те саме в секундах; має пріоритет над --round-minutes")
    parser.add_argument("--caption-seconds", type=float, help="те саме в секундах; має пріоритет над --caption-minutes")
    parser.add_argument("--multi-entry", action="store_true",
                        help="дозволити кілька заявок від одного юзера (для тестів наодинці)")
    args = parser.parse_args()

    cfg_mod.setup_logging()
    cfg_mod.ensure_dirs()

    if args.abort:
        current = st.load()
        if st.is_active(current):
            log.info("Гашу вікторину %s (раунд %s)", current.get("quiz_id"), current.get("round_index", 0) + 1)
        st.clear()
        log.info("Стан очищено — крон більше нічого не робитиме")
        return 0

    cfg = cfg_mod.load_config()
    # Секундні перекриття мають пріоритет, тому хвилинні скидають їх у None,
    # інакше значення з config.yaml перебивало б аргумент командного рядка
    if args.round_minutes:
        cfg["quiz"]["round_minutes"], cfg["quiz"]["round_seconds"] = args.round_minutes, None
    if args.round_seconds:
        cfg["quiz"]["round_seconds"] = args.round_seconds
    if args.caption_minutes:
        cfg["quiz"]["caption_update_minutes"], cfg["quiz"]["caption_update_seconds"] = args.caption_minutes, None
    if args.caption_seconds:
        cfg["quiz"]["caption_update_seconds"] = args.caption_seconds
    if args.multi_entry:
        cfg["voting"]["one_entry_per_user"] = False

    cfg_mod.validate(cfg, require_token=not args.dry_run)
    validate_templates(cfg)

    quiz = cfg["quiz"]
    rows, cols = len(quiz["y_axis"]["values"]), len(quiz["x_axis"]["values"])
    order = cfg_mod.resolve_cell_order(quiz["cell_order"], rows, cols)

    if args.check:
        lock = st.TickLock(wait_seconds=90)
        with lock:
            if not lock.acquired:
                raise SystemExit("Не вдалося взяти замок тіка — схоже, якийсь тік завис")
            bot = Bot()
            me = bot.get_me()
            group = preflight(bot, cfg)

        active = st.load()
        running = st.is_active(active)
        print(f"\nБот:        @{me['username']} (id {me['id']})")
        print(f"Канал:      {cfg['telegram']['channel_id']}")
        print(f"Група:      {group}")
        print(f"Раундів:    {len(order)} по {cfg_mod.human_duration(cfg_mod.round_seconds(quiz))}"
              f" (разом {cfg_mod.human_duration(len(order) * cfg_mod.round_seconds(quiz))})")

        if running:
            rnd = active.get("round") or {}
            print(f"Стан:       вже активна — {active.get('quiz_id')}, "
                  f"раунд {rnd.get('index', 0) + 1}/{len(active.get('cell_order', []))}")
            print("\nНалаштування справні, але вікторина вже йде. Щоб почати нову — "
                  "спершу --abort, або запусти з --force.\n")
        else:
            print("Стан:       чисто")
            print("\nУсе гаразд — можна запускати init_quiz.py\n")
        return 0

    current = st.load()
    if st.is_active(current) and not args.force:
        raise SystemExit(
            f"Вікторина {current.get('quiz_id')} ще активна (раунд "
            f"{current.get('round_index', 0) + 1}/{len(current.get('cell_order', []))}). "
            "Додай --force, щоб почати заново, або --abort, щоб її погасити."
        )

    state = {
        "active": True,
        "version": st.STATE_VERSION,
        "quiz_id": now_utc().strftime("%Y%m%d-%H%M%S"),
        "created_at": to_iso(now_utc()),
        "finished_at": None,
        "config": cfg,
        "channel_id": cfg["telegram"]["channel_id"],
        "discussion_chat_id": None,
        "rows": rows,
        "cols": cols,
        "cell_order": order,
        "round_index": 0,
        "round": None,
        "rounds": [],      # підсумки закритих раундів — для архіву й аналізу
        "winners": [],
    }

    if args.dry_run:
        from datetime import timedelta

        state["round"] = {"index": 0, "cell": order[0]}
        path, kind = build_round_media(
            cfg, filled_map(state), tuple(order[0]), cfg_mod.MEDIA_DIR / "dryrun_round01"
        )
        round_s = cfg_mod.round_seconds(quiz)
        caption = build_caption(state, timedelta(seconds=round_s))
        print(f"\nМедіа:    {path} ({kind}, {path.stat().st_size / 1024:.0f} КБ)")
        print(f"Раундів:  {len(order)} по {cfg_mod.human_duration(round_s)} "
              f"(разом {cfg_mod.human_duration(len(order) * round_s)})")
        print(f"Відлік:   кожні {cfg_mod.human_duration(cfg_mod.caption_seconds(quiz))}")
        print(f"Заявок:   {'кілька від одного юзера' if not cfg['voting']['one_entry_per_user'] else 'одна на юзера'}")
        print(f"Порядок:  {' → '.join(cell_name(state, c) for c in order)}")
        print(f"\n--- Підпис ---\n{caption}\n")
        return 0

    # Замок тіка беремо на весь старт: init теж ходить у getUpdates, а два
    # опитувачі одного бота дають 409 Conflict і губляться апдейти
    lock = st.TickLock(wait_seconds=90)
    with lock:
        if not lock.acquired:
            raise SystemExit(
                "Не вдалося взяти замок тіка за 90 с. Схоже, якийсь тік завис — "
                f"перевір {cfg_mod.LOCK_PATH} і процеси tick.py."
            )

        bot = Bot()
        state["discussion_chat_id"] = preflight(bot, cfg)

        # Чергу від попередніх забігів прибираємо ДО старту, інакше перший тік
        # зарахує давні коментарі як заявки цього раунду.
        drained = drain(bot, {"active": False})
        if drained["updates"]:
            log.info("Прибрано %s старих апдейтів з черги", drained["updates"])

        open_round(bot, state, 0)

        if wait_for_thread(bot, state):
            log.info("Гілка коментарів прив'язана: %s", state["round"]["thread_root_id"])
        else:
            log.warning(
                "Автофорвард у групу поки не прилетів. Це не фатально — його підхопить "
                "найближчий тік, але перевір, що група справді прив'язана як обговорення."
            )
        st.save(state)

    rnd = state["round"]
    print(f"\nВікторина {state['quiz_id']} стартувала")
    print(f"  Пост:     {rnd['post_message_id']} у каналі {state['channel_id']}")
    print(f"  Раунд:    1/{len(order)} — {cell_name(state, rnd['cell'])}")
    print(f"  Тривалість: {cfg_mod.human_duration(cfg_mod.round_seconds(quiz))}, "
          f"відлік кожні {cfg_mod.human_duration(cfg_mod.caption_seconds(quiz))}")
    print(f"  Кінець:   {rnd['ends_at']} (UTC)")
    print(f"  Стан:     {cfg_mod.STATE_PATH}")
    print("\nДалі все робить таймер systemd: votebot.timer.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
