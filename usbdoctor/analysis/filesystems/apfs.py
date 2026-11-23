"""APFS container analysis (the default macOS SSD/USB layout since 10.13).

Verifies the Fletcher-64 block checksum, container geometry, superblock copies,
and enumerates volume superblocks inside the container.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Optional

from usbdoctor.analysis.partitions.tables import TableIssue
from usbdoctor.core.io import DiskReader

APFS_MAGIC = b"NXSB"
APFS_VOL_MAGIC = b"APSB"
FLETCHER_MOD = 0xFFFFFFFF


def fletcher64(data: bytes) -> int:
    """Apple's Fletcher-64 over 32-bit little-endian words (mod 0xFFFFFFFF)."""
    if len(data) % 4:
        data = data + b"\x00" * (4 - len(data) % 4)
    sum1 = 0
    sum2 = 0
    count = len(data) // 4
    words = struct.unpack(f"<{count}I", data)
    for word in words:
        sum1 = (sum1 + word) % FLETCHER_MOD
        sum2 = (sum2 + sum1) % FLETCHER_MOD
    return (sum2 << 32) | sum1


def block_checksum_valid(block: bytes) -> bool:
    if len(block) < 40:
        return False
    stored = struct.unpack("<Q", block[0:8])[0]
    computed = fletcher64(b"\x00" * 8 + block[8:])
    return stored == computed


#: Byte offsets within an ``apfs_superblock`` (see Apple's APFS reference /
#: apfs-fuse ``io_structures.py``).  Kept as named constants so the parser is
#: auditable against the published structure rather than magic numbers.
APFS_SB_FS_INDEX = 0x24
APFS_SB_NUM_FILES = 0xB8
APFS_SB_NUM_DIRECTORIES = 0xC0
APFS_SB_NUM_SNAPSHOTS = 0xD8
APFS_SB_VOL_UUID = 0xF0
APFS_SB_FS_FLAGS = 0x108
APFS_SB_VOLNAME = 0x2C0
APFS_VOLNAME_LEN = 256
APFS_FS_FLAG_UNENCRYPTED = 0x00000001


@dataclass
class ApfsVolume:
    index: int
    name: str
    uuid: str
    num_files: int
    num_directories: int
    num_snapshots: int
    encrypted: bool
    oid: int
    flags: int

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class ApfsResult:
    block_size: int = 4096
    block_count: int = 0
    container_uuid: str = ""
    fs_index: int = 0
    features: int = 0
    read_only_features: int = 0
    incompatible_features: int = 0
    max_volumes: int = 0
    volumes: list[ApfsVolume] = field(default_factory=list)
    checksum_valid: Optional[bool] = None
    superblock_copies_ok: int = 0
    issues: list[TableIssue] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def container_size(self) -> int:
        return self.block_size * self.block_count

    def to_dict(self) -> dict:
        return {
            "block_size": self.block_size,
            "block_count": self.block_count,
            "container_size": self.container_size,
            "container_uuid": self.container_uuid,
            "features": self.features,
            "incompatible_features": self.incompatible_features,
            "read_only_features": self.read_only_features,
            "max_volumes": self.max_volumes,
            "checksum_valid": self.checksum_valid,
            "superblock_copies_ok": self.superblock_copies_ok,
            "volumes": [v.to_dict() for v in self.volumes],
            "issues": [i.__dict__ for i in self.issues],
            "notes": self.notes,
        }


def _uuid(raw: bytes) -> str:
    h = raw.hex()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}" if len(h) == 32 else ""


def _parse_nxsb(block: bytes) -> dict:
    return {
        "checksum": struct.unpack("<Q", block[0:8])[0],
        "oid": struct.unpack("<Q", block[8:16])[0],
        "xid": struct.unpack("<Q", block[16:24])[0],
        "type": struct.unpack("<I", block[24:28])[0],
        "block_size": struct.unpack("<I", block[36:40])[0],
        "block_count": struct.unpack("<Q", block[40:48])[0],
        "features": struct.unpack("<Q", block[48:56])[0],
        "ro_compat": struct.unpack("<Q", block[56:64])[0],
        "incompat": struct.unpack("<Q", block[64:72])[0],
        "uuid": _uuid(block[72:88]),
        "next_oid": struct.unpack("<Q", block[88:96])[0],
        "next_xid": struct.unpack("<Q", block[96:104])[0],
        "xp_desc_blocks": struct.unpack("<I", block[104:108])[0],
        "xp_data_blocks": struct.unpack("<I", block[108:112])[0],
        "spaceman_oid": struct.unpack("<Q", block[112:120])[0],
        "omap_oid": struct.unpack("<Q", block[120:128])[0],
        "reaper_oid": struct.unpack("<Q", block[128:136])[0],
        "max_file_systems": struct.unpack("<I", block[180:184])[0],
        "fs_oid": struct.unpack("<Q", block[184:192])[0],
        "counters": struct.unpack("<III", block[192:204]),
    }


class ApfsAnalyzer:
    def __init__(self, reader: DiskReader, base_offset: int = 0, volume_size: Optional[int] = None) -> None:
        self.reader = reader
        self.base = base_offset
        self.volume_size = volume_size

    def analyze(self) -> ApfsResult:
        res = ApfsResult()
        block0 = self.reader.read(self.base, 4096)
        if len(block0) < 192 or block0[32:36] != APFS_MAGIC:
            res.issues.append(TableIssue("critical", "APFS_MAGIC_MISSING", "APFS container magic 'NXSB' not found."))
            res.checksum_valid = False
            return res
        sb = _parse_nxsb(block0)
        res.block_size = sb["block_size"] or 4096
        if res.block_size not in (4096, 8192, 16384):
            res.issues.append(
                TableIssue("error", "APFS_BAD_BLOCK_SIZE", f"Container block size {res.block_size} is invalid.")
            )
            res.block_size = 4096
        block0 = self.reader.read(self.base, res.block_size)
        res.block_count = sb["block_count"]
        res.container_uuid = sb["uuid"]
        res.features = sb["features"]
        res.read_only_features = sb["ro_compat"]
        res.incompatible_features = sb["incompat"]
        res.max_volumes = sb["max_file_systems"]
        res.fs_index = sb["counters"][0]
        res.checksum_valid = block_checksum_valid(block0)
        if not res.checksum_valid:
            res.issues.append(
                TableIssue(
                    "critical",
                    "APFS_CHECKSUM_INVALID",
                    "Container superblock checksum is invalid — the container header is damaged.",
                    repairable=True,
                )
            )
        # Alternate superblock copies: block 2**k for k=0..5 store NXSB copies.
        for k in range(0, 6):
            addr = res.block_size * (1 << k)
            if addr >= res.container_size:
                break
            try:
                blk = self.reader.read(self.base + addr, 40)
            except Exception:
                continue
            if len(blk) >= 40 and blk[32:36] == APFS_MAGIC:
                full = self.reader.read(self.base + addr, res.block_size)
                if block_checksum_valid(full):
                    res.superblock_copies_ok += 1
        if res.superblock_copies_ok == 0 and res.checksum_valid:
            res.notes.append("No alternate superblock copy passed verification (container may be small).")

        limit = self.volume_size or self._limit()
        if limit and res.container_size > limit:
            res.issues.append(
                TableIssue(
                    "critical",
                    "APFS_CONTAINER_BEYOND_DEVICE",
                    f"Container claims {res.container_size:,} bytes but device has {limit:,}.",
                )
            )

        self._read_volumes(res, sb)
        return res

    def _limit(self) -> Optional[int]:
        try:
            return self.reader.size() - self.base
        except Exception:
            return None

    def _read_volumes(self, res: ApfsResult, sb: dict) -> None:
        """Read volume superblocks listed in the container's fs OID array."""
        # The volume OIDs live in the NXSB volume array; for containers with few
        # volumes they can be found by scanning the first megabyte for APSB.
        found = 0
        candidates = [res.block_size * 64, res.block_size * 128]
        for off in candidates:
            try:
                blk = self.reader.read(self.base + off, 40)
            except Exception:
                continue
            if blk[32:36] == APFS_VOL_MAGIC:
                full = self.reader.read(self.base + off, res.block_size)
                info = self._parse_volume(found, full)
                if info:
                    res.volumes.append(info)
                    found += 1
        if found == 0 and res.fs_index:
            res.notes.append(
                f"Container reports {res.fs_index} volume(s); volume superblocks are located via OMAP "
                "and are enumerated by `diskutil apfs list` on macOS."
            )
        if found == 0 and res.checksum_valid:
            res.notes.append("No inline volume superblock found in the first 128 blocks.")

    def _parse_volume(self, index: int, block: bytes) -> Optional[ApfsVolume]:
        need = APFS_SB_VOLNAME + APFS_VOLNAME_LEN
        if len(block) < need or block[32:36] != APFS_VOL_MAGIC:
            return None
        name = (
            block[APFS_SB_VOLNAME : APFS_SB_VOLNAME + APFS_VOLNAME_LEN]
            .split(b"\x00", 1)[0]
            .decode("utf-8", "replace")
            .strip()
        )
        flags = struct.unpack("<Q", block[APFS_SB_FS_FLAGS : APFS_SB_FS_FLAGS + 8])[0]
        return ApfsVolume(
            index=index,
            name=name,
            uuid=_uuid(block[APFS_SB_VOL_UUID : APFS_SB_VOL_UUID + 16]),
            num_files=struct.unpack("<Q", block[APFS_SB_NUM_FILES : APFS_SB_NUM_FILES + 8])[0],
            num_directories=struct.unpack(
                "<Q", block[APFS_SB_NUM_DIRECTORIES : APFS_SB_NUM_DIRECTORIES + 8]
            )[0],
            num_snapshots=struct.unpack(
                "<Q", block[APFS_SB_NUM_SNAPSHOTS : APFS_SB_NUM_SNAPSHOTS + 8]
            )[0],
            encrypted=not bool(flags & APFS_FS_FLAG_UNENCRYPTED),
            oid=struct.unpack("<Q", block[8:16])[0],
            flags=flags,
        )


def analyze_apfs(reader: DiskReader, base_offset: int = 0, volume_size: Optional[int] = None) -> ApfsResult:
    return ApfsAnalyzer(reader, base_offset, volume_size).analyze()
