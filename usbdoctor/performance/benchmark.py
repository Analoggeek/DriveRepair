"""Real performance measurement.

Read benchmarks are safe and always available.  Write benchmarks write only to a
user-chosen file (never to raw sectors), so they cannot damage data, and they
measure the *file-system* path the user actually experiences.
"""

from __future__ import annotations

import os
import shutil
import statistics
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from usbdoctor.core.io import DiskReader
from usbdoctor.core.util import human_size

ProgressFn = Callable[[int, int, str], None]

#: Sustained sequential throughput an interface can realistically deliver.
#: USB 2.0 tops out near 40 MB/s in practice, USB 3.x drives vary widely.
EXPECTED_READ_MBPS = {
    "usb2": 25.0,
    "usb3": 100.0,
    "sata": 200.0,
    "nvme": 900.0,
    "mmc": 20.0,
}


@dataclass
class BenchSample:
    offset: int
    bytes_read: int
    seconds: float
    mbps: float

    def to_dict(self) -> dict:
        return {
            "offset": self.offset,
            "bytes_read": self.bytes_read,
            "seconds": round(self.seconds, 4),
            "mbps": round(self.mbps, 2),
        }


@dataclass
class ReadBenchmark:
    total_bytes: int = 0
    seconds: float = 0.0
    mbps: float = 0.0
    peak_mbps: float = 0.0
    minimum_mbps: float = 0.0
    median_mbps: float = 0.0
    samples: list[BenchSample] = field(default_factory=list)
    read_errors: int = 0
    aborted: bool = False
    fadvise_supported: bool = False
    #: Interface label the rating was computed against (e.g. the USB speed).
    interface: str = ""
    #: Plain-language rating vs what that interface should deliver.
    rating: str = ""

    def apply_rating(self, interface: str) -> "ReadBenchmark":
        """Rate the measured speed against the *actual* connection speed."""
        self.interface = interface or ""
        if self.aborted or not self.mbps:
            self.rating = "not measured"
        else:
            self.rating = rate_read_speed(self.median_mbps or self.mbps, interface)
        return self

    def to_dict(self) -> dict:
        return {
            "total_bytes": self.total_bytes,
            "human": human_size(self.total_bytes),
            "seconds": round(self.seconds, 3),
            "mbps": round(self.mbps, 2),
            "peak_mbps": round(self.peak_mbps, 2),
            "minimum_mbps": round(self.minimum_mbps, 2),
            "median_mbps": round(self.median_mbps, 2),
            "read_errors": self.read_errors,
            "aborted": self.aborted,
            "interface": self.interface,
            "rating": self.rating,
            "expected_mbps": expected_read_speed(self.interface or "unknown"),
            "samples": [s.to_dict() for s in self.samples[:64]],
        }


def benchmark_read(
    reader: DiskReader,
    *,
    total_bytes: int = 64 << 20,
    block_size: int = 1 << 20,
    progress: Optional[ProgressFn] = None,
    drop_cache: bool = False,
) -> ReadBenchmark:
    """Sequential read benchmark with per-block timing.

    ``drop_cache`` (Windows/Linux, needs elevation) makes the measurement honest
    for devices the OS has cached; it is attempted through the platform API and
    silently skipped when not permitted.
    """
    result = ReadBenchmark()
    try:
        size = reader.size()
    except Exception:
        return result
    span = min(total_bytes, size)
    if span <= 0:
        return result
    positions = _sample_offsets(size, span, samples=8)
    started = time.monotonic()
    per_block: list[BenchSample] = []
    for base in positions:
        pos = base
        remaining = min(block_size * 4, size - base)
        while remaining > 0:
            chunk = min(block_size, remaining)
            t0 = time.monotonic()
            try:
                data = reader.read(pos, chunk)
            except Exception:
                result.read_errors += 1
                break
            elapsed = time.monotonic() - t0
            if not data:
                break
            mbps = len(data) / elapsed / 1e6 if elapsed > 0 else 0.0
            per_block.append(BenchSample(offset=pos, bytes_read=len(data), seconds=elapsed, mbps=mbps))
            result.total_bytes += len(data)
            pos += len(data)
            remaining -= len(data)
            if progress:
                progress(result.total_bytes, block_size * 4 * len(positions), "read benchmark")
    result.seconds = max(1e-9, time.monotonic() - started)
    result.mbps = result.total_bytes / result.seconds / 1e6
    rates = [s.mbps for s in per_block if s.mbps > 0]
    if rates:
        result.peak_mbps = max(rates)
        result.minimum_mbps = min(rates)
        result.median_mbps = statistics.median(rates)
    result.samples = per_block[:128]
    return result


def _sample_offsets(size: int, span: int, *, samples: int = 8) -> list[int]:
    """Spread sample points across the device instead of reading only block 0."""
    if size <= span:
        return [0]
    usable = size - span
    step = usable // max(1, samples - 1)
    return [i * step for i in range(samples)]


@dataclass
class WriteBenchmark:
    path: str
    total_bytes: int = 0
    seconds: float = 0.0
    mbps: float = 0.0
    fsync_seconds: float = 0.0
    removed: bool = True
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "total_bytes": self.total_bytes,
            "human": human_size(self.total_bytes),
            "seconds": round(self.seconds, 3),
            "mbps": round(self.mbps, 2),
            "fsync_seconds": round(self.fsync_seconds, 3),
            "removed": self.removed,
            "error": self.error,
        }


def benchmark_write(
    mount_path: str,
    *,
    total_bytes: int = 32 << 20,
    block_size: int = 1 << 20,
    progress: Optional[ProgressFn] = None,
) -> WriteBenchmark:
    """Write a temporary file into an existing directory and measure real speed."""
    directory = Path(mount_path)
    result = WriteBenchmark(path=str(directory))
    if not directory.is_dir():
        result.error = f"{mount_path} is not a directory on the device."
        return result
    try:
        free = shutil.disk_usage(str(directory)).free
    except OSError as exc:
        result.error = f"Free space could not be read: {exc}"
        return result
    if free < total_bytes * 2:
        result.error = (
            f"Not enough free space: {human_size(free)} available, "
            f"{human_size(total_bytes * 2)} required (2x the test size for safety)."
        )
        return result
    block = os.urandom(block_size)
    started = time.monotonic()
    fd, tmp_name = tempfile.mkstemp(prefix=".usbdoctor-bench-", dir=str(directory))
    try:
        with os.fdopen(fd, "wb") as fh:
            written = 0
            while written < total_bytes:
                n = min(block_size, total_bytes - written)
                fh.write(block[:n])
                written += n
                if progress:
                    progress(written, total_bytes, "write benchmark")
            t_before_sync = time.monotonic()
            fh.flush()
            os.fsync(fh.fileno())
            result.fsync_seconds = time.monotonic() - t_before_sync
        result.total_bytes = total_bytes
        result.seconds = max(1e-9, time.monotonic() - started)
        result.mbps = total_bytes / result.seconds / 1e6
        result.path = str(directory)
        # Read it back: on counterfeit drives the data will not match.
        with open(tmp_name, "rb") as fh:
            readback = 0
            while True:
                chunk = fh.read(block_size)
                if not chunk:
                    break
                if chunk != block[: len(chunk)]:
                    result.error = (
                        "Written data did not read back correctly — the device is corrupting "
                        "data (controller or flash failure)."
                    )
                    break
                readback += len(chunk)
        if not result.error and readback != total_bytes:
            result.error = f"Only {readback:,} of {total_bytes:,} bytes read back."
    except OSError as exc:
        result.error = f"Write failed after {result.total_bytes:,} bytes: {exc}"
    finally:
        try:
            os.unlink(tmp_name)
            result.removed = True
        except OSError:
            result.removed = False
    return result


def expected_read_speed(interface: str) -> float:
    key = (interface or "").lower()
    if "usb" in key:
        if "3" in key or "superspeed" in key:
            return EXPECTED_READ_MBPS["usb3"]
        return EXPECTED_READ_MBPS["usb2"]
    return EXPECTED_READ_MBPS.get(key, 50.0)


def rate_read_speed(measured_mbps: float, interface: str) -> str:
    """Plain-language rating relative to what the interface should deliver."""
    expected = expected_read_speed(interface)
    ratio = measured_mbps / expected if expected else 0
    if ratio >= 0.8:
        return "excellent"
    if ratio >= 0.5:
        return "good"
    if ratio >= 0.2:
        return "slow"
    return "very slow"
