from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from app.image_renderer import ImageLayoutError, RenderConfig, render_post_images


FONT_PAIRS = [
    (Path("C:/Windows/Fonts/msjh.ttc"), Path("C:/Windows/Fonts/msjhbd.ttc")),
    (Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"), Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc")),
    (Path.home() / "Library/Fonts/NotoSansCJKtc-Regular.otf", Path.home() / "Library/Fonts/NotoSansCJKtc-Bold.otf"),
    (Path("/Library/Fonts/NotoSansCJKtc-Regular.otf"), Path("/Library/Fonts/NotoSansCJKtc-Bold.otf")),
]
REGULAR_FONT, BOLD_FONT = next((pair for pair in FONT_PAIRS if all(path.is_file() for path in pair)), FONT_PAIRS[0])


@pytest.mark.skipif(not REGULAR_FONT.is_file(), reason="找不到繁中字型")
def test_renderer_outputs_fixed_size_pages_and_keeps_logo_visible() -> None:
    pages = render_post_images(
        "這是一段繁體中文投稿。\n第二行保留手動換行。",
        regular_font_path=REGULAR_FONT,
        bold_font_path=BOLD_FONT,
    )
    assert len(pages) == 1
    with Image.open(BytesIO(pages[0])) as image:
        assert image.size == (1080, 1350)
        assert image.getpixel((1020, 1240)) != image.getpixel((0, 0))


@pytest.mark.skipif(not REGULAR_FONT.is_file(), reason="找不到繁中字型")
def test_renderer_splits_long_text_into_multiple_pages() -> None:
    pages = render_post_images(
        "這是測試文字。" * 500,
        regular_font_path=REGULAR_FONT,
        bold_font_path=BOLD_FONT,
    )
    assert 2 <= len(pages) <= 10


@pytest.mark.skipif(not REGULAR_FONT.is_file(), reason="找不到繁中字型")
def test_renderer_rejects_content_that_cannot_fit() -> None:
    with pytest.raises(ImageLayoutError):
        render_post_images(
            "測試" * 2000,
            regular_font_path=REGULAR_FONT,
            bold_font_path=BOLD_FONT,
            config=RenderConfig(max_pages=1),
        )


@pytest.mark.skipif(not REGULAR_FONT.is_file(), reason="找不到繁中字型")
def test_renderer_rejects_excessive_manual_newlines() -> None:
    with pytest.raises(ImageLayoutError):
        render_post_images("字\n" * 500, regular_font_path=REGULAR_FONT, bold_font_path=BOLD_FONT)
