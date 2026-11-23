"""Identity-verified write path used by every modifying operation."""

from __future__ import annotations

import os
import struct
from typing import Optional

from usbdoctor.core.errors import DeviceIdentityChangedError, UsbDoctorError
from usbdoctor.core.io import DiskReader, MediaReadError, normalize_device_path
from usbdoctor.core.logging import AuditLog
from usbdoctor.core.model import DeviceRef, DeviceSignature
from usbdoctor.core.platform import detect_system
from usbdoctor.security.policy import SafetyPolicy

#: How much of each end of the device is hashed for the identity fingerprint.
SIGNATURE_WINDOW = 64 * 1024


def compute_signature(path: str, *, size_bytes: Optional[int] = None) -> DeviceSignature:
    """Fingerprint a device (or image) from its leading/trailing sectors."""
    size = size_bytes
    head_hash: Optional[str] = None
    tail_hash: Optional[str] = None
    disk_sig: Optional[str] = None
    try:
        with DiskReader(path) as reader:
            if size is None:
                try:
                    size = reader.size()
                except (OSError, UsbDoctorError):
                    size = None
            head = reader.read(0, SIGNATURE_WINDOW)
            if head:
                import hashlib

                head_hash = hashlib.sha256(head).hexdigest()
                try:
                    from usbdoctor.core.io import read_mbr_disk_signature

                    disk_sig = read_mbr_disk_signature(head[:512])
                except Exception:  # pragma: no cover
                    disk_sig = None
            if size and size > SIGNATURE_WINDOW:
                tail = reader.read(max(0, size - SIGNATURE_WINDOW), SIGNATURE_WINDOW)
                if tail:
                    import hashlib

                    tail_hash = hashlib.sha256(tail).hexdigest()
    except (OSError, UsbDoctorError, MediaReadError):
        pass
    return DeviceSignature(
        path=normalize_device_path(path),
        size_bytes=size,
        head_sha256=head_hash,
        tail_sha256=tail_hash,
        disk_signature=disk_sig,
    )


class GuardedWriter:
    """Sequential writer that refuses to act on a changed or forbidden device."""

    def __init__(
        self,
        device: DeviceRef,
        *,
        expected_signature: DeviceSignature,
        policy: SafetyPolicy,
        audit: Optional[AuditLog] = None,
        operation: str = "write",
        dry_run: bool = False,
    ) -> None:
        self.device = device
        self.expected = expected_signature
        self.policy = policy
        self.audit = audit
        self.operation = operation
        self.dry_run = dry_run
        self._fd: Optional[int] = None
        self._verified = False
        self._bytes_written = 0

    # ------------------------------------------------------------------- open
    def __enter__(self) -> "GuardedWriter":
        self.policy.check_writable(self.device, self.operation)
        self.verify_identity()
        if not self.dry_run:
            self._open()
        if self.audit:
            self.audit.record(
                f"{self.operation}:start",
                device_path=self.device.path,
                serial=self.device.serial,
                detail={"dry_run": self.dry_run, "signature": self.expected.to_dict()},
            )
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
        if self.audit:
            self.audit.record(
                f"{self.operation}:finish",
                device_path=self.device.path,
                serial=self.device.serial,
                outcome="error" if exc_type else "ok",
                detail={"bytes_written": self._bytes_written, "error": str(exc) if exc else None},
            )

    def _open(self) -> None:
        system = detect_system()
        path = normalize_device_path(self.device.path)
        if system == "windows":
            from usbdoctor.core.io import _win_open

            self._fd = _win_open(path, write=True)
        else:
            flags = os.O_WRONLY | (os.O_BINARY if hasattr(os, "O_BINARY") else 0)  # type: ignore[attr-defined]
            self._fd = os.open(path, flags)

    def close(self) -> None:
        if self._fd is not None:
            try:
                if not self.dry_run:
                    try:
                        os.fsync(self._fd)
                    except OSError:
                        pass
            finally:
                os.close(self._fd)
                self._fd = None

    # --------------------------------------------------------------- identity
    def verify_identity(self) -> DeviceSignature:
        """Recompute the fingerprint now; raise if it no longer matches."""
        current = compute_signature(self.device.path, size_bytes=self.device.size_bytes)
        if self.expected.head_sha256 and current.head_sha256 != self.expected.head_sha256:
            raise DeviceIdentityChangedError(
                "The device header changed since the scan — refusing to write.",
                hint="If you did not modify it, the drive may be failing intermittently. Re-scan first.",
                device=self.device.path,
            )
        if (
            self.expected.size_bytes is not None
            and current.size_bytes is not None
            and current.size_bytes != self.expected.size_bytes
        ):
            raise DeviceIdentityChangedError(
                "The device capacity changed since the scan — refusing to write.",
                device=self.device.path,
            )
        self._verified = True
        return current

    def reverify(self) -> None:
        """Cheap re-check used between batched write steps."""
        self.verify_identity()

    # ------------------------------------------------------------------ write
    def write(self, offset: int, data: bytes, *, note: str = "") -> int:
        if not data:
            return 0
        if not self._verified:
            raise DeviceIdentityChangedError("Write attempted before identity verification.")
        sector = max(1, self.device.sector_size)
        if offset % sector or len(data) % sector:
            raise UsbDoctorError(
                f"Raw writes must be sector-aligned ({sector} bytes). "
                f"offset={offset} length={len(data)}"
            )
        if self.dry_run:
            self._bytes_written += len(data)
            if self.audit:
                self.audit.record(
                    f"{self.operation}:dry-write",
                    device_path=self.device.path,
                    serial=self.device.serial,
                    detail={"offset": offset, "length": len(data), "note": note},
                )
            return len(data)
        assert self._fd is not None
        os.lseek(self._fd, offset, os.SEEK_SET)
        written = 0
        while written < len(data):
            written += os.write(self._fd, data[written:])
        self._bytes_written += written
        return written

    def write_zeroes(self, offset: int, length: int) -> int:
        chunk = 1 << 20
        total = 0
        pos = offset
        end = offset + length
        while pos < end:
            n = min(chunk, end - pos)
            self.write(pos, b"\x00" * n)
            pos += n
            total += n
        return total

    # ------------------------------------------------------------------ read
    def readback(self, offset: int, length: int) -> bytes:
        """Verify a write by reading through an independent handle."""
        with DiskReader(self.device.path, sector_size=self.device.sector_size) as reader:
            return reader.read(offset, length)


def aligned(offset: int, length: int, sector: int = 512) -> tuple[int, int]:
    """Expand a range so it covers whole sectors."""
    start = (offset // sector) * sector
    end = ((offset + length + sector - 1) // sector) * sector
    return start, end - start
