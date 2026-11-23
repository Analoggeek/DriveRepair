"""Content carver: recovers files from RAW, formatted or otherwise unmountable media.

Works purely on bytes, so it succeeds in exactly the cases where the file-system
metadata is gone.  Deleted-name recovery (when a healthy directory still exists)
is handled by :mod:`usbdoctor.recovery.fat_recovery`.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from usbdoctor.core.io import DiskReader
from usbdoctor.core.util import human_size, safe_filename
from usbdoctor.recovery.signatures import (
    END_SEARCH_WINDOW,
    SIGNATURES,
    Signature,
    extension_for,
    find_signatures,
)

CHUNK = 8 << 20
#: Two carved files may not overlap; touching boundaries are allowed.
MIN_CARVE_GAP = 0


@dataclass
class RecoveredFile:
    index: int
    filename: str
    path: str
    size_bytes: int
    source_offset: int
    signature: str
    category: str
    confidence: str  # high | medium | low
    complete: bool

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "filename": self.filename,
            "path": self.path,
            "size_bytes": self.size_bytes,
            "source_offset": self.source_offset,
            "signature": self.signature,
            "category": self.category,
            "confidence": self.confidence,
            "complete": self.complete,
        }


@dataclass
class CarveResult:
    scan_start: int
    scan_length: int
    files: list[RecoveredFile] = field(default_factory=list)
    bytes_written: int = 0
    duration_s: float = 0.0
    aborted: bool = False
    skipped_oversize: int = 0
    output_dir: str = ""

    def to_dict(self) -> dict:
        return {
            "scan_start": self.scan_start,
            "scan_length": self.scan_length,
            "output_dir": self.output_dir,
            "file_count": len(self.files),
            "bytes_written": self.bytes_written,
            "duration_s": round(self.duration_s, 1),
            "aborted": self.aborted,
            "skipped_oversize": self.skipped_oversize,
            "files": [f.to_dict() for f in self.files],
            "by_category": self.by_category(),
        }

    def by_category(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.files:
            counts[item.category] = counts.get(item.category, 0) + 1
        return counts


class Carver:
    def __init__(
        self,
        reader: DiskReader,
        output_dir: str,
        *,
        categories: Optional[set[str]] = None,
        progress=None,
        max_files: int = 5000,
    ) -> None:
        self.reader = reader
        self.output_dir = Path(output_dir)
        self.categories = categories
        self.progress = progress
        self.max_files = max_files
        self._abort = False
        self._index = 0
        # Must be per-instance: a class-level default meant a second Carver in
        # the same process could never emit anything.
        self._last_emitted = -1
        self._last_size = 0
        self.result: Optional[CarveResult] = None

    def abort(self) -> None:
        self._abort = True

    # ------------------------------------------------------------------- run
    def carve(self, start: int = 0, length: Optional[int] = None) -> CarveResult:
        started = time.time()
        total = self.reader.size()
        end = min(start + (length if length is not None else total), total)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        result = CarveResult(scan_start=start, scan_length=max(0, end - start), output_dir=str(self.output_dir))
        self.result = result
        pos = start
        carry = b""
        while pos < end and not self._abort and len(result.files) < self.max_files:
            want = min(CHUNK, end - pos)
            try:
                data = self.reader.read(pos, want)
            except Exception:
                # Unreadable area: skip forward but keep alignment for the carry.
                pos += want
                carry = b""
                continue
            if not data:
                break
            buffer = carry + data
            base = pos - len(carry)
            self._process(buffer, base, end, result)
            carry = buffer[-(END_SEARCH_WINDOW // 4) :] if len(buffer) > END_SEARCH_WINDOW // 4 else buffer
            pos += len(data)
            if self.progress:
                self.progress(pos - start, end - start, "carving")
        result.duration_s = time.time() - started
        result.aborted = self._abort
        self._write_manifest(result)
        return result

    # --------------------------------------------------------------- helpers
    def _process(self, buffer: bytes, base: int, end: int, result: CarveResult) -> None:
        matches = find_signatures(buffer, category_filter=self.categories)
        for offset_in_buf, sig in matches:
            absolute = base + offset_in_buf
            # Skip headers that fall *inside* the file we just extracted (a
            # JPEG payload can legitimately contain another JPEG header), but
            # never reject a header that starts exactly where the last file
            # ended: contiguous files are extremely common on real media.
            if self._last_size and self._last_emitted <= absolute < self._last_emitted + self._last_size:
                continue
            if absolute + sig.min_size > end:
                continue
            payload, complete = self._extract(sig, absolute, buffer, offset_in_buf)
            if payload is None:
                continue
            if len(payload) > sig.max_size:
                result.skipped_oversize += 1
                continue
            if len(payload) < sig.min_size:
                continue
            self._last_emitted = absolute
            self._last_size = len(payload)
            self._save(sig, payload, absolute, complete, result)

    def _extract(
        self, sig: Signature, absolute: int, buffer: bytes, offset_in_buf: int
    ) -> tuple[Optional[bytes], bool]:
        if sig.find_end is not None:
            window = buffer[offset_in_buf : offset_in_buf + END_SEARCH_WINDOW]
            if len(window) < END_SEARCH_WINDOW:
                try:
                    window += self.reader.read(absolute + len(window), END_SEARCH_WINDOW - len(window))
                except Exception:
                    pass
            end_idx = sig.find_end(window, len(sig.headers[0]))
            if end_idx is not None:
                return window[:end_idx], True
            # No terminator inside the window: fall back to the next header.
            return window, False
        # No terminator type: cut at the next header of any type inside the block.
        rest = buffer[offset_in_buf :]
        nexts = [off for off, _ in find_signatures(rest[64:], category_filter=self.categories)]
        if nexts:
            cut = nexts[0] + 64
            return rest[:cut], False
        return rest, False

    def _save(self, sig: Signature, payload: bytes, absolute: int, complete: bool, result: CarveResult) -> None:
        self._index += 1
        name = safe_filename(
            f"recovered_{self._index:05d}_{absolute:012x}{extension_for(sig)}"
        )
        target = self.output_dir / name
        target.write_bytes(payload)
        os.chmod(target, 0o600)
        result.bytes_written += len(payload)
        result.files.append(
            RecoveredFile(
                index=self._index,
                filename=name,
                path=str(target),
                size_bytes=len(payload),
                source_offset=absolute,
                signature=sig.label,
                category=sig.category,
                confidence="high" if complete else "medium",
                complete=complete,
            )
        )

    def _write_manifest(self, result: CarveResult) -> None:
        manifest = {
            "generated_at": time.time(),
            "output_dir": str(self.output_dir),
            "summary": {
                "file_count": len(result.files),
                "bytes_written": result.bytes_written,
                "scan_start": result.scan_start,
                "scan_length": result.scan_length,
                "duration_s": round(result.duration_s, 1),
                "by_category": result.by_category(),
            },
            "files": [f.to_dict() for f in result.files],
        }
        path = self.output_dir / "recovery_manifest.json"
        path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def supported_categories() -> list[str]:
    return sorted({sig.category for sig in SIGNATURES})


def supported_extensions() -> list[str]:
    return sorted({ext for sig in SIGNATURES for ext in sig.extensions})
