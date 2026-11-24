"""Vector icons for the desktop shell.

The shell used to draw its navigation from Unicode glyphs (``◉ ▤ ◎ ✚ ⟲ ⚡``).
Those are whatever font the machine happens to have — a different shape on
Windows, macOS and Linux, thin or missing on a system with a cut-down font, and
never aligned to the same optical width as the text beside them. They also cannot
be tinted, so a checked row and an idle row looked identical apart from colour.

The geometry lives in :mod:`usbdoctor.app.icon_geometry`, free of any toolkit, and
this module paints it with QPainter. ``tools/preview_icons.py`` rasterises the
same geometry with Pillow, so the set can be inspected without a display.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import (
    QColor,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)

from usbdoctor.app import icon_geometry as geometry

__all__ = ["Style", "draw", "icon", "pixmap", "names", "app_icon"]


@dataclass(frozen=True)
class Style:
    """How a glyph is drawn: one colour, or a gradient that suggests depth."""

    colour: str = "#c9d8ee"
    accent: Optional[str] = None
    width: float = 1.9

    def pen(self, painter: QPainter, size: float) -> QPen:
        pen = QPen()
        pen.setWidthF(geometry.stroke_width(size, self.width))
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        if self.accent:
            # Light falls from the upper left, as it does on the app tile.
            gradient = QLinearGradient(QPointF(0, 0), QPointF(size, size))
            gradient.setColorAt(0.0, QColor(self.accent).lighter(135))
            gradient.setColorAt(1.0, QColor(self.colour))
            pen.setBrush(gradient)
        else:
            pen.setColor(QColor(self.colour))
        return pen


def _painter_path(segments) -> QPainterPath:
    path = QPainterPath()
    for segment in segments:
        kind = segment[0]
        if kind == "m":
            path.moveTo(segment[1], segment[2])
        elif kind == "l":
            path.lineTo(segment[1], segment[2])
        elif kind == "q":
            path.quadTo(QPointF(segment[1], segment[2]), QPointF(segment[3], segment[4]))
        elif kind == "c":
            path.cubicTo(
                QPointF(segment[1], segment[2]),
                QPointF(segment[3], segment[4]),
                QPointF(segment[5], segment[6]),
            )
        elif kind == "arc":
            box = QRectF(segment[1], segment[2], segment[3], segment[4])
            path.arcMoveTo(box, segment[5])
            path.arcTo(box, segment[5], segment[6])
        elif kind == "close":
            path.closeSubpath()
    return path


def draw(painter: QPainter, name: str, rect: QRectF, style: Optional[Style] = None) -> None:
    """Draw one glyph inside ``rect``."""
    style = style or Style()
    size = min(rect.width(), rect.height())
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.translate(rect.x() + (rect.width() - size) / 2,
                      rect.y() + (rect.height() - size) / 2)
    painter.setPen(style.pen(painter, size))
    painter.setBrush(Qt.NoBrush)
    for primitive in geometry.scaled(name, size):
        kind = primitive[0]
        if kind == "rect":
            x, y, w, h, radius = primitive[1:]
            painter.drawRoundedRect(QRectF(x, y, w, h), radius, radius)
        elif kind == "ellipse":
            cx, cy, rx, ry = primitive[1:]
            painter.drawEllipse(QPointF(cx, cy), rx, ry)
        elif kind == "disc":
            painter.save()
            painter.setBrush(painter.pen().brush())
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(QPointF(primitive[1], primitive[2]),
                                primitive[3], primitive[3])
            painter.restore()
        elif kind == "line":
            painter.drawLine(QPointF(primitive[1], primitive[2]),
                             QPointF(primitive[3], primitive[4]))
        elif kind == "point":
            painter.drawPoint(QPointF(primitive[1], primitive[2]))
        elif kind == "path":
            painter.drawPath(_painter_path(primitive[1]))
    painter.restore()


def pixmap(name: str, size: int = 24, style: Optional[Style] = None,
           device_ratio: float = 1.0) -> QPixmap:
    """Render a glyph to a transparent pixmap at ``size`` logical pixels."""
    physical = max(1, int(round(size * device_ratio)))
    canvas = QPixmap(physical, physical)
    canvas.fill(Qt.transparent)
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.Antialiasing, True)
    draw(painter, name, QRectF(0, 0, physical, physical), style)
    painter.end()
    canvas.setDevicePixelRatio(device_ratio)
    return canvas


def icon(name: str, size: int = 20, style: Optional[Style] = None) -> QIcon:
    """A QIcon carrying a 2x and 3x pixmap too, so it stays sharp when scaled."""
    result = QIcon()
    for ratio in (1.0, 2.0, 3.0):
        result.addPixmap(pixmap(name, size, style, ratio))
    return result


def names() -> tuple[str, ...]:
    return geometry.names()


def app_icon() -> QIcon:
    """The application tile, from the generated artwork in the web assets.

    The same directory ships inside the bundle (PyInstaller copies it to
    ``usbdoctor/frontend/web``), so the lookup is identical frozen and from source.
    """
    root = Path(__file__).resolve().parents[1] / "frontend" / "web" / "assets"
    result = QIcon()
    for size in (16, 32, 48, 64, 128, 256, 512):
        candidate = root / f"icon-{size}.png"
        if candidate.exists():
            result.addFile(str(candidate), QSize(size, size))
    for container in ("app.ico", "app.icns"):
        candidate = root / container
        if candidate.exists():
            result.addFile(str(candidate))
    return result
