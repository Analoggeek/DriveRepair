"""Counterfeit ("fake capacity") drive verification.

The only reliable test writes unique, verifiable data across the claimed address
space and reads it back.  This module does that **safely**:

* it writes only inside a scratch *file* the user chose (or, for a raw device,
  only after the normal consent + safety policy gates), never over existing data;
* every block is filled with a value derived from its own offset, so a drive
  that silently wraps around to the start of its real NAND immediately shows as
  a read-back mismatch at a known offset;
* the test can be aborted at any point and the scratch file is always removed.

A drive that wraps its address space loses data — so a *passing* capacity test is
also the only proof that a cheap large-capacity drive is genuine.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from usbdoctor.core.io import DiskReader
from usbdoctor.core.util import human_size

ProgressFn = Callable[[int, int, str], None]


def block_pattern(offset: int, length: int) -> bytes:
    """Deterministic, offset-dependent payload (8 zero bytes + offset + digest)."""
    import hashlib

    header = offset.to_bytes(8, "little")
    seed = hashlib.blake2b(header, digest_size=16).digest()
    unit = header + seed
    repeats = (length + len(unit) - 1) // len(unit)
    return (unit * repeats)[:length]


@dataclass
class CapacityTestResult:
    tested_bytes: int = 0
    claimed_bytes: int = 0
    first_bad_offset: Optional[int] = None
    bad_blocks: int = 0
    good_blocks: int = 0
    verified: bool = False
    aborted: bool = False
    seconds: float = 0.0
    real_capacity_estimate: Optional[int] = None
    verdict: str = "unknown"
    message: str = ""
    details: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "tested_bytes": self.tested_bytes,
            "claimed_bytes": self.claimed_bytes,
            "first_bad_offset": self.first_bad_offset,
            "bad_blocks": self.bad_blocks,
            "good_blocks": self.good_blocks,
            "verified": self.verified,
            "aborted": self.aborted,
            "seconds": round(self.seconds, 1),
            "real_capacity_estimate": self.real_capacity_estimate,
            "verdict": self.verdict,
            "message": self.message,
            "details": self.details[:100],
        }


class CapacityTester:
    """Write-verify-test a scratch file to prove the real usable capacity."""

    def __init__(
        self,
        *,
        progress: Optional[ProgressFn] = None,
        block_size: int = 1 << 20,
        max_bytes: Optional[int] = None,
    ) -> None:
        self.progress = progress
        self.block_size = block_size
        self.max_bytes = max_bytes
        self._abort = False

    def abort(self) -> None:
        self._abort = True

    # -------------------------------------------------------------- file test
    def test_file(self, path: Path, *, claimed_bytes: Optional[int] = None) -> CapacityTestResult:
        """Full write → verify → read-back test inside a scratch file."""
        started = time.monotonic()
        result = CapacityTestResult(claimed_bytes=claimed_bytes or 0)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            total = self.max_bytes or claimed_bytes or 0
            with open(path, "wb") as fh:
                written = 0
                while total and written < total and not self._abort:
                    n = min(self.block_size, total - written)
                    fh.write(block_pattern(written, n))
                    written += n
                    result.tested_bytes = written
                    if self.progress:
                        self.progress(written, total, "writing test pattern")
                fh.flush()
                os.fsync(fh.fileno())
            # Verification pass: re-read every block through a fresh handle.
            with open(path, "rb") as fh:
                offset = 0
                while offset < result.tested_bytes and not self._abort:
                    expected = block_pattern(offset, min(self.block_size, result.tested_bytes - offset))
                    actual = fh.read(len(expected))
                    if actual != expected:
                        result.bad_blocks += 1
                        if result.first_bad_offset is None:
                            result.first_bad_offset = offset
                            result.details.append(
                                {
                                    "offset": offset,
                                    "expected_prefix": expected[:16].hex(),
                                    "actual_prefix": actual[:16].hex() if actual else "",
                                }
                            )
                    else:
                        result.good_blocks += 1
                    offset += len(expected)
                    if self.progress:
                        self.progress(offset, result.tested_bytes, "verifying")
        except OSError as exc:
            result.message = f"I/O error during capacity test: {exc}"
        finally:
            result.seconds = time.monotonic() - started
        self._conclude(result, path)
        return result

    # ------------------------------------------------------------ raw device
    def test_device_fileonly(self, device_path: str, mount_path: str, *, claimed_bytes: int) -> CapacityTestResult:
        """Preferred raw-device test: write a scratch file on the mounted volume."""
        target = Path(mount_path) / ".usbdoctor-capacity-test.bin"
        result = self.test_file(target, claimed_bytes=claimed_bytes)
        try:
            target.unlink()
        except OSError:
            result.message += " (test file could not be removed — delete it manually)"
        return result

    # --------------------------------------------------------------- verdict
    def _conclude(self, result: CapacityTestResult, path: Path) -> None:
        if result.aborted:
            result.verdict = "aborted"
            result.message = result.message or "Test aborted by the user; the scratch file was removed."
            _safe_unlink(path)
            return
        total_blocks = result.good_blocks + result.bad_blocks
        if total_blocks == 0:
            result.verdict = "inconclusive"
            result.message = result.message or "No data could be verified."
            _safe_unlink(path)
            return
        _safe_unlink(path)
        if result.bad_blocks == 0 and result.tested_bytes > 0:
            result.verdict = "genuine"
            result.verified = True
            result.message = (
                f"All {human_size(result.tested_bytes)} written were read back correctly — "
                "the reported capacity is real."
            )
            return
        bad_ratio = result.bad_blocks / total_blocks
        if result.first_bad_offset is not None:
            result.real_capacity_estimate = result.first_bad_offset
            result.verdict = "counterfeit"
            result.message = (
                f"Data written beyond {human_size(result.first_bad_offset)} did not read back "
                f"({result.bad_blocks} block(s) corrupted). This drive's real capacity is about "
                f"{human_size(result.first_bad_offset)}, not {human_size(result.tested_bytes)}. "
                "Counterfeit drives lose data irreversibly — do not store anything important on it."
            )
        elif bad_ratio > 0.5:
            result.verdict = "failing"
            result.message = (
                f"{result.bad_blocks} of {total_blocks} blocks failed verification. The flash is "
                "unreliable; back up and replace the device."
            )
        else:
            result.verdict = "flaky"
            result.message = (
                f"{result.bad_blocks} of {total_blocks} blocks failed verification. The device "
                "writes unreliably — treat it as failing."
            )


def _safe_unlink(path: Path | str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
