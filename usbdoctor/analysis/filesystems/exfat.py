"""exFAT analysis: boot checksums, backup boot, dirty flags, root directory.

exFAT is the default format for SDXC cards and most modern USB drives above
32 GB, so its metadata is where "RAW card" problems usually live.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Optional

from usbdoctor.analysis.filesystems.fat import Patch
from usbdoctor.analysis.partitions.tables import TableIssue
from usbdoctor.core.io import DiskReader

EXFAT_OEM = b"EXFAT   "
VOLUME_FLAG_DIRTY = 0x0002
VOLUME_FLAG_MEDIA_FAILURE = 0x0004
VOLUME_FLAG_CLEAR_TO_ZERO = 0x0008


def boot_checksum(sectors: bytes) -> int:
    """exFAT boot-region checksum (Microsoft spec: 32-bit ror-xor, skipping 106,107,112)."""
    checksum = 0
    for index, byte in enumerate(sectors):
        if index in (106, 107, 112):
            continue
        checksum = ((checksum << 31) | (checksum >> 1)) & 0xFFFFFFFF
        checksum = (checksum + byte) & 0xFFFFFFFF
    return checksum


@dataclass
class ExfatResult:
    bytes_per_sector: int = 512
    sectors_per_cluster: int = 8
    volume_length_sectors: int = 0
    fat_offset_sectors: int = 0
    fat_length_sectors: int = 0
    cluster_heap_offset: int = 0
    cluster_count: int = 0
    root_cluster: int = 0
    volume_serial: int = 0
    fs_revision: str = ""
    volume_flags: int = 0
    num_fats: int = 1
    percent_in_use: int = 0
    boot_checksum_valid: Optional[bool] = None
    backup_boot_present: bool = False
    backup_boot_matches: Optional[bool] = None
    boot_region_restorable: bool = False
    root_entries: int = 0
    root_files: int = 0
    root_dirs: int = 0
    issues: list[TableIssue] = field(default_factory=list)
    patches: list[Patch] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def cluster_size(self) -> int:
        return self.bytes_per_sector * self.sectors_per_cluster

    @property
    def dirty(self) -> bool:
        return bool(self.volume_flags & VOLUME_FLAG_DIRTY)

    @property
    def media_failure(self) -> bool:
        return bool(self.volume_flags & VOLUME_FLAG_MEDIA_FAILURE)

    @property
    def volume_size(self) -> int:
        return self.volume_length_sectors * self.bytes_per_sector

    def to_dict(self) -> dict:
        return {
            "bytes_per_sector": self.bytes_per_sector,
            "sectors_per_cluster": self.sectors_per_cluster,
            "cluster_size": self.cluster_size,
            "volume_length_sectors": self.volume_length_sectors,
            "fat_offset_sectors": self.fat_offset_sectors,
            "fat_length_sectors": self.fat_length_sectors,
            "cluster_heap_offset": self.cluster_heap_offset,
            "cluster_count": self.cluster_count,
            "root_cluster": self.root_cluster,
            "volume_serial": f"{self.volume_serial:08X}",
            "fs_revision": self.fs_revision,
            "volume_flags": self.volume_flags,
            "dirty": self.dirty,
            "media_failure": self.media_failure,
            "num_fats": self.num_fats,
            "percent_in_use": self.percent_in_use,
            "boot_checksum_valid": self.boot_checksum_valid,
            "backup_boot_present": self.backup_boot_present,
            "backup_boot_matches": self.backup_boot_matches,
            "root_entries": self.root_entries,
            "root_files": self.root_files,
            "root_dirs": self.root_dirs,
            "issues": [i.__dict__ for i in self.issues],
            "patches": [p.to_dict() for p in self.patches],
            "notes": self.notes,
        }


class ExfatAnalyzer:
    def __init__(self, reader: DiskReader, base_offset: int = 0, volume_size: Optional[int] = None) -> None:
        self.reader = reader
        self.base = base_offset
        self.volume_size = volume_size

    def analyze(self) -> ExfatResult:
        res = ExfatResult()
        boot_region = self.reader.read(self.base, 12 * 512)
        if len(boot_region) < 12 * 512 or boot_region[3:11] != EXFAT_OEM:
            res.issues.append(
                TableIssue("critical", "EXFAT_SIGNATURE_MISSING", "exFAT OEM signature 'EXFAT   ' not found.")
            )
            res.boot_checksum_valid = False
            return res
        boot = boot_region[:512]
        res.bytes_per_sector = 1 << boot[108] if boot[108] <= 12 else 512
        res.sectors_per_cluster = 1 << boot[109] if boot[109] <= 25 else 8
        res.fat_offset_sectors = struct.unpack("<I", boot[80:84])[0]
        res.fat_length_sectors = struct.unpack("<I", boot[84:88])[0]
        res.cluster_heap_offset = struct.unpack("<I", boot[88:92])[0]
        res.cluster_count = struct.unpack("<I", boot[92:96])[0]
        res.root_cluster = struct.unpack("<I", boot[96:100])[0]
        res.volume_serial = struct.unpack("<I", boot[100:104])[0]
        major = boot[104]
        minor = boot[105]
        res.fs_revision = f"{major}.{minor}"
        res.volume_flags = struct.unpack("<H", boot[106:108])[0]
        res.num_fats = boot[110]
        res.percent_in_use = boot[112]
        res.volume_length_sectors = struct.unpack("<Q", boot[72:80])[0]

        # --- boot region checksum (sectors 0..10, field at 5632..5635)
        checksum_sector = boot_region[11 * 512 : 12 * 512]
        stored = struct.unpack("<I", checksum_sector[0:4])[0]
        computed = boot_checksum(boot_region[: 11 * 512])
        res.boot_checksum_valid = stored == computed
        if not res.boot_checksum_valid:
            res.issues.append(
                TableIssue(
                    "error",
                    "EXFAT_BOOT_CHECKSUM",
                    "Boot region checksum mismatch — the boot sector is damaged or partially overwritten.",
                    repairable=True,
                )
            )

        # --- geometry sanity
        if res.bytes_per_sector not in (512, 1024, 2048, 4096):
            res.issues.append(
                TableIssue("error", "EXFAT_BAD_SECTOR_SIZE", f"Bytes-per-sector shift yields {res.bytes_per_sector}.")
            )
        if res.num_fats not in (1, 2):
            res.issues.append(
                TableIssue("error", "EXFAT_BAD_FAT_COUNT", f"Number of FATs is {res.num_fats}.")
            )
        if res.root_cluster < 2 or (res.cluster_count and res.root_cluster > res.cluster_count + 1):
            res.issues.append(
                TableIssue("error", "EXFAT_BAD_ROOT_CLUSTER", f"Root cluster {res.root_cluster} is out of range.",
                           repairable=True)
            )
        if res.cluster_count == 0:
            res.issues.append(TableIssue("critical", "EXFAT_ZERO_CLUSTERS", "Cluster count is zero."))
        # volume length vs device
        limit = self.volume_size or self._device_limit()
        if limit and res.volume_size > limit:
            res.issues.append(
                TableIssue(
                    "critical",
                    "EXFAT_VOLUME_BEYOND_DEVICE",
                    f"Volume length {res.volume_size:,} bytes exceeds the device ({limit:,}).",
                    detail={"claimed": res.volume_size, "device": limit},
                )
            )
        # percent-in-use plausibility
        if res.percent_in_use > 100:
            res.issues.append(
                TableIssue("warning", "EXFAT_BAD_PERCENT", f"Percent-in-use field is {res.percent_in_use}%.",
                           repairable=True)
            )

        # --- volume flags
        if res.dirty:
            res.issues.append(
                TableIssue(
                    "warning",
                    "EXFAT_DIRTY",
                    "The volume was not unmounted cleanly (dirty flag set). Metadata may need repair.",
                    repairable=True,
                )
            )
        if res.media_failure:
            res.issues.append(
                TableIssue(
                    "error",
                    "EXFAT_MEDIA_FAILURE",
                    "The card itself recorded a media failure — the OS saw I/O errors on this volume.",
                )
            )

        # --- backup boot region at sectors 12..23
        backup = self.reader.read(self.base + 12 * 512, 12 * 512)
        if len(backup) >= 12 * 512 and backup[3:11] == EXFAT_OEM:
            res.backup_boot_present = True
            res.backup_boot_matches = backup == boot_region
            backup_checksum = struct.unpack("<I", backup[11 * 512 : 11 * 512 + 4])[0]
            backup_valid = backup_checksum == boot_checksum(backup[: 11 * 512])
            if not res.boot_checksum_valid and backup_valid:
                res.boot_region_restorable = True
                res.patches.append(
                    Patch(
                        offset=self.base,
                        data=backup,
                        description="Restore the exFAT boot region from the verified backup copy",
                        reason_code="EXFAT_RESTORE_BOOT_REGION",
                    )
                )
            if res.boot_checksum_valid and not res.backup_boot_matches:
                res.patches.append(
                    Patch(
                        offset=self.base + 12 * 512,
                        data=boot_region,
                        description="Refresh the stale exFAT backup boot region from the primary",
                        reason_code="EXFAT_REFRESH_BACKUP",
                    )
                )
        else:
            res.issues.append(
                TableIssue("warning", "EXFAT_NO_BACKUP_BOOT", "Backup boot region is missing or invalid.",
                           repairable=True)
            )

        self._scan_root(res)
        return res

    def _device_limit(self) -> Optional[int]:
        try:
            return self.reader.size() - self.base
        except Exception:
            return None

    def _scan_root(self, res: ExfatResult) -> None:
        cluster = res.cluster_size or 4096
        if cluster <= 0 or cluster > 32 << 20:
            return
        data_offset = self.base + res.cluster_heap_offset * res.bytes_per_sector
        current = res.root_cluster
        seen: set[int] = set()
        scanned = 0
        entry = res.root_cluster
        while 2 <= entry <= res.cluster_count + 1 and len(seen) < 512:
            if entry in seen:
                res.issues.append(
                    TableIssue("error", "EXFAT_ROOT_LOOP", f"Directory chain loops at cluster {entry}.")
                )
                break
            seen.add(entry)
            try:
                block = self.reader.read(data_offset + (entry - 2) * cluster, cluster)
            except Exception as exc:
                res.issues.append(
                    TableIssue("error", "EXFAT_ROOT_UNREADABLE", f"Root directory read failed: {exc}")
                )
                break
            if not block:
                break
            for off in range(0, len(block), 32):
                rec = block[off : off + 32]
                if len(rec) < 32 or rec[0] == 0x00:
                    return
                if rec[0] & 0x80:
                    res.root_entries += 1
                    if rec[0] == 0x85:
                        res.root_files += 1
                    elif rec[0] == 0x83:
                        res.root_dirs += 1
            scanned += 1
            fat_entry_off = (
                self.base
                + res.fat_offset_sectors * res.bytes_per_sector
                + (entry * 4)
            )
            nxt = struct.unpack("<I", self.reader.read(fat_entry_off, 4) or b"\xff\xff\xff\xff")[0]
            if nxt >= 0xFFFFFFF8 or nxt == 0:
                break
            entry = nxt
        if res.root_entries == 0:
            res.issues.append(
                TableIssue("warning", "EXFAT_ROOT_EMPTY", "Root directory contains no file entries.")
            )


def analyze_exfat(reader: DiskReader, base_offset: int = 0, volume_size: Optional[int] = None) -> ExfatResult:
    return ExfatAnalyzer(reader, base_offset, volume_size).analyze()
