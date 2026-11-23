"""Surface scanning and failure detection on raw media.

Two independent, real techniques:

* :func:`detect_read_errors` reads the full addressable range in large blocks
  and records every region the kernel/OS refuses to return.  On Linux/macOS a
  short read (``EIO``) marks the sector range; on Windows the same happens via
  ``ERROR_CRC``/``ERROR_SECTOR_NOT_FOUND``.
* :func:`detect_bad_blocks_by_content` looks for large runs of an unwritable
  pattern (all-zero or all-0xFF) that should contain data — the classic
  signature of dead NAND blocks behind a masking controller.

Both are strictly read-only.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from usbdoctor.core.io import MediaReadError
from usbdoctor.core.io import DiskReader
from usbdoctor.core.platform import detect_system
from usbdoctor.core.util import human_size

ProgressFn = Callable[[int, int, str], None]


@dataclass
class ErrorRegion:
    offset: int
    length: int
    note: str = ""

    @property
    def end(self) -> int:
        return self.offset + self.length

    def to_dict(self) -> dict:
        return {"offset": self.offset, "length": self.length, "note": self.note}


@dataclass
class SurfaceScanResult:
    scanned_bytes: int = 0
    total_bytes: int = 0
    read_errors: list[ErrorRegion] = field(default_factory=list)
    zero_runs: list[ErrorRegion] = field(default_factory=list)
    duration_s: float = 0.0
    read_speed_bps: Optional[float] = None
    aborted: bool = False
    abort_reason: str = ""
    skipped_regions: list[ErrorRegion] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.read_errors and not self.zero_runs

    @property
    def error_bytes(self) -> int:
        return sum(r.length for r in self.read_errors)

    def to_dict(self) -> dict:
        return {
            "scanned_bytes": self.scanned_bytes,
            "total_bytes": self.total_bytes,
            "read_errors": [r.to_dict() for r in self.read_errors],
            "zero_runs": [r.to_dict() for r in self.zero_runs],
            "error_bytes": self.error_bytes,
            "duration_s": self.duration_s,
            "read_speed_bps": self.read_speed_bps,
            "aborted": self.aborted,
            "abort_reason": self.abort_reason,
            "notes": self.notes,
            "clean": self.clean,
        }


def _is_media_error(exc: Exception) -> bool:
    if isinstance(exc, MediaReadError):
        return True
    if isinstance(exc, OSError):
        # EIO=5, ENXIO=6 on Linux/macOS; Windows maps to errno 22/5
        return exc.errno in (5, 6, 22, 121) if exc.errno is not None else False
    return False


def scan_surface(
    reader: DiskReader,
    *,
    start: int = 0,
    length: Optional[int] = None,
    block_size: int = 1 << 20,
    progress: Optional[ProgressFn] = None,
    max_error_regions: int = 512,
    deadline_s: Optional[float] = None,
    detect_zero_runs: bool = True,
    zero_run_threshold: int = 4 << 20,
) -> SurfaceScanResult:
    """Read the whole range and record unreadable regions.

    Never stops at the first error: each failed block is bisected down to a
    single sector to pinpoint the damage, then scanning continues past it.
    """
    result = SurfaceScanResult()
    try:
        total = reader.size()
    except Exception as exc:
        result.aborted = True
        result.abort_reason = f"Device size could not be determined: {exc}"
        return result
    if length is None:
        length = max(0, total - start)
    end = min(start + length, total)
    result.total_bytes = max(0, end - start)
    started = time.monotonic()

    pos = start
    zero_run_start: Optional[int] = None
    while pos < end:
        if deadline_s is not None and time.monotonic() - started > deadline_s:
            result.aborted = True
            result.abort_reason = f"scan stopped after {deadline_s:.0f}s (time budget)"
            break
        chunk = min(block_size, end - pos)
        try:
            data = reader.read(pos, chunk)
        except Exception as exc:
            if _is_media_error(exc):
                if len(result.read_errors) >= max_error_regions:
                    result.notes.append("Error list capped; further unreadable regions not recorded.")
                    pos += chunk
                    continue
                bad = _bisect_error(reader, pos, chunk)
                for region in bad:
                    result.read_errors.append(region)
                pos += chunk
                result.scanned_bytes += chunk
                zero_run_start = None
                if progress:
                    progress(result.scanned_bytes, result.total_bytes, f"read error at {human_size(pos)}")
                continue
            result.aborted = True
            result.abort_reason = str(exc)
            break
        if not data:
            result.notes.append(
                f"Device returned no data beyond {human_size(pos)} — reported capacity may exceed real capacity."
            )
            break
        result.scanned_bytes += len(data)
        if detect_zero_runs:
            if data.count(0) == len(data):
                if zero_run_start is None:
                    zero_run_start = pos
            else:
                if zero_run_start is not None and pos - zero_run_start >= zero_run_threshold:
                    result.zero_runs.append(
                        ErrorRegion(zero_run_start, pos - zero_run_start, "all-zero run")
                    )
                zero_run_start = None
        pos += len(data)
        if progress and (result.scanned_bytes % (256 << 20) < block_size):
            progress(result.scanned_bytes, result.total_bytes, "scanning")
    if zero_run_start is not None and end - zero_run_start >= zero_run_threshold:
        result.zero_runs.append(ErrorRegion(zero_run_start, end - zero_run_start, "all-zero run"))

    result.duration_s = max(1e-6, time.monotonic() - started)
    if result.scanned_bytes:
        result.read_speed_bps = result.scanned_bytes / result.duration_s
    if progress:
        progress(result.scanned_bytes, result.total_bytes, "complete")
    return result


def _bisect_error(reader: DiskReader, offset: int, length: int, *, depth: int = 0) -> list[ErrorRegion]:
    """Narrow a failed block down to the smallest unreadable unit."""
    sector = max(512, reader.sector_size)
    if length <= sector * 2 or depth >= 12:
        return [ErrorRegion(offset, max(sector, length), "unreadable")]
    half = (length // 2 // sector) * sector
    if half <= 0:
        return [ErrorRegion(offset, length, "unreadable")]
    regions: list[ErrorRegion] = []
    for sub_off, sub_len in ((offset, half), (offset + half, length - half)):
        try:
            reader.read(sub_off, min(sub_len, 1 << 20))
        except Exception as exc:
            if _is_media_error(exc):
                regions.extend(_bisect_error(reader, sub_off, sub_len, depth=depth + 1))
    if not regions:
        regions.append(ErrorRegion(offset, length, "intermittent read error"))
    return regions


def detect_bad_blocks_by_content(
    reader: DiskReader,
    *,
    start: int = 0,
    length: Optional[int] = None,
    block_size: int = 1 << 20,
    progress: Optional[ProgressFn] = None,
) -> list[ErrorRegion]:
    """Flag large uniform 0xFF / 0x00 blocks inside a file-system's data area."""
    regions: list[ErrorRegion] = []
    total = reader.size()
    end = min(start + (length or total - start), total)
    pos = start
    while pos < end:
        chunk = min(block_size, end - pos)
        try:
            data = reader.read(pos, chunk)
        except Exception:
            pos += chunk
            continue
        if len(data) >= 4096:
            zeros = data.count(0)
            ff = data.count(0xFF)
            if zeros == len(data) or ff == len(data):
                regions.append(ErrorRegion(pos, len(data), "uniform 0x00" if zeros else "uniform 0xFF"))
        pos += chunk
        if progress:
            progress(min(end, pos), end, "content check")
    return regions


def check_reported_capacity(reader: DiskReader, *, probe_tail: int = 1 << 20) -> dict:
    """Verify the highest addressable byte matches the reported capacity.

    A counterfeit drive reports e.g. 128 GB but wraps around to 8 GB of real
    NAND; writes past the real end silently overwrite earlier data.  Reading the
    tail and comparing is the first evidence of that (the write test proves it).
    """
    info = {"reported": None, "readable_tail": False, "tail_matches_header": None, "note": ""}
    try:
        size = reader.size()
    except Exception as exc:
        info["note"] = f"size unknown: {exc}"
        return info
    info["reported"] = size
    try:
        tail = reader.read(max(0, size - probe_tail), probe_tail)
        info["readable_tail"] = len(tail) == probe_tail
        head = reader.read(0, min(probe_tail, size))
        info["tail_matches_header"] = tail == head if tail and head else None
    except Exception as exc:
        info["note"] = f"tail read failed: {exc}"
    return info
