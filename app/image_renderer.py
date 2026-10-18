from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from app.utils import clean_submission_text


class ImageLayoutError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RenderConfig:
    width: int = 1080
    height: int = 1350
    background: tuple[int, int, int] = (24, 32, 45)
    foreground: tuple[int, int, int] = (244, 247, 250)
    muted: tuple[int, int, int] = (145, 157, 175)
    divider: tuple[int, int, int] = (52, 63, 80)
    telegram_blue: tuple[int, int, int] = (39, 167, 231)
    brand: str = "新工爆言版"
    body_left: int = 112
    body_right: int = 86
    body_top: int = 255
    body_bottom: int = 1125
    font_max: int = 56
    font_min: int = 34
    font_step: int = 2
    line_gap: int = 17
    max_pages: int = 10


def _font(path: str | Path, size: int) -> ImageFont.FreeTypeFont:
    font_path = Path(path)
    if not font_path.is_file():
        raise FileNotFoundError(f"找不到字型：{font_path}")
    return ImageFont.truetype(str(font_path), size=size)


def _line_height(font: ImageFont.FreeTypeFont, gap: int) -> int:
    _, top, _, bottom = font.getbbox("國Ag")
    return bottom - top + gap


def _wrap_by_pixels(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
) -> list[str]:
    lines: list[str] = []
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    for source_line in normalized.split("\n"):
        if source_line == "":
            lines.append("")
            continue
        current = ""
        for char in source_line:
            candidate = current + char
            if not current or draw.textlength(candidate, font=font) <= max_width:
                current = candidate
            else:
                lines.append(current.rstrip())
                current = "" if char.isspace() else char
        lines.append(current.rstrip())
    while lines and lines[-1] == "":
        lines.pop()
    return lines or [""]


def _paginate(lines: list[str], lines_per_page: int) -> list[list[str]]:
    pages: list[list[str]] = []
    for offset in range(0, len(lines), lines_per_page):
        page = lines[offset : offset + lines_per_page]
        while page and page[0] == "":
            page = page[1:]
        while page and page[-1] == "":
            page = page[:-1]
        pages.append(page or [""])
    return pages


def _choose_layout(
    text: str,
    font_path: str | Path,
    config: RenderConfig,
) -> tuple[ImageFont.FreeTypeFont, int, list[list[str]]]:
    probe = Image.new("RGB", (config.width, config.height), config.background)
    draw = ImageDraw.Draw(probe)
    max_width = config.width - config.body_left - config.body_right
    body_height = config.body_bottom - config.body_top
    for size in range(config.font_max, config.font_min - 1, -config.font_step):
        font = _font(font_path, size)
        line_height = _line_height(font, config.line_gap)
        lines = _wrap_by_pixels(draw, text, font, max_width)
        pages = _paginate(lines, max(1, body_height // line_height))
        if len(pages) <= config.max_pages:
            return font, line_height, pages
    raise ImageLayoutError(
        f"投稿內容無法在最低字級內排成 {config.max_pages} 張圖片"
    )


def _fallback_logo(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    size: int,
    blue: tuple[int, int, int],
) -> None:
    draw.ellipse((x, y, x + size, y + size), fill=blue)
    scale = size / 100
    points = [
        (x + 18 * scale, y + 48 * scale),
        (x + 82 * scale, y + 23 * scale),
        (x + 68 * scale, y + 78 * scale),
        (x + 47 * scale, y + 61 * scale),
        (x + 35 * scale, y + 72 * scale),
        (x + 38 * scale, y + 55 * scale),
    ]
    draw.polygon(points, fill=(255, 255, 255))


def _place_logo(
    image: Image.Image,
    draw: ImageDraw.ImageDraw,
    logo_path: str | Path | None,
    config: RenderConfig,
) -> None:
    size = 104
    x = config.width - 58 - size
    y = config.height - 58 - size
    if logo_path and Path(logo_path).is_file():
        with Image.open(logo_path) as source:
            logo = source.convert("RGBA")
            logo.thumbnail((size, size), Image.Resampling.LANCZOS)
            image.paste(
                logo,
                (x + (size - logo.width) // 2, y + (size - logo.height) // 2),
                logo,
            )
        return
    _fallback_logo(draw, x, y, size, config.telegram_blue)


def render_post_images(
    text: str,
    *,
    regular_font_path: str | Path,
    bold_font_path: str | Path,
    logo_path: str | Path | None = None,
    config: RenderConfig | None = None,
) -> list[bytes]:
    config = config or RenderConfig()
    clean_text = clean_submission_text(text)

    body_font, line_height, pages = _choose_layout(
        clean_text, regular_font_path, config
    )
    brand_font = _font(bold_font_path, 48)
    page_font = _font(regular_font_path, 27)
    output: list[bytes] = []
    for page_number, lines in enumerate(pages, start=1):
        image = Image.new("RGB", (config.width, config.height), config.background)
        draw = ImageDraw.Draw(image)
        draw.text(
            (config.width // 2, 76),
            config.brand,
            font=brand_font,
            fill=config.foreground,
            anchor="ma",
        )
        draw.line((0, 176, config.width, 176), fill=config.divider, width=2)
        draw.rounded_rectangle(
            (
                config.body_left - 34,
                config.body_top + 2,
                config.body_left - 25,
                config.body_top + 78,
            ),
            radius=4,
            fill=config.telegram_blue,
        )
        y = config.body_top
        for line in lines:
            if line:
                draw.text((config.body_left, y), line, font=body_font, fill=config.foreground)
            y += line_height
        if len(pages) > 1:
            draw.text(
                (config.body_left, config.height - 100),
                f"{page_number} / {len(pages)}",
                font=page_font,
                fill=config.muted,
                anchor="lm",
            )
        _place_logo(image, draw, logo_path, config)
        buffer = BytesIO()
        image.save(buffer, format="PNG", optimize=True)
        output.append(buffer.getvalue())
    return output
