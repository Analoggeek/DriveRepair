"""Generate the platform icon files (.ico for Windows, .icns for macOS).

Run on the target platform after installing the packaging extras:

    pip install "usbdoctor-ai[package]"
    python tools/make_icons.py

PNG sizes are produced with Qt (already a desktop dependency) so no extra
imaging library is required; Pillow is used only to assemble the multi-resolution
.ico container, which is the format Windows expects.
"""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "usbdoctor" / "frontend" / "web" / "assets"
SIZES = (16, 32, 48, 64, 128, 256, 512, 1024)
ICO_SIZES = (16, 32, 48, 64, 128, 256)

#: ICNS entry types and the pixel size each expects.
ICNS_TYPES = {
    16: b"icp4", 32: b"icp5", 64: b"icp6",
    128: b"ic07", 256: b"ic08", 512: b"ic09", 1024: b"ic10",
}


def render_pngs() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QPointF, QRectF, Qt
    from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPen, QPixmap
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])  # noqa: F841 - Qt needs the app
    ASSETS.mkdir(parents=True, exist_ok=True)

    for size in SIZES:
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing, True)
        margin = size * 0.055
        rect = QRectF(margin, margin, size - 2 * margin, size - 2 * margin)
        gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
        gradient.setColorAt(0.0, QColor("#4a91ff"))
        gradient.setColorAt(1.0, QColor("#7c5cff"))
        painter.setPen(Qt.NoPen)
        painter.setBrush(gradient)
        painter.drawRoundedRect(rect, size * 0.22, size * 0.22)
        centre = QPointF(size / 2, size / 2)
        pen = QPen(QColor("#ffffff"), max(1.6, size * 0.052))
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        radius = size * 0.175
        painter.drawEllipse(centre, radius, radius)
        painter.drawEllipse(centre, radius * 0.42, radius * 0.42)
        for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):
            painter.drawLine(
                QPointF(centre.x() + dx * radius * 1.22, centre.y() + dy * radius * 1.22),
                QPointF(centre.x() + dx * radius * 1.72, centre.y() + dy * radius * 1.72),
            )
        painter.end()
        pixmap.save(str(ASSETS / f"icon-{size}.png"))
        print(f"png  {size:>5}px")


def build_ico() -> None:
    try:
        from PIL import Image
    except ImportError:
        raise SystemExit("Pillow is required for .ico: pip install Pillow") from None
    images = [Image.open(ASSETS / f"icon-{s}.png").convert("RGBA") for s in ICO_SIZES]
    target = ASSETS / "app.ico"
    images[-1].save(target, format="ICO", sizes=[(s, s) for s in ICO_SIZES])
    print(f"ico  {target}  {target.stat().st_size / 1024:.1f} KiB")


def build_icns() -> None:
    """Assemble an ICNS container directly (no macOS-only tooling required)."""
    chunks = bytearray()
    for size, kind in ICNS_TYPES.items():
        source = ASSETS / f"icon-{size}.png"
        if not source.exists():
            print(f"skip {kind.decode()} (missing {source.name})")
            continue
        payload = source.read_bytes()
        chunks += kind + struct.pack(">I", len(payload) + 8) + payload
    icns = b"icns" + struct.pack(">I", len(chunks) + 8) + bytes(chunks)
    target = ASSETS / "app.icns"
    target.write_bytes(icns)
    print(f"icns {target}  {len(icns) / 1024:.1f} KiB")


if __name__ == "__main__":
    render_pngs()
    build_ico()
    if sys.platform == "darwin":
        build_icns()
    else:
        build_icns()  # the container is portable; macOS accepts it on any build host
