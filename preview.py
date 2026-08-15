"""Превʼю чарта без запуску вікторини.

Малює чарт із поточного config.yaml, підставляючи згенеровані заглушки замість
картинок переможців. Потрібен, щоб крутити оформлення (кольори, шрифти,
пропорції) не чекаючи живого голосування.

    python preview.py                 # 4 заповнені клітинки, активна — пʼята
    python preview.py --filled 0      # порожній чарт, активна — перша
    python preview.py --filled 9      # повністю заповнений, без підсвітки
    python preview.py --format gif
"""
import argparse
import colorsys
from pathlib import Path

from PIL import Image, ImageDraw

from votebot import config as cfg_mod
from votebot.render import _font, build_round_media

PLACEHOLDER_NAMES = [
    "Олег Коваленко", "Марія Іваненко", "Дмитро Шевченко",
    "Анна Бондаренко", "Тарас Мельник", "Ірина Ткаченко",
    # Навмисно задовге — перевірка обрізання підпису під клітинкою
    "Володимир Христопольський-Заболотний",
    "Богдан Кравченко", "Юлія Савченко",
]


# Навмисно різні пропорції: учасники шлють що завгодно, і рендер має
# коректно опрацювати вертикальні, горизонтальні та майже-квадратні картинки.
SHAPES = [
    (900, 500),   # горизонтальна
    (520, 900),   # вертикальна
    (700, 660),   # майже квадратна — має розтягнутись
    (640, 640),   # рівно квадратна
    (1200, 480),  # дуже широка
    (480, 1100),  # дуже висока
    (820, 760),   # майже квадратна
    (600, 900),   # вертикальна
    (1000, 620),  # горизонтальна
]


def make_placeholder(index: int) -> Path:
    """Кольоровий градієнт із номером і підписом пропорцій."""
    out_dir = cfg_mod.MEDIA_DIR / "preview"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"placeholder_{index}.jpg"

    width, height = SHAPES[index % len(SHAPES)]
    hue = (index * 0.137) % 1.0
    top = tuple(round(c * 255) for c in colorsys.hsv_to_rgb(hue, 0.55, 0.95))
    bottom = tuple(round(c * 255) for c in colorsys.hsv_to_rgb((hue + 0.08) % 1.0, 0.75, 0.55))

    img = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(img)
    for y in range(height):
        t = y / height
        draw.line(
            [(0, y), (width, y)],
            fill=tuple(round(top[i] + (bottom[i] - top[i]) * t) for i in range(3)),
        )

    short = min(width, height)
    draw.text(
        (width // 2, height // 2), str(index + 1),
        fill=(255, 255, 255), anchor="mm", font=_font(round(short * 0.34), True, None),
    )
    draw.text(
        (width // 2, height - round(short * 0.08)), f"{width}×{height}",
        fill=(255, 255, 255), anchor="mm", font=_font(round(short * 0.09), False, None),
    )
    img.save(path, quality=90)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Превʼю чарта вікторини")
    parser.add_argument("--filled", type=int, default=4, help="скільки клітинок заповнити заглушками")
    parser.add_argument("--format", dest="fmt", choices=["auto", "mp4", "gif", "png"], help="перевизначити render.format")
    parser.add_argument("--width", type=int, help="перевизначити render.width")
    args = parser.parse_args()

    cfg = cfg_mod.load_config()
    cfg_mod.validate(cfg, require_token=False)
    if args.fmt:
        cfg["render"]["format"] = args.fmt
    if args.width:
        cfg["render"]["width"] = args.width

    quiz = cfg["quiz"]
    rows, cols = len(quiz["y_axis"]["values"]), len(quiz["x_axis"]["values"])
    order = cfg_mod.resolve_cell_order(quiz["cell_order"], rows, cols)

    filled_count = max(0, min(args.filled, len(order)))
    filled = {
        (r, c): {"image": make_placeholder(i), "author": PLACEHOLDER_NAMES[i % len(PLACEHOLDER_NAMES)]}
        for i, (r, c) in enumerate(order[:filled_count])
    }
    active = tuple(order[filled_count]) if filled_count < len(order) else None

    path, kind = build_round_media(cfg, filled, active, cfg_mod.MEDIA_DIR / "preview" / "chart")

    size_kb = path.stat().st_size / 1024
    print(f"Формат:   {kind}")
    print(f"Файл:     {path}")
    print(f"Розмір:   {size_kb:.0f} КБ")
    print(f"Активна:  {active if active else '— (чарт заповнено)'}")


if __name__ == "__main__":
    main()
