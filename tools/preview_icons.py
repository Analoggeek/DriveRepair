"""Render the icon set to a PNG contact sheet, with no display and no Qt.

The icons are drawn by ``usbdoctor.app.icons`` through QPainter at run time, which
makes them impossible to look at on a build machine without a GUI stack. This
script rasterises the *same geometry* (``usbdoctor.app.icon_geometry``) with
Pillow, so the set can be reviewed, and a regression in a path is visible instead
of only a pixel count.

    python tools/preview_icons.py                     # every glyph, dark + light
    python tools/preview_icons.py --out sheet.png --size 96

Pillow is only a development convenience: ``pip install Pillow``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from usbdoctor.app import icon_geometry as geometry  # noqa: E402

THEMES = {
    "dark": {"bg": "#0b1017", "panel": "#141c27", "colour": "#dce8f8", "accent": "#3d8bff"},
    "light": {"bg": "#f4f6fb", "panel": "#ffffff", "colour": "#243247", "accent": "#2f6fe0"},
}


def _stroke(image, points, colour, width):
    from PIL import ImageDraw

    draw = ImageDraw.Draw(image)
    if len(points) >= 2:
        draw.line([(float(x), float(y)) for x, y in points], fill=colour, width=int(round(width)),
                  joint="curve")
    # Round caps: Pillow's line has square ends, which reads as a cut glyph.
    radius = width / 2.0
    for x, y in (points[0], points[-1]):
        draw.ellipse([x - radius, y - radius, x + radius, y + radius], fill=colour)


def render_sheet(out: Path, size: int = 84, theme: str = "dark", pad: int = 26) -> Path:
    from PIL import Image

    palette = THEMES[theme]
    names = geometry.names()
    columns = 4
    rows_per_theme = (len(names) + columns - 1) // columns
    cell = size + pad
    width = columns * cell
    height = rows_per_theme * cell * len(THEMES)

    sheet = Image.new("RGBA", (width, height), palette["bg"])
    from PIL import ImageDraw, ImageFont

    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 11)
    except OSError:  # pragma: no cover - font availability differs per machine
        font = ImageFont.load_default()

    for theme_index, theme_name in enumerate(THEMES):
        colours = THEMES[theme_name]
        band = sheet.crop((0, theme_index * rows_per_theme * cell,
                           width, (theme_index + 1) * rows_per_theme * cell))
        band.paste(colours["bg"], (0, 0, width, band.height))
        draw = ImageDraw.Draw(band)
        for index, name in enumerate(names):
            column, row = index % columns, index // columns
            origin_x, origin_y = column * cell, row * cell
            draw.rounded_rectangle(
                [origin_x + 4, origin_y + 4, origin_x + cell - 4, origin_y + cell - 20],
                radius=10, fill=colours["panel"],
            )
            glyph = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            width_px = geometry.stroke_width(size)
            for points in geometry.polylines(name, size):
                _stroke(glyph, points, colours["colour"], width_px)
            for primitive in geometry.scaled(name, size):
                if primitive[0] == "disc":
                    from PIL import ImageDraw
                    cx, cy, radius = primitive[1], primitive[2], primitive[3]
                    ImageDraw.Draw(glyph).ellipse(
                        [cx - radius, cy - radius, cx + radius, cy + radius],
                        fill=colours["colour"],
                    )
            band.alpha_composite(glyph, (origin_x + pad // 2, origin_y + 8))
            draw.text((origin_x + 10, origin_y + cell - 17), name, font=font,
                      fill="#7f8ea6" if theme_name == "dark" else "#5b6b83")
        sheet.paste(band, (0, theme_index * rows_per_theme * cell))

    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, format="PNG")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(ROOT / "build" / "icons-preview.png"))
    parser.add_argument("--size", type=int, default=84, help="glyph size in pixels")
    args = parser.parse_args(argv)

    target = render_sheet(Path(args.out), size=args.size)
    print(f"{target}  ({len(geometry.names())} glyphs x {len(THEMES)} themes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
