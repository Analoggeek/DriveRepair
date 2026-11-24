"""Icon geometry, free of any toolkit.

The shell's icons are described once, here, as a short list of drawing
primitives on a 24 x 24 grid. Two backends consume that description:
``usbdoctor.app.icons`` paints it with QPainter for the running application, and
``tools/preview_icons.py`` rasterises it with Pillow so the set can be inspected
(and tested) on a machine with no display and no Qt. A picture described once and
rendered twice never drifts.

Primitives are plain tuples so they stay cheap to create and trivial to assert on:

    ("rect",    x, y, w, h, radius)
    ("ellipse", cx, cy, rx, ry)
    ("disc",    cx, cy, r)          # filled: knobs, LEDs
    ("line",    x1, y1, x2, y2)
    ("point",   x, y)
    ("path",    [("m", x, y), ("l", x, y), ("q", cx, cy, x, y),
                 ("c", c1x, c1y, c2x, c2y, x, y),
                 ("arc", x, y, w, h, start_deg, span_deg), ("close",)])
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Sequence, Tuple

#: The drawing grid every glyph is authored on.
GRID = 24.0

#: Rendered at this fraction, a glyph sits comfortably inside its tile.
GLYPH_BOX = 0.84

Point = Tuple[float, float]
Segment = Tuple[Any, ...]        # ("m", x, y) and friends, lengths vary by command
Path = List[Segment]
Primitive = Tuple[Any, ...]      # the first item names the kind; the rest are numbers
Glyph = List[Primitive]


def _arc_points(x: float, y: float, w: float, h: float,
                start: float, span: float, steps: int = 48) -> List[Point]:
    """Sample a Qt-style arc: angles in degrees, counter-clockwise, 0 = 3 o'clock."""
    cx, cy, rx, ry = x + w / 2.0, y + h / 2.0, w / 2.0, h / 2.0
    points = []
    for index in range(steps + 1):
        angle = math.radians(start + span * index / steps)
        points.append((cx + rx * math.cos(angle), cy - ry * math.sin(angle)))
    return points


def _quad(p0: Point, control: Point, p1: Point, steps: int = 24) -> List[Point]:
    out = []
    for index in range(1, steps + 1):
        t = index / steps
        mt = 1.0 - t
        out.append((
            mt * mt * p0[0] + 2 * mt * t * control[0] + t * t * p1[0],
            mt * mt * p0[1] + 2 * mt * t * control[1] + t * t * p1[1],
        ))
    return out


def _cubic(p0: Point, c1: Point, c2: Point, p1: Point, steps: int = 32) -> List[Point]:
    out = []
    for index in range(1, steps + 1):
        t = index / steps
        mt = 1.0 - t
        out.append((
            mt ** 3 * p0[0] + 3 * mt * mt * t * c1[0] + 3 * mt * t * t * c2[0] + t ** 3 * p1[0],
            mt ** 3 * p0[1] + 3 * mt * mt * t * c1[1] + 3 * mt * t * t * c2[1] + t ** 3 * p1[1],
        ))
    return out


def flatten_path(segments: Sequence[Segment]) -> Tuple[List[Point], bool]:
    """Turn a path into polylines for a raster backend. Returns (points, closed).

    One glyph may consist of several subpaths; the caller draws them in order.
    Curves are sampled finely enough that a 96 px render shows no faceting.
    """
    points: List[Point] = []
    current: Point = (0.0, 0.0)
    closed = False
    for segment in segments:
        kind = segment[0]
        if kind == "m":
            current = (float(segment[1]), float(segment[2]))
            points.append(current)
        elif kind == "l":
            current = (float(segment[1]), float(segment[2]))
            points.append(current)
        elif kind == "q":
            control = (float(segment[1]), float(segment[2]))
            end = (float(segment[3]), float(segment[4]))
            points.extend(_quad(current, control, end))
            current = end
        elif kind == "c":
            c1 = (float(segment[1]), float(segment[2]))
            c2 = (float(segment[3]), float(segment[4]))
            end = (float(segment[5]), float(segment[6]))
            points.extend(_cubic(current, c1, c2, end))
            current = end
        elif kind == "arc":
            sampled = _arc_points(float(segment[1]), float(segment[2]), float(segment[3]),
                                  float(segment[4]), float(segment[5]), float(segment[6]))
            points.extend(sampled)
            current = sampled[-1]
        elif kind == "close":
            closed = True
        else:  # pragma: no cover - a programming error, never user input
            raise ValueError(f"unknown path segment {kind!r}")
    return points, closed


def arc_outline(cx: float, cy: float, rx: float, ry: float) -> List[Point]:
    return _arc_points(cx - rx, cy - ry, rx * 2, ry * 2, 0.0, 360.0)


def rounded_rect_outline(x: float, y: float, w: float, h: float, r: float,
                         steps: int = 10) -> List[Point]:
    """Corner radius honoured, as a closed polyline."""
    r = min(r, w / 2.0, h / 2.0)
    points: List[Point] = []
    corners = (
        (x + w - r, y + r, -90.0, 90.0),      # top-right
        (x + w - r, y + h - r, 0.0, 90.0),    # bottom-right
        (x + r, y + h - r, 90.0, 90.0),       # bottom-left
        (x + r, y + r, 180.0, 90.0),          # top-left
    )
    for cx, cy, start, span in corners:
        points.extend(_arc_points(cx - r, cy - r, r * 2, r * 2, start, span, steps))
    return points


# --------------------------------------------------------------------- glyphs
def dashboard() -> Glyph:
    return [("rect", 3.2, 3.2, 7.4, 7.4, 2.0),
            ("rect", 13.4, 3.2, 7.4, 4.4, 1.8),
            ("rect", 13.4, 10.0, 7.4, 10.8, 2.0),
            ("rect", 3.2, 13.0, 7.4, 7.8, 2.0)]


def devices() -> Glyph:
    """A USB stick seen from the front: connector, body, seam."""
    return [("rect", 8.0, 8.8, 8.0, 11.2, 1.5),
            ("rect", 10.0, 4.0, 4.0, 4.6, 0.6),
            ("line", 11.2, 5.4, 11.2, 7.4),
            ("line", 12.8, 5.4, 12.8, 7.4),
            ("line", 9.6, 13.0, 14.4, 13.0)]


def scan() -> Glyph:
    return [("ellipse", 12.0, 12.0, 7.4, 7.4),
            ("ellipse", 12.0, 12.0, 3.3, 3.3),
            ("path", [("m", 12.0, 12.0), ("l", 17.6, 6.4)]),
            ("path", [("arc", 5.0, 5.0, 14.0, 14.0, 35.0, 34.0)]),
            ("path", [("arc", 5.0, 5.0, 14.0, 14.0, 215.0, 34.0)])]


def repair() -> Glyph:
    """A hex nut with a spark: hardware work, not a magnifying glass.

    A jaw ring plus handle read as a search icon at sidebar size, which is exactly
    the wrong thing for a button that can write to a disk.
    """
    segments: List[Segment] = []
    for index in range(6):
        angle = math.radians(-90 + index * 60)
        point = (11.2 + math.cos(angle) * 7.6, 12.6 + math.sin(angle) * 7.6)
        segments.append(("m" if index == 0 else "l",) + point)
    segments.append(("close",))
    return [("path", segments),
            ("ellipse", 11.2, 12.6, 3.2, 3.2),
            ("line", 19.2, 3.0, 19.2, 6.0),
            ("line", 17.7, 4.5, 20.7, 4.5)]


def recovery() -> Glyph:
    return [("path", [("arc", 4.6, 4.6, 14.8, 14.8, 40.0, 280.0)]),
            ("path", [("m", 15.2, 2.6), ("l", 20.4, 5.4), ("l", 15.0, 8.0), ("close",)])]


def performance() -> Glyph:
    return [("path", [("m", 13.4, 2.8), ("l", 5.6, 13.4), ("l", 11.0, 13.4),
                      ("l", 9.8, 21.2), ("l", 18.0, 10.2), ("l", 12.4, 10.2),
                      ("close",)])]


def _sparkle(cx: float, cy: float, r: float) -> Primitive:
    return ("path", [("m", cx, cy - r),
                     ("q", cx + r * 0.22, cy - r * 0.22, cx + r, cy),
                     ("q", cx + r * 0.22, cy + r * 0.22, cx, cy + r),
                     ("q", cx - r * 0.22, cy + r * 0.22, cx - r, cy),
                     ("q", cx - r * 0.22, cy - r * 0.22, cx, cy - r),
                     ("close",)])


def assistant() -> Glyph:
    return [_sparkle(9.9, 13.0, 6.3), _sparkle(18.3, 5.7, 2.9)]


def history() -> Glyph:
    return [("ellipse", 12.0, 12.4, 7.8, 7.8),
            ("path", [("m", 12.0, 8.0), ("l", 12.0, 12.6), ("l", 15.4, 14.6)])]


def reports() -> Glyph:
    return [("path", [("m", 5.4, 3.2), ("l", 14.0, 3.2), ("l", 18.6, 7.8),
                      ("l", 18.6, 20.8), ("l", 5.4, 20.8), ("close",)]),
            ("path", [("m", 14.0, 3.2), ("l", 14.0, 7.8), ("l", 18.6, 7.8)]),
            ("line", 8.4, 11.4, 15.6, 11.4),
            ("line", 8.4, 14.4, 15.6, 14.4),
            ("line", 8.4, 17.4, 15.6, 17.4)]


def settings() -> Glyph:
    """A cog: thick teeth around a ring.

    Sliders were too delicate at 20 px in the sidebar; the theme toggle is a
    crescent now, so a cog can no longer be mistaken for it.
    """
    primitives: Glyph = [("ellipse", 12.0, 12.0, 5.6, 5.6)]
    for index in range(8):
        angle = math.radians(index * 45.0)
        primitives.append(("line",
                           12 + math.cos(angle) * 6.6, 12 + math.sin(angle) * 6.6,
                           12 + math.cos(angle) * 9.2, 12 + math.sin(angle) * 9.2))
    primitives.append(("ellipse", 12.0, 12.0, 2.2, 2.2))
    return primitives


def update() -> Glyph:
    return [("path", [("m", 12.0, 3.4), ("l", 12.0, 14.2)]),
            ("path", [("m", 7.8, 10.2), ("l", 12.0, 14.4), ("l", 16.2, 10.2)]),
            ("path", [("m", 4.4, 16.4), ("l", 4.4, 19.4), ("l", 19.6, 19.4), ("l", 19.6, 16.4)])]


def theme() -> Glyph:
    """A crescent moon: the toggle goes to the other theme."""
    return [("path", [("m", 12.0, 3.6),
                      ("arc", 3.6, 3.6, 16.8, 16.8, 90.0, 180.0),
                      ("q", 17.8, 12.0, 12.0, 3.6)])]


def shield() -> Glyph:
    return [("path", [("m", 12.0, 3.0), ("l", 19.4, 6.0), ("l", 19.4, 12.2),
                      ("q", 19.4, 18.2, 12.0, 21.2), ("q", 4.6, 18.2, 4.6, 12.2),
                      ("l", 4.6, 6.0), ("close",)]),
            ("path", [("m", 8.6, 12.0), ("l", 11.2, 14.8), ("l", 15.8, 9.4)])]


def warning() -> Glyph:
    return [("path", [("m", 12.0, 3.4), ("l", 21.2, 19.6), ("l", 2.8, 19.6), ("close",)]),
            ("line", 12.0, 9.2, 12.0, 14.2),
            ("point", 12.0, 16.9)]


def check() -> Glyph:
    return [("path", [("m", 4.6, 12.6), ("l", 9.8, 18.0), ("l", 19.4, 6.4)])]


def arrow_right() -> Glyph:
    return [("path", [("m", 5.0, 12.0), ("l", 19.0, 12.0)]),
            ("path", [("m", 13.4, 6.4), ("l", 19.0, 12.0), ("l", 13.4, 17.6)])]


GLYPHS: Dict[str, Glyph] = {
    "dashboard": dashboard(),
    "devices": devices(),
    "scan": scan(),
    "repair": repair(),
    "recovery": recovery(),
    "performance": performance(),
    "assistant": assistant(),
    "history": history(),
    "reports": reports(),
    "settings": settings(),
    "update": update(),
    "theme": theme(),
    "shield": shield(),
    "warning": warning(),
    "check": check(),
    "arrow": arrow_right(),
}


def names() -> Tuple[str, ...]:
    return tuple(sorted(GLYPHS))


def glyph(name: str) -> Glyph:
    try:
        return GLYPHS[name]
    except KeyError:
        raise KeyError(f"unknown icon {name!r}; known: {', '.join(names())}") from None


def box(size: float) -> Tuple[float, float, float]:
    """Where a glyph sits inside a square tile: (origin, side, scale)."""
    side = size * GLYPH_BOX
    origin = (size - side) / 2.0
    return origin, side, side / GRID


def scaled(name: str, size: float) -> Glyph:
    """The glyph's primitives mapped onto a square tile of ``size`` pixels."""
    origin, _side, factor = box(size)
    out: Glyph = []
    for primitive in glyph(name):
        kind = primitive[0]
        if kind == "path":
            out.append(("path", [_scale_segment(segment, origin, factor)
                                 for segment in primitive[1]]))
            continue
        numbers = [float(value) for value in primitive[1:]]
        out.append(tuple([kind] + [origin + value * factor for value in numbers]))
    return out


def _scale_segment(segment: Sequence[Any], origin: float, factor: float) -> Segment:
    kind = segment[0]
    if kind == "close":
        return ("close",)
    values = [float(value) for value in segment[1:]]
    scaled = [origin + value * factor for value in values]
    # ("arc", x, y, w, h, start, span) scales its box but not its angles.
    if kind == "arc":
        return ("arc", *scaled[:4], values[4], values[5])
    return (kind, *scaled)


def polylines(name: str, size: float) -> List[List[Point]]:
    """Every stroke of a glyph as one or more point lists, for raster backends."""
    out: List[List[Point]] = []
    for primitive in scaled(name, size):
        kind = primitive[0]
        if kind == "rect":
            x, y, w, h, r = (float(value) for value in primitive[1:])
            out.append(rounded_rect_outline(x, y, w, h, r))
        elif kind == "disc":
            cx, cy, radius = (float(value) for value in primitive[1:])
            out.append(arc_outline(cx, cy, radius, radius))
        elif kind == "ellipse":
            cx, cy, rx, ry = (float(value) for value in primitive[1:])
            out.append(arc_outline(cx, cy, rx, ry))
        elif kind == "line":
            x1, y1, x2, y2 = (float(value) for value in primitive[1:])
            out.append([(x1, y1), (x2, y2)])
        elif kind == "point":
            x, y = (float(value) for value in primitive[1:])
            out.append([(x, y), (x + 0.01, y)])
        elif kind == "path":
            points, closed = flatten_path(primitive[1])
            if closed and points:
                points = points + [points[0]]
            out.append(points)
    return out


def stroke_width(size: float, width: float = 1.9) -> float:
    _origin, _side, factor = box(size)
    return max(1.0, width * factor)


def _svg_path_data(segments: Sequence[Segment], factor: float = 1.0) -> str:
    """Path data in SVG's dialect: same commands, y axis flipped by the caller."""
    parts: List[str] = []
    for segment in segments:
        kind = segment[0]
        numbers = [float(value) * factor for value in segment[1:]]
        if kind == "m":
            parts.append(f"M{numbers[0]:.2f} {numbers[1]:.2f}")
        elif kind == "l":
            parts.append(f"L{numbers[0]:.2f} {numbers[1]:.2f}")
        elif kind == "q":
            parts.append(f"Q{numbers[0]:.2f} {numbers[1]:.2f} {numbers[2]:.2f} {numbers[3]:.2f}")
        elif kind == "c":
            parts.append("C" + " ".join(f"{value:.2f}" for value in numbers))
        elif kind == "arc":
            parts.append(_svg_arc(numbers))
        elif kind == "close":
            parts.append("Z")
    return " ".join(parts)


def _svg_arc(numbers: Sequence[float]) -> str:
    """Qt's arc (box, start, span) as an SVG elliptical arc, sampled along the way."""
    x, y, w, h, start, span = numbers
    points = _arc_points(x, y, w, h, start, span, steps=6)
    head = f"A{w / 2:.2f} {h / 2:.2f} 0 0 {0 if span >= 0 else 1} {points[1][0]:.2f} {points[1][1]:.2f}"
    tail = "".join(f" L{px:.2f} {py:.2f}" for px, py in points[2:])
    return head + tail


def to_svg(name: str, size: float = 24.0, *, colour: str = "currentColor",
           stroke: float = 1.9) -> str:
    """An inline SVG of one glyph, sized in pixels and tinted with ``currentColor``.

    The web interface uses these so its navigation shows the same shapes as the
    desktop shell, from the same file, instead of a second set of emoji.
    """
    origin, _side, factor = box(size)
    width = stroke * factor
    body: List[str] = []
    for primitive in scaled(name, size):
        kind = primitive[0]
        if kind == "rect":
            x, y, w, h, r = primitive[1:]
            body.append(
                f'<rect x="{x:.2f}" y="{y:.2f}" width="{w:.2f}" height="{h:.2f}" '
                f'rx="{r:.2f}" ry="{r:.2f}"/>'
            )
        elif kind == "ellipse":
            cx, cy, rx, ry = primitive[1:]
            body.append(f'<ellipse cx="{cx:.2f}" cy="{cy:.2f}" rx="{rx:.2f}" ry="{ry:.2f}"/>')
        elif kind == "disc":
            cx, cy, radius = primitive[1:]
            body.append(f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{radius:.2f}" '
                        f'fill="{colour}" stroke="none"/>')
        elif kind == "line":
            x1, y1, x2, y2 = primitive[1:]
            body.append(f'<line x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" y2="{y2:.2f}"/>')
        elif kind == "point":
            x, y = primitive[1:]
            body.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="{width / 2:.2f}" '
                        f'fill="{colour}" stroke="none"/>')
        elif kind == "path":
            body.append(f'<path d="{_svg_path_data(primitive[1])}"/>')
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size:.0f} {size:.0f}" '
        f'width="{size:.0f}" height="{size:.0f}" fill="none" stroke="{colour}" '
        f'stroke-width="{width:.2f}" stroke-linecap="round" stroke-linejoin="round" '
        f'aria-hidden="true" focusable="false">{"".join(body)}</svg>'
    )
