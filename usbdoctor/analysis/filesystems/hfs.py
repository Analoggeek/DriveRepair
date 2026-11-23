"""HFS+ / HFSX analysis: volume header, backup header, catalog plausibility."""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Optional

from usbdoctor.analysis.partitions.tables import TableIssue
from usbdoctor.core.io import DiskReader

HFS_SIG = b"H+"
HFSX_SIG = b"HX"
HFS_ALT_SIG = b"H+"
WRAPPER_SIG = b"BD"


@dataclass
class HfsResult:
    signature: str = ""
    version: int = 0
    block_size: int = 4096
    total_blocks: int = 0
    free_blocks: int = 0
    file_count: int = 0
    folder_count: int = 0
    volume_name: str = ""
    journaled: bool = False
    unmounted_cleanly: bool = True
    backup_header_valid: Optional[bool] = None
    is_hfsx: bool = False
    issues: list[TableIssue] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def volume_size(self) -> int:
        return self.block_size * self.total_blocks

    def to_dict(self) -> dict:
        return {
            "signature": self.signature,
            "version": self.version,
            "block_size": self.block_size,
            "total_blocks": self.total_blocks,
            "free_blocks": self.free_blocks,
            "file_count": self.file_count,
            "folder_count": self.folder_count,
            "volume_name": self.volume_name,
            "journaled": self.journaled,
            "unmounted_cleanly": self.unmounted_cleanly,
            "backup_header_valid": self.backup_header_valid,
            "is_hfsx": self.is_hfsx,
            "volume_size": self.volume_size,
            "issues": [i.__dict__ for i in self.issues],
            "notes": self.notes,
        }


class HfsAnalyzer:
    def __init__(self, reader: DiskReader, base_offset: int = 0, volume_size: Optional[int] = None) -> None:
        self.reader = reader
        self.base = base_offset
        self.volume_size = volume_size

    def _read_header(self, offset: int) -> Optional[dict]:
        data = self.reader.read(offset, 512)
        if len(data) < 512:
            return None
        sig = data[0:2]
        if sig not in (HFS_SIG, HFSX_SIG):
            return None
        attrs = struct.unpack(">I", data[4:8])[0]
        return {
            "signature": sig.decode("latin-1"),
            "version": struct.unpack(">H", data[2:4])[0],
            "attributes": attrs,
            "unmounted": bool(attrs & (1 << 8)),
            "journaled": bool(attrs & (1 << 13)),
            "block_size": struct.unpack(">I", data[40:44])[0],
            "total_blocks": struct.unpack(">I", data[44:48])[0],
            "free_blocks": struct.unpack(">I", data[48:52])[0],
            "file_count": struct.unpack(">I", data[32:36])[0],
            "folder_count": struct.unpack(">I", data[36:40])[0],
        }

    def analyze(self) -> HfsResult:
        res = HfsResult()
        wrapper = self.reader.read(self.base, 512)
        offset = self.base + 1024
        if len(wrapper) >= 512 and wrapper[0:2] == WRAPPER_SIG:
            # HFS+ inside an HFS wrapper: real volume header lives at block 2.
            block_size = struct.unpack(">I", wrapper[20:24])[0] or 512
            offset = self.base + 2 * block_size
            res.notes.append("HFS wrapper detected; analysing the embedded HFS+ volume.")
        header = self._read_header(offset)
        if header is None:
            res.issues.append(TableIssue("critical", "HFS_SIGNATURE_MISSING", "HFS+ volume header signature not found."))
            return res
        res.signature = header["signature"]
        res.version = header["version"]
        res.is_hfsx = header["signature"] == "HX"
        res.block_size = header["block_size"] or 4096
        res.total_blocks = header["total_blocks"]
        res.free_blocks = header["free_blocks"]
        res.file_count = header["file_count"]
        res.folder_count = header["folder_count"]
        res.journaled = header["journaled"]
        res.unmounted_cleanly = header["unmounted"]
        name_raw = self.reader.read(offset + 512, 256)
        if len(name_raw) >= 2:
            length = struct.unpack(">H", name_raw[0:2])[0]
            if 0 < length < 256:
                res.volume_name = name_raw[2 : 2 + length * 2].decode("utf-16-be", "replace")
        if not res.unmounted_cleanly:
            res.issues.append(
                TableIssue(
                    "warning",
                    "HFS_NOT_UNMOUNTED",
                    "Volume was not ejected cleanly; the journal may need replay.",
                    repairable=True,
                )
            )
        if res.block_size not in (512, 1024, 2048, 4096, 8192, 16384, 32768, 65536):
            res.issues.append(
                TableIssue("error", "HFS_BAD_BLOCK_SIZE", f"HFS+ allocation block size {res.block_size} is invalid.")
            )
        if res.free_blocks > res.total_blocks:
            res.issues.append(
                TableIssue("error", "HFS_FREE_EXCEEDS_TOTAL",
                           f"Free blocks ({res.free_blocks:,}) exceed total blocks ({res.total_blocks:,}).",
                           repairable=True)
            )
        limit = self.volume_size or self._limit()
        if limit and res.volume_size > limit:
            res.issues.append(
                TableIssue("critical", "HFS_VOLUME_BEYOND_DEVICE",
                           f"HFS+ geometry claims {res.volume_size:,} bytes but device has {limit:,}.")
            )
        # Alternate volume header at (total_blocks - 2) allocation blocks.
        alt_offset = offset + (res.total_blocks - 2) * res.block_size if res.total_blocks > 2 else 0
        if alt_offset:
            try:
                alt = self._read_header(alt_offset)
            except Exception:
                alt = None
            res.backup_header_valid = alt is not None and alt["total_blocks"] == res.total_blocks
            if not res.backup_header_valid:
                res.issues.append(
                    TableIssue("warning", "HFS_BACKUP_HEADER_INVALID",
                               "Alternate (backup) volume header is missing or inconsistent.", repairable=True)
                )
        return res

    def _limit(self) -> Optional[int]:
        try:
            return self.reader.size() - self.base
        except Exception:
            return None


def analyze_hfs(reader: DiskReader, base_offset: int = 0, volume_size: Optional[int] = None) -> HfsResult:
    return HfsAnalyzer(reader, base_offset, volume_size).analyze()
