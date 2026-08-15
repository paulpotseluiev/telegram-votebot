"""Рендер чарта N×M і анімації активної клітинки.

Статична основа (сітка, підписи, вже заповнені клітинки) малюється рівно один
раз, а кожен кадр — це основа плюс «дихаюча» підсвітка активної клітинки.
Завдяки цьому 18 кадрів коштують майже стільки ж, скільки один.

Формат виводу обирається автоматично:
    mp4 — якщо в системі є ffmpeg. Фото зберігають повний колір, файл малий.
    gif — запасний варіант. 256 кольорів помітно псують фото, тому GIF ще й
          масштабується вниз, інакше файл роздувається до десятків мегабайт.
    png — коли анімація не потрібна взагалі.
"""
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

log = logging.getLogger(__name__)

FONT_CANDIDATES = {
    True: [  # жирний
        "C:/Windows/Fonts/segoeuib.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
        "/Library/Fonts/Arial Bold.ttf",
    ],
    False: [  # звичайний
        "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
        "/Library/Fonts/Arial.ttf",
    ],
}

_font_cache: dict[tuple[str | None, int, bool], ImageFont.FreeTypeFont] = {}
_font_path_cache: dict[tuple[str | None, bool], str | None] = {}


def _resolve_font_path(override: str | None, bold: bool) -> str | None:
    key = (override, bold)
    if key in _font_path_cache:
        return _font_path_cache[key]

    found = None
    if override and Path(override).exists():
        found = override
    else:
        if override:
            log.warning("Шрифт %s не знайдено, шукаю системний", override)
        for candidate in FONT_CANDIDATES[bold]:
            if Path(candidate).exists():
                found = candidate
                break
    if not found:
        log.warning(
            "Не знайдено жодного системного шрифту (bold=%s) — Pillow візьме "
            "вбудований бітмапний, кирилиця може виглядати погано. "
            "Задай render.font / render.font_bold у config.yaml.",
            bold,
        )
    _font_path_cache[key] = found
    return found


def _font(size: int, bold: bool, override: str | None = None) -> ImageFont.FreeTypeFont:
    path = _resolve_font_path(override, bold)
    key = (path, size, bold)
    if key not in _font_cache:
        _font_cache[key] = (
            ImageFont.truetype(path, size) if path else ImageFont.load_default(size)
        )
    return _font_cache[key]


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))


def _text_size(font: ImageFont.FreeTypeFont, text: str) -> tuple[int, int]:
    x0, y0, x1, y1 = font.getbbox(text)
    return x1 - x0, y1 - y0


def _fit_font(texts, max_w: int, start: int, bold: bool, override: str | None, min_size: int = 11):
    """Найбільший кегль, при якому В УСІ передані рядки влазять у max_w.

    Приймає і один рядок, і список: підписи однієї осі мусять мати спільний
    розмір, інакше коротке «марно» виглядає крупнішим за «ефективно».
    """
    if isinstance(texts, str):
        texts = [texts]
    # Підпис може бути багаторядковим —міряємо кожен рядок окремо
    texts = [line for t in texts if t for line in t.split("\n")] or [""]

    size = start
    while size > min_size:
        font = _font(size, bold, override)
        if all(_text_size(font, t)[0] <= max_w for t in texts):
            return font
        size -= 1
    return _font(min_size, bold, override)


def _ellipsize(text: str, font: ImageFont.FreeTypeFont, max_w: int) -> str:
    if _text_size(font, text)[0] <= max_w:
        return text
    ellipsis = "…"
    trimmed = text
    while trimmed and _text_size(font, trimmed + ellipsis)[0] > max_w:
        trimmed = trimmed[:-1]
    return (trimmed + ellipsis) if trimmed else ellipsis


def _draw_lines(draw, text: str, font, color, x: int, y_center: int, anchor_h: str, line_gap: float = 1.2) -> None:
    """Малює підпис, можливо багаторядковий, центрований по вертикалі відносно y_center."""
    lines = text.split("\n")
    line_h = round(font.size * line_gap)
    top = y_center - (line_h * len(lines)) // 2
    for i, line in enumerate(lines):
        draw.text((x, top + line_h * i + line_h // 2), line, font=font, fill=color, anchor=f"{anchor_h}m")


def _draw_rotated(base: Image.Image, text: str, font, color, center: tuple[int, int]) -> None:
    """Малює вертикальний підпис (знизу вгору) з центром у center."""
    w, h = _text_size(font, text)
    pad = 4
    strip = Image.new("RGBA", (w + pad * 2, h + pad * 2), (0, 0, 0, 0))
    ImageDraw.Draw(strip).text((pad, pad), text, font=font, fill=color, anchor="lt")
    strip = strip.rotate(90, expand=True, resample=Image.BICUBIC)
    base.paste(strip, (center[0] - strip.width // 2, center[1] - strip.height // 2), strip)


def _fit_into_cell(
    photo: Image.Image,
    box_w: int,
    box_h: int,
    *,
    square_tolerance: float,
    letterbox: str,
    bg_color: tuple[int, int, int],
) -> Image.Image:
    """Вписує картинку в клітинку, нічого не обрізаючи.

    Учасники шлють що завгодно, тому:
      • майже квадратне — просто розтягуємо в квадрат, спотворення непомітне;
      • вертикальне — вписуємо по висоті, з полями з боків;
      • горизонтальне — вписуємо по ширині, з полями зверху й знизу.
    Поля заповнюємо розмитим збільшенням тієї ж картинки (або рівним кольором),
    щоб клітинка не мала мертвих зон.
    """
    cell_ratio = box_w / box_h
    ratio = photo.width / photo.height

    if 1 / (1 + square_tolerance) <= ratio / cell_ratio <= 1 + square_tolerance:
        return photo.resize((box_w, box_h), Image.LANCZOS)

    fitted = photo.copy()
    fitted.thumbnail((box_w, box_h), Image.LANCZOS)

    if letterbox == "blur":
        backdrop = ImageOps.fit(photo, (box_w, box_h), method=Image.LANCZOS, centering=(0.5, 0.5))
        backdrop = backdrop.filter(ImageFilter.GaussianBlur(max(6, box_w // 22)))
        backdrop = ImageEnhance.Brightness(backdrop).enhance(0.55)
    else:
        backdrop = Image.new("RGB", (box_w, box_h), bg_color)

    backdrop.paste(fitted, ((box_w - fitted.width) // 2, (box_h - fitted.height) // 2))
    return backdrop


def _perimeter_point(rect: tuple[int, int, int, int], distance: float, perimeter: float) -> tuple[float, float]:
    """Точка на периметрі прямокутника на відстані distance від лівого верхнього кута.

    Обхід за годинниковою стрілкою: вправо по верху → вниз по правому боку →
    вліво по низу → вгору по лівому.
    """
    x0, y0, x1, y1 = rect
    width, height = x1 - x0, y1 - y0
    distance %= perimeter

    if distance < width:
        return (x0 + distance, y0)
    distance -= width
    if distance < height:
        return (x1, y0 + distance)
    distance -= height
    if distance < width:
        return (x1 - distance, y1)
    distance -= width
    return (x0, y1 - distance)


class ChartLayout:
    """Геометрія чарта. Розміри виводяться з ширини, щоб усе масштабувалось разом."""

    def __init__(self, width: int, rows: int, cols: int, *, has_title: bool, has_x_title: bool,
                 has_y_title: bool, row_label_frac: float = 0.140, col_label_lines: int = 1):
        width = width + (width % 2)  # парна ширина — вимога h264
        self.width = width
        self.rows = rows
        self.cols = cols

        self.margin = round(width * 0.030)
        self.line_w = max(3, round(width * 0.005))

        self.y_title_w = round(width * 0.045) if has_y_title else 0
        self.row_label_w = round(width * row_label_frac)
        self.title_h = round(width * 0.072) if has_title else 0
        self.x_title_h = round(width * 0.044) if has_x_title else 0
        self.col_label_h = round(width * 0.052) * max(1, col_label_lines)

        left = self.margin + self.y_title_w + self.row_label_w
        available = width - left - self.margin
        self.cell = available // cols
        self.grid_w = self.cell * cols
        # залишок від ділення розкидаємо порівну, щоб сітка не «липла» до краю
        self.grid_x = left + (available - self.grid_w) // 2
        self.grid_y = self.margin + self.title_h + self.x_title_h + self.col_label_h
        self.grid_h = self.cell * rows

        height = self.grid_y + self.grid_h + self.margin
        self.height = height + (height % 2)

    def cell_rect(self, row: int, col: int) -> tuple[int, int, int, int]:
        x0 = self.grid_x + col * self.cell
        y0 = self.grid_y + row * self.cell
        return x0, y0, x0 + self.cell, y0 + self.cell


def render_base(
    *,
    x_values: list[str],
    y_values: list[str],
    x_title: str = "",
    y_title: str = "",
    title: str = "",
    filled: dict[tuple[int, int], dict] | None = None,
    active: tuple[int, int] | None = None,
    theme: dict,
    width: int = 1200,
    font: str | None = None,
    font_bold: str | None = None,
    credit_template: str = "{author}",
    square_tolerance: float = 0.15,
    letterbox: str = "blur",
) -> tuple[Image.Image, ChartLayout]:
    """Малює статичну основу чарта (все, крім пульсуючої підсвітки)."""
    filled = filled or {}
    rows, cols = len(y_values), len(x_values)

    # Бічне поле під підписи рядків розширюємо під найдовший рядок: підписи
    # однієї осі малюються спільним кеглем, тож довге «миттєва смерть» одним
    # рядком стиснуло б усю вісь Y. Перенос у конфізі (\n) знімає потребу.
    probe = _font(round(width * 0.031), True, font_bold)
    y_lines = [line for v in y_values for line in v.split("\n")]
    needed = max((_text_size(probe, line)[0] for line in y_lines), default=0) + round(width * 0.022)
    row_label_frac = min(0.26, max(0.140, needed / width))

    lay = ChartLayout(
        width, rows, cols,
        has_title=bool(title), has_x_title=bool(x_title), has_y_title=bool(y_title),
        row_label_frac=row_label_frac,
        col_label_lines=max((len(v.split("\n")) for v in x_values), default=1),
    )

    c_bg = _hex_to_rgb(theme["background"])
    c_grid = _hex_to_rgb(theme["grid"])
    c_text = _hex_to_rgb(theme["text"])
    c_muted = _hex_to_rgb(theme["muted"])
    c_empty = _hex_to_rgb(theme["empty_cell"])
    c_hl = _hex_to_rgb(theme["highlight"])
    c_cap_bg = _hex_to_rgb(theme["caption_bg"])
    c_cap_text = _hex_to_rgb(theme["caption_text"])

    img = Image.new("RGB", (lay.width, lay.height), c_bg)
    draw = ImageDraw.Draw(img)

    # --- Заголовок ---
    if title:
        f = _fit_font(title, lay.width - 2 * lay.margin, round(lay.width * 0.046), True, font_bold)
        draw.text((lay.width // 2, lay.margin + lay.title_h // 2), title, font=f, fill=c_text, anchor="mm")

    # --- Підпис осі X ---
    if x_title:
        f = _fit_font(x_title, lay.grid_w, round(lay.width * 0.027), False, font)
        y = lay.margin + lay.title_h + lay.x_title_h // 2
        draw.text((lay.grid_x + lay.grid_w // 2, y), x_title, font=f, fill=c_muted, anchor="mm")

    # --- Підписи колонок (спільний кегль на всю вісь) ---
    label_w = lay.cell - round(lay.cell * 0.08)
    f = _fit_font(x_values, label_w, round(lay.width * 0.031), True, font_bold)
    for col, label in enumerate(x_values):
        x = lay.grid_x + col * lay.cell + lay.cell // 2
        _draw_lines(draw, label, f, c_text, x, lay.grid_y - lay.col_label_h // 2, "m")

    # --- Підпис осі Y (вертикально) ---
    if y_title:
        f = _fit_font(y_title, lay.grid_h, round(lay.width * 0.027), False, font)
        _draw_rotated(img, y_title, f, c_muted,
                      (lay.margin + lay.y_title_w // 2, lay.grid_y + lay.grid_h // 2))

    # --- Підписи рядків (спільний кегль на всю вісь) ---
    row_label_right = lay.grid_x - round(lay.width * 0.012)
    row_label_max = row_label_right - (lay.margin + lay.y_title_w)
    f = _fit_font(y_values, row_label_max, round(lay.width * 0.031), True, font_bold)
    for row, label in enumerate(y_values):
        y = lay.grid_y + row * lay.cell + lay.cell // 2
        _draw_lines(draw, label, f, c_text, row_label_right, y, "r")

    # --- Клітинки ---
    credit_h = max(22, round(lay.cell * 0.115))
    credit_font_size = max(12, round(credit_h * 0.62))
    inset = lay.line_w // 2

    for row in range(rows):
        for col in range(cols):
            x0, y0, x1, y1 = lay.cell_rect(row, col)
            box = (x0 + inset, y0 + inset, x1 - inset, y1 - inset)
            box_w, box_h = box[2] - box[0], box[3] - box[1]
            entry = filled.get((row, col))

            if not entry:
                draw.rectangle(box, fill=c_empty)
                continue

            try:
                with Image.open(entry["image"]) as src:
                    photo = ImageOps.exif_transpose(src).convert("RGB")
                    photo = _fit_into_cell(
                        photo, box_w, box_h,
                        square_tolerance=square_tolerance, letterbox=letterbox, bg_color=c_empty,
                    )
                img.paste(photo, (box[0], box[1]))
            except Exception as exc:  # битий файл не має валити весь раунд
                log.warning("Не вдалося вставити %s у клітинку (%s,%s): %s", entry.get("image"), row, col, exc)
                draw.rectangle(box, fill=c_empty)
                continue

            author = (entry.get("author") or "").strip()
            if author and credit_template:
                credit = credit_template.format(author=author)
                strip = Image.new("RGBA", (box_w, credit_h), c_cap_bg + (170,))
                img.paste(
                    Image.alpha_composite(
                        img.crop((box[0], box[3] - credit_h, box[2], box[3])).convert("RGBA"), strip
                    ).convert("RGB"),
                    (box[0], box[3] - credit_h),
                )
                cf = _font(credit_font_size, False, font)
                draw.text(
                    (box[0] + box_w // 2, box[3] - credit_h // 2),
                    _ellipsize(credit, cf, box_w - 16),
                    font=cf, fill=c_cap_text, anchor="mm",
                )

    # --- Знак питання в порожній активній клітинці ---
    if active and active not in filled:
        x0, y0, x1, y1 = lay.cell_rect(*active)
        qf = _font(round(lay.cell * 0.42), True, font_bold)
        mark = Image.new("RGBA", (x1 - x0, y1 - y0), (0, 0, 0, 0))
        ImageDraw.Draw(mark).text(
            ((x1 - x0) // 2, (y1 - y0) // 2), "?", font=qf, fill=c_hl + (70,), anchor="mm"
        )
        img.paste(Image.alpha_composite(img.crop((x0, y0, x1, y1)).convert("RGBA"), mark).convert("RGB"), (x0, y0))

    # --- Сітка поверх усього ---
    for col in range(cols + 1):
        x = lay.grid_x + col * lay.cell
        draw.line([(x, lay.grid_y), (x, lay.grid_y + lay.grid_h)], fill=c_grid, width=lay.line_w)
    for row in range(rows + 1):
        y = lay.grid_y + row * lay.cell
        draw.line([(lay.grid_x, y), (lay.grid_x + lay.grid_w, y)], fill=c_grid, width=lay.line_w)

    return img, lay


def _apply_snake(
    base: Image.Image,
    lay: ChartLayout,
    active: tuple[int, int],
    color: tuple[int, int, int],
    head: float | None,
    snake_length: float = 0.28,
) -> Image.Image:
    """Малює активну клітинку: тьмяна рамка + яскрава «змійка» по периметру.

    head ∈ [0, 1) — позиція голови змійки на периметрі. head=None дає суцільну
    яскраву рамку (статичний вивід, де бігати нічому).
    """
    frame = base.copy()
    x0, y0, x1, y1 = lay.cell_rect(*active)
    # Запас під сяйво: товщина рамки + хвіст розмиття. Замалий pad обріже
    # сяйво на межі crop і дасть видимий шов.
    pad = lay.line_w * 10

    box = (max(0, x0 - pad), max(0, y0 - pad), min(base.width, x1 + pad), min(base.height, y1 + pad))
    region = frame.crop(box).convert("RGBA")
    local = (x0 - box[0], y0 - box[1], x1 - box[0], y1 - box[1])
    border_w = max(lay.line_w, round(lay.line_w * 1.6))

    # Тьмяний контур: клітинка позначена навіть там, де змійки зараз немає
    dim = Image.new("RGBA", region.size, (0, 0, 0, 0))
    ImageDraw.Draw(dim).rectangle(local, outline=color + (85,), width=border_w)
    region = Image.alpha_composite(region, dim)

    bright = Image.new("RGBA", region.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(bright)

    if head is None:
        draw.rectangle(local, outline=color + (255,), width=border_w)
    else:
        width, height = local[2] - local[0], local[3] - local[1]
        perimeter = 2 * (width + height)
        head_at = (head % 1.0) * perimeter
        length = max(perimeter * snake_length, lay.line_w * 8)

        # Дрібний крок — щоб хорда не зрізала кути прямокутника
        steps = max(24, int(length / 3))
        for k in range(steps):
            far, near = k / steps, (k + 1) / steps
            point_a = _perimeter_point(local, head_at - length * (1 - far), perimeter)
            point_b = _perimeter_point(local, head_at - length * (1 - near), perimeter)
            # Хвіст згасає: alpha росте від нуля біля хвоста до 255 біля голови.
            # Показник близький до одиниці — інакше видно лише яскраву крапку
            # замість сегмента, що біжить.
            draw.line([point_a, point_b], fill=color + (int(255 * near**1.15),), width=border_w)

    glow = bright.filter(ImageFilter.GaussianBlur(lay.line_w * 1.8))
    region = Image.alpha_composite(region, glow)
    region = Image.alpha_composite(region, bright)

    frame.paste(region.convert("RGB"), (box[0], box[1]))
    return frame


def render_frames(
    *, frames: int = 18, active=None, theme: dict, snake_length: float = 0.28, **kwargs
) -> list[Image.Image]:
    """Повертає список кадрів. Без active — один статичний кадр."""
    base, lay = render_base(active=active, theme=theme, **kwargs)
    if not active:
        return [base]

    color = _hex_to_rgb(theme["highlight"])
    if frames <= 1:
        # Статичний вивід — суцільна рамка, інакше картинка не показує,
        # за яку саме клітинку йде голосування.
        return [_apply_snake(base, lay, active, color, None, snake_length)]

    # Голова проходить рівно один повний оберт, тому цикл склеюється безшовно
    return [
        _apply_snake(base, lay, active, color, i / frames, snake_length)
        for i in range(frames)
    ]


def save_media(
    frames: list[Image.Image],
    out_stem: Path,
    *,
    fmt: str = "auto",
    fps: int = 12,
    gif_max_width: int = 800,
) -> tuple[Path, str]:
    """Кодує кадри. Повертає (шлях, вид), де вид ∈ {mp4, gif, png}."""
    out_stem = Path(out_stem)
    out_stem.parent.mkdir(parents=True, exist_ok=True)

    if len(frames) == 1:
        fmt = "png"
    elif fmt == "auto":
        fmt = "mp4" if shutil.which("ffmpeg") else "gif"
    elif fmt == "mp4" and not shutil.which("ffmpeg"):
        log.warning("render.format=mp4, але ffmpeg не знайдено — відкочуюсь на gif")
        fmt = "gif"

    if fmt == "png":
        path = out_stem.with_suffix(".png")
        frames[len(frames) // 2].save(path, optimize=True)
        return path, "png"

    if fmt == "mp4":
        path = out_stem.with_suffix(".mp4")
        with tempfile.TemporaryDirectory(prefix="votebot-") as tmp:
            tmp = Path(tmp)
            for i, frame in enumerate(frames):
                frame.save(tmp / f"f{i:04d}.png")
            cmd = [
                "ffmpeg", "-y", "-loglevel", "error",
                "-framerate", str(fps),
                "-i", str(tmp / "f%04d.png"),
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-pix_fmt", "yuv420p",
                "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                "-movflags", "+faststart",
                str(path),
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            log.error("ffmpeg впав (%s): %s", result.returncode, result.stderr.strip()[:500])
            return save_media(frames, out_stem, fmt="gif", fps=fps, gif_max_width=gif_max_width)
        return path, "mp4"

    # --- GIF ---
    path = out_stem.with_suffix(".gif")
    work = frames
    if gif_max_width and frames[0].width > gif_max_width:
        scale = gif_max_width / frames[0].width
        size = (gif_max_width, round(frames[0].height * scale))
        work = [f.resize(size, Image.LANCZOS) for f in frames]

    # Спільна палітра на всі кадри: інакше кожен кадр тягне власну таблицю
    # кольорів і GIF роздувається у кілька разів.
    palette = work[0].convert("P", palette=Image.ADAPTIVE, colors=256)
    quantized = [f.quantize(palette=palette, dither=Image.FLOYDSTEINBERG) for f in work]
    quantized[0].save(
        path,
        save_all=True,
        append_images=quantized[1:],
        duration=max(20, round(1000 / fps)),
        loop=0,
        optimize=True,
        disposal=1,
    )
    return path, "gif"


def build_round_media(cfg: dict, filled: dict, active, out_stem: Path) -> tuple[Path, str]:
    """Зручна обгортка: бере параметри просто з конфіга вікторини."""
    quiz, render_cfg = cfg["quiz"], cfg["render"]
    frame_count = 1
    if active and render_cfg["format"] != "png":
        frame_count = max(2, round(render_cfg["fps"] * render_cfg["loop_seconds"]))

    frames = render_frames(
        x_values=quiz["x_axis"]["values"],
        y_values=quiz["y_axis"]["values"],
        x_title=quiz["x_axis"].get("title", ""),
        y_title=quiz["y_axis"].get("title", ""),
        title=quiz.get("title", ""),
        filled=filled,
        active=active,
        theme=render_cfg["theme"],
        width=render_cfg["width"],
        font=render_cfg.get("font"),
        font_bold=render_cfg.get("font_bold"),
        credit_template=cfg["templates"].get("cell_credit", "{author}"),
        square_tolerance=render_cfg["square_tolerance"],
        letterbox=render_cfg["letterbox"],
        snake_length=render_cfg["snake_length"],
        frames=frame_count,
    )
    return save_media(
        frames, out_stem,
        fmt=render_cfg["format"], fps=render_cfg["fps"], gif_max_width=render_cfg["gif_max_width"],
    )
