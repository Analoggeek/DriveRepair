"""Frozen-application launcher used by PyInstaller.

Kept deliberately tiny: it only decides between the desktop GUI and the CLI and
must not import anything heavy before the frozen bundle is ready.
"""

from __future__ import annotations

import multiprocessing
import sys


def main() -> int:
    # Required for one-file builds: prevents a restart loop when a worker
    # process re-imports the frozen entry point.
    multiprocessing.freeze_support()

    if len(sys.argv) > 1 and sys.argv[1] in (
        "devices", "status", "scan", "plan", "repair", "image", "recover",
        "capacity", "report", "research", "history", "serve",
    ):
        from usbdoctor.cli import main as cli_main

        return cli_main(sys.argv[1:])

    from usbdoctor.app.main import main as gui_main

    return gui_main([])


if __name__ == "__main__":
    raise SystemExit(main())
