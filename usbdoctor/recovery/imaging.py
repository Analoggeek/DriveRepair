"""Sector-by-sector imaging with resume support (the safe first step on a dying drive).

Implements a real three-pass imager modelled on ddrescue's strategy:

1. **Rescue pass** — copy forward, skipping failing blocks so the healthy
   majority is captured fast.
2. **Trim pass** — re-read failures block by block to shrink them.
3. **Scrape pass** — re-read remaining bad areas sector by sector, keeping any
   sector that finally reads.

The map file records every state so an interrupted image can be resumed exactly.
External ``ddrescue`` is used when installed (it is faster and battle-tested),
with this implementation as the dependency-free fallback and for testability.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from usbdoctor.core.errors import UsbDoctorError
from usbdoctor.core.io import DiskReader
from usbdoctor.core.util import human_size, run_process, tool_available

ProgressFn = Callable[[int, int, str], None]

#: One-character states stored per block in the map.  ``bytes.count`` needs
#: single byte values, so these are integers rather than str (a real bug we hit
#: and cover with tests).
GOOD = ord("+")
BAD = ord("-")
PENDING = ord("?")
NON_TRIMMED = ord("*")
NON_SCRAPED = ord("/")


@dataclass
class ImageMap:
    """Persisted copy map: one state byte per block."""

    source: str
    image_path: str
    block_size: int
    total_bytes: int
    states: bytearray = field(default_factory=bytearray)
    bad_offsets: list[int] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        expected = (self.total_bytes + self.block_size - 1) // self.block_size
        if not self.states:
            self.states = bytearray(bytes([PENDING]) * expected)
        elif len(self.states) != expected:
            padded = bytes(self.states[:expected]).ljust(expected, bytes([PENDING]))
            self.states = bytearray(padded)

    # --------------------------------------------------------------- helpers
    @property
    def map_path(self) -> Path:
        return Path(self.image_path + ".map")

    @property
    def bytes_done(self) -> int:
        good = self.states.count(GOOD)
        return min(self.total_bytes, good * self.block_size)

    @property
    def bytes_bad(self) -> int:
        return self.states.count(BAD) * self.block_size

    def percent(self) -> float:
        if not self.total_bytes:
            return 0.0
        done = self.states.count(GOOD) + self.states.count(BAD)
        return done / len(self.states) * 100.0 if self.states else 0.0

    def save(self) -> None:
        payload = {
            "version": 1,
            "source": self.source,
            "image_path": self.image_path,
            "block_size": self.block_size,
            "total_bytes": self.total_bytes,
            "states_b64": _b64(self.states),
            "bad_offsets": self.bad_offsets[:4096],
            "created_at": self.created_at,
            "updated_at": time.time(),
        }
        tmp = self.map_path.with_suffix(".map.tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, self.map_path)

    @classmethod
    def load(cls, map_path: Path) -> "ImageMap":
        payload = json.loads(Path(map_path).read_text(encoding="utf-8"))
        instance = cls(
            source=payload["source"],
            image_path=payload["image_path"],
            block_size=payload["block_size"],
            total_bytes=payload["total_bytes"],
            states=bytearray(_unb64(payload["states_b64"])),
            bad_offsets=payload.get("bad_offsets", []),
            created_at=payload.get("created_at", time.time()),
            updated_at=payload.get("updated_at", time.time()),
        )
        return instance

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "image_path": self.image_path,
            "block_size": self.block_size,
            "total_bytes": self.total_bytes,
            "percent": round(self.percent(), 2),
            "bytes_good": self.bytes_done,
            "bytes_bad": self.bytes_bad,
            "bad_blocks": self.states.count(BAD),
            "resumable": self.map_path.exists(),
        }


def _b64(data: bytes) -> str:
    import base64

    return base64.b64encode(bytes(data)).decode("ascii")


def _unb64(text: str) -> bytes:
    import base64

    return base64.b64decode(text.encode("ascii"))


@dataclass
class ImagingResult:
    image_path: str
    total_bytes: int
    good_bytes: int
    bad_bytes: int
    duration_s: float
    passes: int
    aborted: bool = False
    engine: str = "builtin"

    @property
    def percent(self) -> float:
        return (self.good_bytes / self.total_bytes * 100.0) if self.total_bytes else 0.0

    def to_dict(self) -> dict:
        return {
            "image_path": self.image_path,
            "total_bytes": self.total_bytes,
            "good_bytes": self.good_bytes,
            "bad_bytes": self.bad_bytes,
            "percent": round(self.percent, 2),
            "duration_s": round(self.duration_s, 1),
            "passes": self.passes,
            "aborted": self.aborted,
            "engine": self.engine,
            "human_size": human_size(self.total_bytes),
        }


class ImageCreator:
    def __init__(
        self,
        source: str,
        image_path: str,
        *,
        total_bytes: Optional[int] = None,
        block_size: int = 1 << 20,
        sector_size: int = 512,
        progress: Optional[ProgressFn] = None,
        resume: bool = True,
    ) -> None:
        self.source = source
        self.image_path = image_path
        self.block_size = max(block_size, sector_size)
        self.sector_size = sector_size
        self.progress = progress
        self.resume = resume
        self._total = total_bytes
        self._abort = False

    def abort(self) -> None:
        self._abort = True

    # ------------------------------------------------------------------- run
    def run(self) -> ImagingResult:
        started = time.time()
        total = self._total
        if total is None:
            with DiskReader(self.source) as reader:
                total = reader.size()
        if not total:
            raise UsbDoctorError(
                "The device reports zero capacity, so there is nothing to image.",
                hint="A controller locked this way usually needs professional recovery.",
            )
        image = Path(self.image_path)
        image.parent.mkdir(parents=True, exist_ok=True)

        map_path = Path(self.image_path + ".map")
        if self.resume and map_path.exists():
            image_map = ImageMap.load(map_path)
            if image_map.source != self.source or image_map.total_bytes != total:
                image_map = ImageMap(self.source, self.image_path, self.block_size, total)
        else:
            image_map = ImageMap(self.source, self.image_path, self.block_size, total)

        # Pre-allocate the image so offsets map 1:1 with the source.
        if not image.exists() or image.stat().st_size != total:
            with open(image, "wb") as fh:
                fh.truncate(total)
        passes = 0
        # Three real passes: copy everything readable, then shrink the failures,
        # then scrape the remaining areas sector by sector.
        for attempt, (label, wanted) in enumerate(
            (
                ("rescue", {PENDING, BAD, NON_TRIMMED, NON_SCRAPED}),
                ("trim", {BAD, NON_TRIMMED}),
                ("scrape", {BAD}),
            ),
            start=1,
        ):
            if self._abort:
                break
            pending = [i for i, st in enumerate(image_map.states) if st in wanted and st != GOOD]
            if not pending:
                continue
            passes = attempt
            with DiskReader(self.source, sector_size=self.sector_size) as reader, open(image, "r+b") as out:
                self._copy_pass(reader, out, image_map, pending, label)
            image_map.save()
            if not any(st == BAD for st in image_map.states):
                break

        image_map.updated_at = time.time()
        image_map.save()
        return ImagingResult(
            image_path=str(image),
            total_bytes=total,
            good_bytes=image_map.bytes_done,
            bad_bytes=image_map.bytes_bad,
            duration_s=time.time() - started,
            passes=max(1, passes),
            aborted=self._abort,
        )

    def _copy_pass(
        self,
        reader: DiskReader,
        out,
        image_map: ImageMap,
        pending: list[int],
        label: str,
    ) -> None:
        block = image_map.block_size
        handled = 0
        for index in pending:
            if self._abort:
                return
            offset = index * block
            length = min(block, image_map.total_bytes - offset)
            if length <= 0:
                image_map.states[index] = GOOD
                continue
            try:
                data = reader.read(offset, length)
            except Exception:
                if label == "scrape":
                    self._scrape_sectors(reader, out, image_map, index, offset, length)
                elif label == "trim":
                    image_map.states[index] = NON_SCRAPED
                else:
                    image_map.states[index] = BAD
                handled += 1
                continue
            if not data:
                image_map.states[index] = BAD
                continue
            out.seek(offset)
            out.write(data)
            if len(data) < length:
                image_map.states[index] = BAD
                image_map.bad_offsets.append(offset + len(data))
            else:
                image_map.states[index] = GOOD
            handled += 1
            if self.progress and handled % 16 == 0:
                self.progress(image_map.bytes_done, image_map.total_bytes, f"{label} pass")
        out.flush()
        os.fsync(out.fileno())
        if self.progress:
            self.progress(image_map.bytes_done, image_map.total_bytes, f"{label} pass complete")

    def _scrape_sectors(self, reader: DiskReader, out, image_map: ImageMap, index: int, offset: int, length: int) -> None:
        """Last-chance read of individual sectors inside a failed block."""
        sector = self.sector_size
        recovered = 0
        pos = offset
        while pos < offset + length and not self._abort:
            try:
                chunk = reader.read(pos, sector)
            except Exception:
                pos += sector
                continue
            if chunk:
                out.seek(pos)
                out.write(chunk)
                recovered += len(chunk)
            pos += sector
        image_map.states[index] = GOOD if recovered >= length else BAD
        if recovered < length:
            image_map.bad_offsets.append(offset)


# ---------------------------------------------------------------------------
# ddrescue back-end
# ---------------------------------------------------------------------------


def ddrescue_available() -> bool:
    return tool_available("ddrescue") or tool_available("gddrescue")


def create_image_ddrescue(
    source: str,
    image_path: str,
    *,
    progress: Optional[ProgressFn] = None,
    timeout: float = 86400,
) -> ImagingResult:
    """Use GNU ddrescue (preferred when installed) with a real map file."""
    exe = "ddrescue" if tool_available("ddrescue") else "gddrescue"
    started = time.time()
    map_path = image_path + ".map"
    argv = [exe, "-f", "-n", source, image_path, map_path]
    proc = run_process(argv, timeout=timeout)
    if not proc.ok and "already" not in proc.stderr.lower():
        raise UsbDoctorError(
            f"ddrescue failed: {proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else proc.returncode}"
        )
    # Second pass trims bad areas.
    run_process([exe, "-f", "-d", source, image_path, map_path], timeout=timeout)
    good, bad = _parse_ddrescue_map(map_path)
    return ImagingResult(
        image_path=image_path,
        total_bytes=good + bad,
        good_bytes=good,
        bad_bytes=bad,
        duration_s=time.time() - started,
        passes=2,
        engine="ddrescue",
    )


def _parse_ddrescue_map(map_path: str) -> tuple[int, int]:
    good = bad = 0
    try:
        with open(map_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not line or line[0] not in "+-*/?":
                    continue
                parts = line.split()
                if len(parts) < 3:
                    continue
                try:
                    size = int(parts[2], 16) if parts[2].startswith("0x") else int(parts[2])
                except ValueError:
                    continue
                if line[0] == "+":
                    good += size
                elif line[0] == "-":
                    bad += size
    except OSError:
        pass
    return good, bad
