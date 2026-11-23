"""Platform facts and privilege handling for Windows, macOS and Linux."""

from __future__ import annotations

import ctypes
import os
import platform
import shutil
import struct
import sys
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

from usbdoctor.core.errors import PrivilegeError


@dataclass(frozen=True)
class PlatformInfo:
    system: str  # "windows" | "macos" | "linux" | other
    machine: str  # x86_64, arm64, ...
    release: str
    python: str
    is_admin: bool
    supports_raw_io: bool

    @property
    def is_windows(self) -> bool:
        return self.system == "windows"

    @property
    def is_macos(self) -> bool:
        return self.system == "macos"

    @property
    def is_linux(self) -> bool:
        return self.system == "linux"

    def to_dict(self) -> dict:
        return {
            "system": self.system,
            "machine": self.machine,
            "release": self.release,
            "python": self.python,
            "is_admin": self.is_admin,
            "supports_raw_io": self.supports_raw_io,
        }


def detect_system() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    return sys.platform


def is_admin() -> bool:
    """True when the process can open raw disks / issue privileged ioctls."""
    system = detect_system()
    if system == "windows":
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            return False
    if hasattr(os, "geteuid"):
        return os.geteuid() == 0
    return False  # pragma: no cover


def require_admin(operation: str) -> None:
    if not is_admin():
        raise PrivilegeError(
            f"Administrator privileges are required to {operation}.",
            hint=(
                "On Windows run USB Doctor AI as Administrator. "
                "On macOS/Linux start it with sudo."
            ),
            operation=operation,
        )


@lru_cache(maxsize=1)
def platform_info() -> PlatformInfo:
    system = detect_system()
    machine = platform.machine().lower()
    # Probe raw I/O capability by attempting a harmless zero-length seek on a
    # device node where one is guaranteed to exist.
    raw = False
    try:
        if system == "windows":
            raw = True  # \\\\.\\PhysicalDriveN raw reads work for admins
        elif system == "macos":
            raw = os.path.exists("/dev/disk0") and is_admin()
        elif system == "linux":
            raw = os.path.exists("/dev/null") and is_admin()
    except OSError:
        raw = False
    return PlatformInfo(
        system=system,
        machine=machine,
        release=platform.release(),
        python=platform.python_version(),
        is_admin=is_admin(),
        supports_raw_io=raw,
    )


def native_sector_size(fd: int) -> int:
    """Return the device's logical sector size using native APIs.

    Falls back to 512 when the platform call is unavailable.
    """
    system = detect_system()
    try:
        if system == "linux":
            # BLKSSZGET = 0x1268
            import array
            import fcntl

            buf = array.array("i", [0])
            fcntl.ioctl(fd, 0x1268, buf, True)
            size = int(buf[0])
            if size in (512, 1024, 2048, 4096):
                return size
        elif system == "macos":
            # DKIOCGETBLOCKSIZE = 0x40046418 ; DKIOCGETPHYSICALBLOCKSIZE follows.
            import array
            import fcntl

            buf = array.array("I", [0])
            fcntl.ioctl(fd, 0x40046418, buf, True)
            size = int(buf[0])
            if size in (512, 1024, 2048, 4096):
                return size
        elif system == "windows":
            import msvcrt  # noqa: F401  (ensures we are on a real Windows build)

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            bytes_returned = ctypes.c_ulong(0)
            geo = (ctypes.c_ubyte * 24)()
            # IOCTL_DISK_GET_DRIVE_GEOMETRY = 0x00070000
            ok = kernel32.DeviceIoControl(
                msvcrt.get_osfhandle(fd), 0x00070000, None, 0, geo, 24, ctypes.byref(bytes_returned), None
            )
            if ok:
                # struct DISK_GEOMETRY: LARGE_INTEGER(8) MEDIA_TYPE(4) 3x DWORD
                _, _, bytes_per_sector, _, _ = struct.unpack("<qIIII", bytes(geo))
                if bytes_per_sector in (512, 1024, 2048, 4096):
                    return int(bytes_per_sector)
    except (OSError, ValueError, struct.error, ImportError):
        pass
    return 512


def find_tool(*names: str) -> Optional[str]:
    """Find the first available tool among ``names`` (bundled dir first)."""
    for name in names:
        if os.path.isabs(name) and os.access(name, os.X_OK):
            return name
        found = shutil.which(name)
        if found:
            return found
    return None


def ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path
