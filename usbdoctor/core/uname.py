"""Version-compatible ``os.uname`` shim.

``os.uname`` does not exist on Windows, so every platform check must go through
this helper instead of calling ``os.uname()`` directly (a real crash we hit in
early builds on Windows 11).
"""

from __future__ import annotations

import os
import platform
import sys


def uname_sysname() -> str:
    if hasattr(os, "uname"):
        return os.uname().sysname
    return {
        "win32": "Windows",
        "darwin": "Darwin",
    }.get(sys.platform, platform.system() or "Unknown")


def uname_release() -> str:
    if hasattr(os, "uname"):
        return os.uname().release
    return platform.release()


def uname_machine() -> str:
    if hasattr(os, "uname"):
        return os.uname().machine
    return platform.machine()
