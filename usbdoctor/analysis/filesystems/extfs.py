"""ext2/ext3/ext4 superblock analysis (Linux, Android cards, Raspberry Pi media)."""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass, field
from typing import Optional

from usbdoctor.analysis.partitions.tables import TableIssue
from usbdoctor.core.io import DiskReader

EXT_MAGIC = 0xEF53
SUPERBLOCK_OFFSET = 1024
INCOMPAT_FILETYPE = 0x2
INCOMPAT_EXTENTS = 0x40
INCOMPAT_64BIT = 0x80
RO_COMPAT_SPARSE_SUPER = 0x1
RO_COMPAT_METADATA_CSUM = 0x400
STATE_CLEAN = 0x0001


@dataclass
class ExtResult:
    block_size: int = 4096
    blocks_count: int = 0
    free_blocks_count: int = 0
    inodes_count: int = 0
    free_inodes_count: int = 0
    first_data_block: int = 0
    inode_size: int = 128
    inodes_per_group: int = 0
    blocks_per_group: int = 0
    volume_uuid: str = ""
    volume_name: str = ""
    last_mounted: str = ""
    state: int = 0
    errors: int = 0
    compatible: int = 0
    incompatible: int = 0
    ro_compatible: int = 0
    revision: int = 0
    last_check: Optional[int] = None
    mount_count: int = 0
    max_mount_count: int = 0
    is_ext4: bool = False
    issues: list[TableIssue] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def volume_size(self) -> int:
        return self.blocks_count * self.block_size

    @property
    def clean(self) -> bool:
        return bool(self.state & STATE_CLEAN)

    @property
    def fs_name(self) -> str:
        return "ext4" if self.is_ext4 else ("ext3" if self.compatible & 0x4 else "ext2")

    def to_dict(self) -> dict:
        return {
            "block_size": self.block_size,
            "blocks_count": self.blocks_count,
            "free_blocks_count": self.free_blocks_count,
            "inodes_count": self.inodes_count,
            "free_inodes_count": self.free_inodes_count,
            "inode_size": self.inode_size,
            "volume_uuid": self.volume_uuid,
            "volume_name": self.volume_name,
            "last_mounted": self.last_mounted,
            "state": self.state,
            "clean": self.clean,
            "errors": self.errors,
            "compatible": self.compatible,
            "incompatible": self.incompatible,
            "ro_compatible": self.ro_compatible,
            "revision": self.revision,
            "last_check": self.last_check,
            "mount_count": self.mount_count,
            "max_mount_count": self.max_mount_count,
            "fs_name": self.fs_name,
            "volume_size": self.volume_size,
            "issues": [i.__dict__ for i in self.issues],
            "notes": self.notes,
        }


class ExtAnalyzer:
    def __init__(self, reader: DiskReader, base_offset: int = 0, volume_size: Optional[int] = None) -> None:
        self.reader = reader
        self.base = base_offset
        self.volume_size = volume_size

    def analyze(self) -> ExtResult:
        res = ExtResult()
        sb = self.reader.read(self.base + SUPERBLOCK_OFFSET, 1024)
        if len(sb) < 1024:
            res.issues.append(TableIssue("critical", "EXT_SUPERBLOCK_UNREADABLE", "Superblock could not be read."))
            return res
        if struct.unpack("<H", sb[56:58])[0] != EXT_MAGIC:
            res.issues.append(
                TableIssue("critical", "EXT_MAGIC_MISSING", "ext2/3/4 magic 0xEF53 not found at offset 1080.")
            )
            return res
        res.inodes_count = struct.unpack("<I", sb[0:4])[0]
        res.blocks_count = struct.unpack("<I", sb[4:8])[0]
        res.free_blocks_count = struct.unpack("<I", sb[12:16])[0]
        res.free_inodes_count = struct.unpack("<I", sb[16:20])[0]
        res.first_data_block = struct.unpack("<I", sb[20:24])[0]
        res.block_size = 1024 << struct.unpack("<I", sb[24:28])[0]
        res.inodes_per_group = struct.unpack("<I", sb[40:44])[0]
        res.blocks_per_group = struct.unpack("<I", sb[32:36])[0]
        res.revision = struct.unpack("<I", sb[76:80])[0]
        res.mount_count = struct.unpack("<H", sb[52:54])[0]
        res.max_mount_count = struct.unpack("<h", sb[54:56])[0]
        res.state = struct.unpack("<H", sb[58:60])[0]
        res.errors = struct.unpack("<H", sb[60:62])[0]
        res.compatible = struct.unpack("<I", sb[92:96])[0]
        res.incompatible = struct.unpack("<I", sb[96:100])[0]
        res.ro_compatible = struct.unpack("<I", sb[100:104])[0]
        uuid = sb[104:120]
        res.volume_uuid = (
            f"{uuid[0:4].hex()}-{uuid[4:6].hex()}-{uuid[6:8].hex()}-{uuid[8:10].hex()}-{uuid[10:16].hex()}"
        )
        res.volume_name = sb[120:136].split(b"\x00", 1)[0].decode("utf-8", "replace")
        res.last_mounted = sb[136:200].split(b"\x00", 1)[0].decode("utf-8", "replace")
        last_check = struct.unpack("<I", sb[64:68])[0]
        res.last_check = last_check or None
        if res.revision >= 1:
            res.inode_size = struct.unpack("<H", sb[88:90])[0] or 128
        res.is_ext4 = bool(
            (res.incompatible & (INCOMPAT_EXTENTS | INCOMPAT_64BIT)) or (res.ro_compatible & RO_COMPAT_METADATA_CSUM)
        )

        if res.block_size not in (1024, 2048, 4096):
            res.issues.append(
                TableIssue("error", "EXT_BAD_BLOCK_SIZE", f"Block size {res.block_size} is invalid.")
            )
        if res.inodes_per_group == 0 or res.blocks_per_group == 0:
            res.issues.append(
                TableIssue("error", "EXT_BAD_GROUP_GEOMETRY", "Block/inode group geometry is zero (superblock corrupt).",
                           repairable=True)
            )
        if not res.clean:
            res.issues.append(
                TableIssue("warning", "EXT_NOT_CLEAN",
                           "File system is not marked clean (unclean unmount or pending journal replay).",
                           repairable=True)
            )
        if res.errors > 0:
            res.issues.append(
                TableIssue("error", "EXT_ERRORS_FLAGGED",
                           f"Kernel recorded {res.errors} file-system error(s); e2fsck should run.")
            )
        if res.free_blocks_count > res.blocks_count:
            res.issues.append(
                TableIssue("error", "EXT_FREE_EXCEEDS_TOTAL", "Free block count exceeds total block count.",
                           repairable=True)
            )
        limit = self.volume_size or self._limit()
        if limit and res.volume_size > limit:
            res.issues.append(
                TableIssue("critical", "EXT_VOLUME_BEYOND_DEVICE",
                           f"ext geometry claims {res.volume_size:,} bytes but the device has {limit:,}.")
            )
        if res.last_check:
            age_days = (time.time() - res.last_check) / 86400
            if age_days > 365:
                res.notes.append(f"Last fsck was {age_days:.0f} days ago; a check is recommended.")
        self._check_backup_superblocks(res)
        return res

    def _limit(self) -> Optional[int]:
        try:
            return self.reader.size() - self.base
        except Exception:
            return None

    def _check_backup_superblocks(self, res: ExtResult) -> None:
        """Verify group-1 backup superblock; its presence is what makes repair possible."""
        if not (res.ro_compatible & RO_COMPAT_SPARSE_SUPER) and not res.blocks_per_group:
            return
        group_blocks = res.first_data_block + res.blocks_per_group
        backup_off = self.base + group_blocks * res.block_size + SUPERBLOCK_OFFSET
        try:
            backup = self.reader.read(backup_off, 128)
        except Exception:
            return
        if len(backup) >= 2 and struct.unpack("<H", backup[56:58])[0] == EXT_MAGIC:
            res.notes.append("Backup superblock in block group 1 verified — fsck -b 8193 recovery is possible.")


def analyze_ext(reader: DiskReader, base_offset: int = 0, volume_size: Optional[int] = None) -> ExtResult:
    return ExtAnalyzer(reader, base_offset, volume_size).analyze()
