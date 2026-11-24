"""Screenshot the desktop window without a display.

Design work on this application used to be blind: the window could only be looked
at on a machine with a GUI stack, so a spacing or contrast mistake survived until
somebody ran the installer. Qt can render offscreen, so this renders the real
window — same stylesheet, same pages, same icons — to PNG files that can be opened
anywhere.

    python tools/preview_ui.py                    # every page, both themes
    python tools/preview_ui.py --theme dark --pages dashboard,settings

On Linux the minimal platform needs no X server; on Windows and macOS the default
platform is already offscreen-capable. The window is built against a temporary
state directory, so nothing in your real history is touched.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _register_fonts(app) -> None:
    """Give Qt a usable font on a machine with none (CI, containers).

    Without this the screenshot is a grid of tofu boxes: Qt has no fallback here,
    and the point of the tool is to look at type, not just at boxes.
    """
    from PySide6.QtGui import QFontDatabase

    candidates = [
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"),
    ]
    for font in candidates:
        if font.exists():
            QFontDatabase.addApplicationFont(str(font))
    if not QFontDatabase.families():
        from PySide6.QtGui import QFont

        # A container with no fontconfig database: name the one font we do have so
        # the stylesheet's font-family list has something to land on.
        app.setFont(QFont("DejaVu Sans", 10))


def render(out_dir: Path, *, pages: list[str], themes: list[str], size: tuple[int, int]) -> list[Path]:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from usbdoctor.app.main_window import MainWindow
    from usbdoctor.service.engine import DoctorService

    app = QApplication.instance() or QApplication([])
    _register_fonts(app)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    with tempfile.TemporaryDirectory() as state:
        service = DoctorService(state_dir=Path(state))
        window = MainWindow(service, theme=themes[0])
        window.resize(*size)
        window.show()
        app.processEvents()

        for theme in themes:
            window.apply_theme(theme)
            app.processEvents()
            for page in pages:
                window.show_page(page)
                app.processEvents()
                # Let layout settle: Qt defers polishing until the event loop runs.
                QTimer.singleShot(0, lambda: None)
                app.processEvents()
                target = out_dir / f"{theme}-{page}.png"
                window.grab().save(str(target))
                written.append(target)
                print(f"  {target}")
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(ROOT / "build" / "ui-preview"))
    parser.add_argument("--theme", action="append", choices=("dark", "light"),
                        help="repeatable; default is both")
    parser.add_argument("--pages", default="dashboard,scan,settings,reports",
                        help="comma-separated page keys")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=820)
    args = parser.parse_args(argv)

    written = render(
        Path(args.out),
        pages=[page for page in args.pages.split(",") if page],
        themes=args.theme or ["dark", "light"],
        size=(args.width, args.height),
    )
    print(f"\n{len(written)} screenshots in {Path(args.out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
