"""Desktop application entry point."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Optional

from usbdoctor.core.logging import setup_logging
from usbdoctor.version import APP_NAME, __version__

LOG = logging.getLogger(__name__)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="usbdoctor-gui", description=f"{APP_NAME} desktop application")
    parser.add_argument("--theme", choices=("dark", "light"), default="dark")
    parser.add_argument("--state-dir", help="override the application data directory")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    args = parser.parse_args(argv)

    setup_logging()
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QIcon, QPixmap
        from PySide6.QtWidgets import QApplication, QMessageBox
    except ImportError as exc:  # a clear message beats a traceback for end users
        print(
            "USB Doctor AI's desktop interface needs PySide6.\n"
            "Install it with:  pip install PySide6\n"
            f"(import error: {exc})",
            file=sys.stderr,
        )
        return 2

    from usbdoctor.app.main_window import MainWindow
    from usbdoctor.service.engine import DoctorService

    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setOrganizationName("USB Doctor AI")

    service = DoctorService(state_dir=Path(args.state_dir) if args.state_dir else None)
    saved_theme = service.history.get_setting("theme")
    theme = saved_theme if saved_theme in ("dark", "light") else args.theme
    window = MainWindow(service, theme=theme)
    window.show()

    # Non-blocking advisory about privileges — the app still works read-only.
    status = service.status()
    if not status["is_admin"]:
        from PySide6.QtCore import QTimer

        def advise() -> None:
            QMessageBox.information(
                window,
                "Limited privileges",
                "USB Doctor AI is not running with administrator/root rights.\n\n"
                "Read-only analysis works, but raw-device repairs and some SMART queries will be "
                "refused by the operating system.\n\nRestart the app elevated for complete results.",
            )

        QTimer.singleShot(700, advise)
    return app.exec()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
