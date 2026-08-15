"""Статистика завершеної вікторини з архіву.

    python stats.py                    # останній архів
    python stats.py runtime/archive/20260815-110849.json

Головне питання, заради якого це написано: чи програють заявки, подані пізно.
Якщо кореляція часу подачі зі скором стабільно відʼємна на кількох забігах —
формат треба чинити. Якщо ні — рання подача просто не така важлива, як здається,
і жодних додаткових правил вигадувати не треба.
"""
import argparse
import json
from collections import Counter
from pathlib import Path

from votebot import config as cfg_mod


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """Рангова кореляція. Стійка до викидів і не припускає лінійності."""
    n = len(xs)
    if n < 3:
        return None

    def ranks(values):
        order = sorted(range(n), key=lambda i: values[i])
        out = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and values[order[j + 1]] == values[order[i]]:
                j += 1
            average = (i + j) / 2 + 1
            for k in range(i, j + 1):
                out[order[k]] = average
            i = j + 1
        return out

    rx, ry = ranks(xs), ranks(ys)
    d2 = sum((a - b) ** 2 for a, b in zip(rx, ry))
    return 1 - 6 * d2 / (n * (n * n - 1))


def cell_name(cfg: dict, cell) -> str:
    row, col = cell
    x = " ".join(cfg["quiz"]["x_axis"]["values"][col].split())
    y = " ".join(cfg["quiz"]["y_axis"]["values"][row].split())
    return f"{x} + {y}"


def bar(value: float, total: float, width: int = 22) -> str:
    return "█" * round(value / total * width) if total else ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Статистика завершеної вікторини")
    parser.add_argument("archive", nargs="?", help="шлях до архіву; типово — найсвіжіший")
    args = parser.parse_args()

    if args.archive:
        path = Path(args.archive)
    else:
        files = sorted((cfg_mod.RUNTIME_DIR / "archive").glob("*.json"))
        if not files:
            raise SystemExit("Архівів не знайдено — вікторина ще жодного разу не завершувалась")
        path = files[-1]

    data = json.loads(path.read_text(encoding="utf-8"))
    cfg = data["config"]
    rounds = data.get("rounds") or []
    winners = data.get("winners") or []

    print(f"\nАрхів: {path.name}   тема: «{cfg['quiz'].get('title')}»")
    if not rounds:
        print("\nУ цьому архіві немає подробиць по раундах — він записаний старою версією.")
        print("Доступні лише переможці:\n")
        for w in winners:
            print(f"  раунд {w['round']}: {w['author']} — скор {w['score']} з {w['entries_total']} заявок")
        return 0

    # --- Раунди ---
    print(f"\n{'#':>2}  {'клітинка':34} {'заявок':>6} {'голосів':>8} {'скор':>5}  переможець")
    print("-" * 88)
    for r in rounds:
        win = next((e for e in r["entries"] if e["message_id"] == r["winner_message_id"]), None)
        print(f"{r['round']:>2}  {cell_name(cfg, r['cell']):34} {r['entries_total']:>6} "
              f"{r['unique_voters']:>8} {(win or {}).get('score', 0):>5}  {(win or {}).get('author', '—')}")
    print("-" * 88)

    all_entries = [e for r in rounds for e in r["entries"]]
    authors = {e["author_id"] for e in all_entries}
    print(f"{'':2}  {'РАЗОМ':34} {len(all_entries):>6}")
    print(f"\nунікальних учасників: {len(authors)}   "
          f"переможців: {len({w['author_id'] for w in winners})} на {len(winners)} раундів")

    # --- Коли надсилають ---
    timed = [e for e in all_entries if e.get("seconds_after_start") is not None]
    if timed:
        round_len = cfg_mod.round_seconds(cfg["quiz"])
        buckets = Counter(min(int(e["seconds_after_start"] / round_len * 6), 5) for e in timed)
        step = round_len / 6
        print("\nКОЛИ НАДСИЛАЮТЬ ЗАЯВКИ")
        # Для довгих раундів межі зручніше читати у хвилинах, для тестових — у секундах
        unit, div = ("хв", 60) if round_len >= 360 else ("с", 1)
        for b in range(6):
            label = f"{int(b * step // div)}–{int((b + 1) * step // div)} {unit}"
            print(f"  {label:>13}  {buckets[b]:>3} {bar(buckets[b], len(timed))}")
        early = sum(buckets[b] for b in (0, 1)) / len(timed) * 100
        print(f"  у першу третину раунду: {early:.0f}%")

    # --- Чи програють пізні заявки ---
    print("\nЧИ ЗАЛЕЖИТЬ РЕЗУЛЬТАТ ВІД ЧАСУ ПОДАЧІ")
    per_round = []
    for r in rounds:
        pts = [(e["seconds_after_start"], e["score"]) for e in r["entries"]
               if e.get("seconds_after_start") is not None]
        rho = spearman([p[0] for p in pts], [p[1] for p in pts])
        if rho is not None:
            per_round.append((r["round"], rho, len(pts)))
            print(f"  раунд {r['round']}: ρ = {rho:+.2f}  (заявок {len(pts)})")

    if per_round:
        weighted = sum(rho * n for _, rho, n in per_round) / sum(n for _, _, n in per_round)
        print(f"\n  зважена кореляція: ρ = {weighted:+.2f}")
        if weighted <= -0.3:
            verdict = "пізні заявки помітно програють — формат варто чинити"
        elif weighted >= 0.3:
            verdict = "пізні заявки несподівано виграють — варто подивитись, чому"
        else:
            verdict = "виразного звʼязку немає — час подачі не вирішує"
        print(f"  {verdict}")
        print("  (ρ від -1 до +1; відʼємне означає «чим пізніше, тим менший скор»)")
    else:
        print("  замало даних")

    # --- Емодзі ---
    emoji = Counter()
    for e in all_entries:
        emoji.update(e.get("emoji") or {})
    if emoji:
        positive = set(cfg["voting"]["positive"])
        print("\nЯКИМИ ЕМОДЗІ ГОЛОСУЮТЬ")
        for symbol, count in emoji.most_common(12):
            pool = "+" if symbol in positive else ("−" if symbol in set(cfg["voting"]["negative"]) else " ")
            print(f"  {pool} {symbol}  {count:>4} {bar(count, sum(emoji.values()))}")
        unused = [e for e in cfg["voting"]["positive"] + cfg["voting"]["negative"] if e not in emoji]
        if unused:
            print(f"  жодного разу не використали: {' '.join(unused)}")

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
