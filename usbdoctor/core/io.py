"""Raw block-device I/O for Windows, macOS and Linux.

All reads are sector-aligned underneath (every OS requires it for raw disks) and
sliced for the caller, so upper layers can ask for arbitrary offset/length.
Writes go through :class:`GuardedWriter`, which enforces the write policy in
``usbdoctor.security.writer_guard``.
"""

from __future__ import annotations

import errno
import hashlib
import os
import struct
import sys
from dataclasses import dataclass
from typing import BinaryIO, Optional, Union

from usbdoctor.core.errors import (
    DeviceNotFoundError,
    PlatformUnsupportedError,
    UsbDoctorError,
)
from usbdoctor.core.platform import detect_system

PathLike = Union[str, "os.PathLike[str]"]

# Windows constants
_WIN_GENERIC_READ = 0x80000000
_WIN_GENERIC_WRITE = 0x40000000
_WIN_FILE_SHARE_READ = 0x00000001
_WIN_FILE_SHARE_WRITE = 0x00000002
_WIN_OPEN_EXISTING = 3
_WIN_INVALID_HANDLE = -1


def normalize_device_path(path: str) -> str:
    """Return the OS-canonical form of a device path."""
    system = detect_system()
    if system == "windows":
        if path.upper().startswith("\\\\.\\"):
            return path
        # Accept "PhysicalDrive2", "2", "2:" style inputs from expert users.
        if path.isdigit():
            return f"\\\\.\\PhysicalDrive{path}"
        return path
    if system == "macos":
        if path.startswith("/dev/rdisk") or path == "/dev/disk" or path.startswith("/dev/disk"):
            return path
        return path
    return path


def _win_open(path: str, write: bool) -> int:
    """CreateFileW wrapper returning a C runtime file descriptor."""
    import ctypes
    import msvcrt

    access = _WIN_GENERIC_READ | (_WIN_GENERIC_WRITE if write else 0)
    share = _WIN_FILE_SHARE_READ | _WIN_FILE_SHARE_WRITE
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.CreateFileW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_void_p,
    ]
    handle = kernel32.CreateFileW(path, access, share, None, _WIN_OPEN_EXISTING, 0, None)
    if handle in (None, _WIN_INVALID_HANDLE, ctypes.c_void_p(-1).value):
        err = ctypes.get_last_error() if hasattr(ctypes, "get_last_error") else kernel32.GetLastError()
        if err == 5:
            from usbdoctor.core.errors import PrivilegeError

            raise PrivilegeError(
                "Access denied opening the raw device.",
                hint="Run USB Doctor AI as Administrator.",
                path=path,
            )
        raise DeviceNotFoundError(f"Cannot open {path} (Windows error {err})", path=path)
    try:
        return msvcrt.open_osfhandle(handle, os.O_RDONLY if not write else os.O_RDWR)  # type: ignore[attr-defined]
    except OSError:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        raise


class DiskReader:
    """Sector-aligned, seekable reader over a raw device or image file."""

    def __init__(self, path: PathLike, *, sector_size: int = 512, buffered: bool = True) -> None:
        self.path = os.fspath(path)
        self.sector_size = sector_size
        self._buffered = buffered
        self._fd: Optional[int] = None
        self._fh: Optional[BinaryIO] = None
        self._cached_start = -1
        self._cached_data = b""

    # ------------------------------------------------------------- lifecycle
    def __enter__(self) -> "DiskReader":
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def open(self) -> None:
        if self._fd is not None or self._fh is not None:
            return
        system = detect_system()
        path = normalize_device_path(self.path)
        if system == "windows":
            self._fd = _win_open(path, write=False)
        else:
            if not os.path.exists(path):
                raise DeviceNotFoundError(f"Device {path} is not present", path=path)
            flags = os.O_RDONLY
            if hasattr(os, "O_BINARY"):
                flags |= os.O_BINARY  # type: ignore[attr-defined]
            self._fd = os.open(path, flags)

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            finally:
                self._fh = None
        if self._fd is not None:
            try:
                os.close(self._fd)
            finally:
                self._fd = None

    @property
    def closed(self) -> bool:
        return self._fd is None and self._fh is None

    # ------------------------------------------------------------------ size
    def size(self) -> int:
        self.open()
        assert self._fd is not None
        try:
            return os.lseek(self._fd, 0, os.SEEK_END)
        except OSError:
            if detect_system() == "linux":
                # Fall back to sysfs for character/block nodes that reject lseek.
                return _linux_block_size(self.path) or 0
            raise

    # ------------------------------------------------------------------ read
    def _read_raw(self, offset: int, size: int) -> bytes:
        self.open()
        assert self._fd is not None
        try:
            os.lseek(self._fd, offset, os.SEEK_SET)
        except OSError as exc:
            raise UsbDoctorError(
                f"Cannot seek to byte {offset} on {self.path}: {exc.strerror}",
                path=self.path,
            ) from exc
        chunks: list[bytes] = []
        remaining = size
        while remaining > 0:
            try:
                chunk = os.read(self._fd, remaining)
            except OSError as exc:
                if exc.errno in (errno.EIO, errno.ENXIO, errno.ESPIPE):
                    raise MediaReadError(self.path, offset, exc.errno or 0) from exc
                raise
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    #: Refuse absurdly large single reads: metadata parsers must never be able
    #: to request more than this, and a corrupt on-disk value should fail
    #: cleanly rather than raise OverflowError from the kernel call.
    MAX_READ = 64 << 20

    def read(self, offset: int, size: int) -> bytes:
        """Read ``size`` bytes at ``offset``; short reads mean end of media."""
        if size <= 0:
            return b""
        if offset < 0:
            raise ValueError("offset must be >= 0")
        if size > self.MAX_READ:
            raise ValueError(
                f"refusing oversized read of {size:,} bytes at offset {offset:,} "
                f"(max {self.MAX_READ:,}) — on-disk metadata is probably corrupt"
            )
        sector = self.sector_size
        start = (offset // sector) * sector
        end = offset + size
        end_aligned = ((end + sector - 1) // sector) * sector
        if start == self._cached_start and end_aligned <= self._cached_start + len(self._cached_data):
            base = offset - self._cached_start
            return self._cached_data[base : base + size]
        data = self._read_raw(start, end_aligned - start)
        # Keep a small cache: most metadata reads are adjacent.
        if len(data) <= 1 << 20:
            self._cached_start = start
            self._cached_data = data
        base = offset - start
        return data[base : base + size]

    def read_all_limited(self, offset: int, size: int, *, limit: int = 4 << 20) -> bytes:
        return self.read(offset, min(size, limit))

    def hash_range(self, offset: int, length: int, *, chunk: int = 1 << 20) -> str:
        digest = hashlib.sha256()
        pos = offset
        end = offset + length
        while pos < end:
            data = self.read(pos, min(chunk, end - pos))
            if not data:
                break
            digest.update(data)
            pos += len(data)
        return digest.hexdigest()


class MediaReadError(UsbDoctorError):
    """A hardware read error occurred (bad sector, failing controller, ...)."""

    code = "MEDIA_READ_ERROR"

    def __init__(self, path: str, offset: int, errno_value: int) -> None:
        super().__init__(
            f"The device returned a read error at byte offset {offset:,}.",
            hint="This usually indicates a bad sector or failing flash/controller.",
            path=path,
            offset=offset,
            errno=errno_value,
        )
        self.offset = offset
        self.errno_value = errno_value


def _linux_block_size(path: str) -> Optional[int]:
    name = os.path.basename(os.path.realpath(path))
    candidate = f"/sys/class/block/{name}/size"
    try:
        with open(candidate, "r", encoding="ascii") as fh:
            sectors = int(fh.read().strip())
        return sectors * 512
    except (OSError, ValueError):
        return None


def open_reader(path: PathLike, *, sector_size: int = 512) -> DiskReader:
    reader = DiskReader(path, sector_size=sector_size)
    reader.open()
    return reader


def probe_size(path: PathLike) -> Optional[int]:
    try:
        with DiskReader(path) as reader:
            return reader.size()
    except (OSError, UsbDoctorError):
        return None


# ---------------------------------------------------------------------------
# Disk image helpers (real, used by recovery and automated tests)
# ---------------------------------------------------------------------------


def create_sparse_image(path: PathLike, size_bytes: int) -> None:
    """Create a sparse image file of exactly ``size_bytes`` (no RAM consumed)."""
    if size_bytes <= 0:
        raise ValueError("image size must be positive")
    os.makedirs(os.path.dirname(os.fspath(path)) or ".", exist_ok=True)
    with open(path, "wb") as fh:
        fh.truncate(size_bytes)


class ImageWriter:
    """Writes to an image file (used by imaging, never by repair-by-default)."""

    def __init__(self, path: PathLike, *, truncate: bool = True) -> None:
        self.path = os.fspath(path)
        self._fh = open(self.path, "wb" if truncate else "r+b")

    def write(self, offset: int, data: bytes) -> None:
        self._fh.seek(offset)
        self._fh.write(data)

    def close(self) -> None:
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._fh.close()

    def __enter__(self) -> "ImageWriter":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def read_mbr_disk_signature(head: bytes) -> Optional[str]:
    """Extract the 4-byte MBR disk signature (offset 0x1B8) when present."""
    if len(head) < 512 or head[510:512] != b"\x55\xaa":
        return None
    sig = head[0x1B8:0x1BC]
    if sig == b"\x00\x00\x00\x00":
        return None
    return sig.hex().upper()
