"""FAT12/16/32 file-system analysis and repair planning.

The analyzer is read-only.  When it finds repairable damage it produces
byte-accurate :class:`Patch` objects; the repair engine applies them through the
guarded writer after user consent.  Every check below is performed on real
on-disk structures — nothing is inferred from a filename or guessed.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Optional

from usbdoctor.analysis.partitions.tables import TableIssue
from usbdoctor.core.io import DiskReader

BOOT_SIGNATURE = b"\x55\xaa"
EXTENDED_BOOT_SIG = 0x29


@dataclass
class Patch:
    """A verified, minimal write to fix one specific metadata problem."""

    offset: int  # absolute offset from the start of the analysed device
    data: bytes
    description: str
    reason_code: str

    def to_dict(self) -> dict:
        return {
            "offset": self.offset,
            "length": len(self.data),
            "description": self.description,
            "reason_code": self.reason_code,
        }


@dataclass
class DirEntry:
    name: str
    attributes: int
    first_cluster: int
    size: int
    is_dir: bool
    deleted: bool

    @property
    def is_volume_label(self) -> bool:
        return bool(self.attributes & 0x08) and not self.name.strip() == ""


@dataclass
class FatResult:
    variant: str  # fat12 | fat16 | fat32
    bytes_per_sector: int
    sectors_per_cluster: int
    reserved_sectors: int
    num_fats: int
    fat_size_sectors: int
    total_sectors: int
    root_cluster: int
    volume_label: str = ""
    volume_id: str = ""
    fsinfo_valid: Optional[bool] = None
    fsinfo_free_clusters: Optional[int] = None
    cluster_count: int = 0
    fat_copies_match: Optional[bool] = None
    fat_diff_bytes: int = 0
    issues: list[TableIssue] = field(default_factory=list)
    patches: list[Patch] = field(default_factory=list)
    boot: Optional[dict] = None
    notes: list[str] = field(default_factory=list)

    @property
    def cluster_size(self) -> int:
        return self.bytes_per_sector * self.sectors_per_cluster

    @property
    def volume_size(self) -> int:
        return self.total_sectors * self.bytes_per_sector

    def to_dict(self) -> dict:
        return {
            "variant": self.variant,
            "bytes_per_sector": self.bytes_per_sector,
            "sectors_per_cluster": self.sectors_per_cluster,
            "cluster_size": self.cluster_size,
            "reserved_sectors": self.reserved_sectors,
            "num_fats": self.num_fats,
            "fat_size_sectors": self.fat_size_sectors,
            "total_sectors": self.total_sectors,
            "root_cluster": self.root_cluster,
            "volume_label": self.volume_label,
            "volume_id": self.volume_id,
            "fsinfo_valid": self.fsinfo_valid,
            "fsinfo_free_clusters": self.fsinfo_free_clusters,
            "cluster_count": self.cluster_count,
            "fat_copies_match": self.fat_copies_match,
            "fat_diff_bytes": self.fat_diff_bytes,
            "issues": [i.__dict__ for i in self.issues],
            "patches": [p.to_dict() for p in self.patches],
            "notes": self.notes,
        }


def decode_label(raw: bytes) -> str:
    return raw.decode("cp437", "replace").rstrip("\x00 ").strip()


def parse_dirent(raw: bytes, *, variant: str) -> Optional[DirEntry]:
    if len(raw) < 32:
        return None
    first = raw[0]
    if first == 0x00:
        return None
    if first == 0xE5:
        deleted = True
        name_raw = raw[1:11]
    else:
        deleted = False
        name_raw = raw[0:11]
    if raw[11] & 0x3F == 0x0F:  # LFN entry: continuation of a long name
        return None
    attributes = raw[11]
    name = name_raw.decode("cp437", "replace")
    base = name[:8].rstrip()
    ext = name[8:].rstrip()
    full = f"{base}.{ext}" if ext else base
    first_cluster = struct.unpack("<H", raw[26:28])[0]
    if variant == "fat32":
        first_cluster |= struct.unpack("<H", raw[20:22])[0] << 16
    size = struct.unpack("<I", raw[28:32])[0]
    return DirEntry(
        name=full,
        attributes=attributes,
        first_cluster=first_cluster,
        size=size,
        is_dir=bool(attributes & 0x10),
        deleted=deleted,
    )


class FatAnalyzer:
    """Read-only FAT12/16/32 checker with repair-patch generation."""

    def __init__(self, reader: DiskReader, base_offset: int = 0, volume_size: Optional[int] = None) -> None:
        self.reader = reader
        self.base = base_offset
        self.volume_size = volume_size
        self.result: Optional[FatResult] = None

    # ------------------------------------------------------------------ main
    def analyze(self) -> FatResult:
        boot = self.reader.read(self.base, 512)
        res = self._parse_bpb(boot)
        self.result = res
        if res is None or res.variant == "invalid":
            assert res is not None
            return res
        self._check_capacity(res)
        self._check_boot_signature(boot, res)
        self._check_fsinfo(res)
        self._check_fat_copies(res)
        self._walk_root(res)
        return res

    # ----------------------------------------------------------------- BPB
    def _parse_bpb(self, boot: bytes) -> Optional[FatResult]:
        issues: list[TableIssue] = []
        if len(boot) < 512:
            return _invalid("BOOT_SECTOR_UNREADABLE", "The boot sector could not be read.")
        if boot[0] not in (0xEB, 0xE9):
            return _invalid("FAT_BAD_JUMP", "FAT boot sector jump instruction is invalid (not a FAT volume).")
        bytes_per_sector = struct.unpack("<H", boot[11:13])[0]
        sectors_per_cluster = boot[13]
        reserved = struct.unpack("<H", boot[14:16])[0]
        num_fats = boot[16]
        root_entries = struct.unpack("<H", boot[17:19])[0]
        total_sectors_16 = struct.unpack("<H", boot[19:21])[0]
        media = boot[21]
        fat_size_16 = struct.unpack("<H", boot[22:24])[0]
        total_sectors_32 = struct.unpack("<I", boot[32:36])[0]
        fat_size_32 = struct.unpack("<I", boot[36:40])[0]
        fs_type = boot[82:90].decode("ascii", "replace").strip()
        label = decode_label(boot[71:82])
        vol_id = struct.unpack("<I", boot[67:71])[0]

        total_sectors = total_sectors_16 or total_sectors_32
        fat_size = fat_size_32 or fat_size_16

        cluster_count = 0
        if bytes_per_sector and sectors_per_cluster and fat_size:
            data_start = reserved + num_fats * fat_size + (root_entries * 32 + bytes_per_sector - 1) // bytes_per_sector
            if total_sectors > data_start:
                cluster_count = (total_sectors - data_start) // sectors_per_cluster

        if fat_size_32:
            variant = "fat32"
        elif cluster_count < 4085:
            variant = "fat12"
        elif cluster_count < 65525:
            variant = "fat16"
        else:
            variant = "fat32"

        # ---- structural validations
        if bytes_per_sector not in (512, 1024, 2048, 4096):
            issues.append(
                TableIssue("error", "FAT_BAD_SECTOR_SIZE", f"Bytes per sector is invalid ({bytes_per_sector}).",
                           repairable=False)
            )
        if sectors_per_cluster == 0 or sectors_per_cluster & (sectors_per_cluster - 1):
            issues.append(
                TableIssue("error", "FAT_BAD_CLUSTER_SIZE",
                           f"Sectors per cluster must be a power of two (found {sectors_per_cluster}).",
                           repairable=False)
            )
        cluster_size = bytes_per_sector * max(1, sectors_per_cluster)
        if cluster_size > 64 * 1024:
            issues.append(
                TableIssue("warning", "FAT_CLUSTER_TOO_LARGE",
                           f"Cluster size {cluster_size} bytes exceeds 64 KiB; some systems cannot read it.")
            )
        if reserved == 0:
            issues.append(TableIssue("error", "FAT_NO_RESERVED", "Reserved sector count is zero.", repairable=True))
        if num_fats not in (1, 2):
            issues.append(
                TableIssue("error", "FAT_NUM_FATS", f"Number of FAT copies is {num_fats} (expected 1 or 2).",
                           repairable=True)
            )
        if total_sectors == 0:
            issues.append(TableIssue("critical", "FAT_ZERO_SIZE", "Volume reports zero total sectors."))
        if media not in (0xF0, 0xF8, 0xF9, 0xFA, 0xFB, 0xFC, 0xFD, 0xFE, 0xFF):
            issues.append(TableIssue("warning", "FAT_BAD_MEDIA", f"Media descriptor 0x{media:02X} is unusual."))

        boot_sig = boot[510:512]
        has_boot_sig = boot_sig == BOOT_SIGNATURE
        ext_sig = boot[66]

        res = FatResult(
            variant=variant,
            bytes_per_sector=bytes_per_sector or 512,
            sectors_per_cluster=sectors_per_cluster or 1,
            reserved_sectors=reserved,
            num_fats=num_fats,
            fat_size_sectors=fat_size,
            total_sectors=total_sectors,
            root_cluster=struct.unpack("<I", boot[44:48])[0] if variant == "fat32" else 0,
            volume_label=label,
            volume_id=f"{vol_id:08X}",
            cluster_count=cluster_count,
            issues=issues,
            boot={
                "oem": boot[3:11].decode("ascii", "replace").strip(),
                "fs_type_string": fs_type,
                "root_entries": root_entries,
                "hidden_sectors": struct.unpack("<I", boot[28:32])[0],
                "media": f"0x{media:02X}",
                "boot_signature": has_boot_sig,
                "extended_boot_signature": ext_sig == EXTENDED_BOOT_SIG,
            },
        )
        if not has_boot_sig:
            res.issues.append(
                TableIssue(
                    "critical",
                    "FAT_BOOT_SIGNATURE_MISSING",
                    "Boot sector signature 0x55AA is missing — the OS will treat this volume as RAW.",
                    repairable=True,
                )
            )
            res.issues.append(
                TableIssue(
                    "critical",
                    "FAT_BACKUP_REQUIRED",
                    "Primary boot sector is damaged; a backup boot sector may be present on FAT32.",
                    repairable=True,
                )
            )
        if ext_sig != EXTENDED_BOOT_SIG:
            res.notes.append("Extended boot signature (0x29) is absent; volume label/serial checks skipped.")
        if variant == "fat32" and fs_type.upper().startswith("FAT") and "FAT32" not in fs_type.upper():
            res.issues.append(
                TableIssue("warning", "FAT_TYPE_STRING_MISMATCH",
                           f"File-system type string says '{fs_type}' but geometry indicates FAT32.")
            )
        # FAT32 root cluster must be a valid data cluster
        if variant == "fat32":
            root = res.root_cluster
            if root < 2 or (cluster_count and root > cluster_count + 1):
                res.issues.append(
                    TableIssue("error", "FAT32_BAD_ROOT_CLUSTER",
                               f"Root directory cluster {root} is outside the data area.", repairable=True)
                )
        return res

    # ------------------------------------------------------------- capacity
    def _check_capacity(self, res: FatResult) -> None:
        limit = None
        if self.volume_size:
            limit = self.volume_size
        else:
            try:
                limit = self.reader.size() - self.base
            except Exception:
                limit = None
        if limit and res.volume_size > limit:
            res.issues.append(
                TableIssue(
                    "critical",
                    "FAT_VOLUME_BEYOND_DEVICE",
                    f"File system claims {res.volume_size:,} bytes but only {limit:,} are addressable. "
                    "This indicates a counterfeit-capacity USB drive or a corrupt boot sector.",
                    repairable=False,
                    detail={"claimed": res.volume_size, "device": limit},
                )
            )
        elif limit and res.volume_size < limit * 0.5 and limit > 0:
            res.notes.append(
                f"Volume uses {res.volume_size / limit:.0%} of the device — normal for a single-partition layout."
            )

    def _check_boot_signature(self, boot: bytes, res: FatResult) -> None:
        if res.variant != "fat32":
            return
        backup_sector = struct.unpack("<H", boot[50:52])[0]
        if backup_sector in (0, 0xFFFF) and res.boot and res.boot.get("boot_signature"):
            return
        if not backup_sector:
            res.issues.append(
                TableIssue("warning", "FAT32_BACKUP_SECTOR_ZERO", "Backup boot sector pointer is zero.")
            )
            return
        backup = self.reader.read(self.base + backup_sector * res.bytes_per_sector, 512)
        if backup[510:512] != BOOT_SIGNATURE:
            res.issues.append(
                TableIssue(
                    "error",
                    "FAT32_BACKUP_BOOT_INVALID",
                    "The backup boot sector is not a valid FAT boot sector.",
                )
            )
            return
        res.notes.append(f"Backup boot sector at sector {backup_sector} verified (0x55AA present).")
        primary_ok = boot[510:512] == BOOT_SIGNATURE
        if not primary_ok and boot[0] not in (0xEB, 0xE9):
            # Full restore possible: copy the valid backup over the primary.
            res.patches.append(
                Patch(
                    offset=self.base,
                    data=backup,
                    description="Restore the FAT32 boot sector from its verified backup copy",
                    reason_code="FAT32_RESTORE_PRIMARY_BOOT",
                )
            )
            res.issues.append(
                TableIssue(
                    "info",
                    "FAT32_BOOT_RESTORE_AVAILABLE",
                    "A complete, valid backup boot sector was found and can restore the primary.",
                    repairable=True,
                )
            )

    def _check_fsinfo(self, res: FatResult) -> None:
        if res.variant != "fat32":
            return
        boot = self.reader.read(self.base, 512)
        fsinfo_sector = struct.unpack("<H", boot[48:50])[0]
        if fsinfo_sector in (0, 0xFFFF):
            res.issues.append(
                TableIssue("warning", "FAT32_NO_FSINFO", "FSInfo sector pointer is not set.", repairable=True)
            )
            res.fsinfo_valid = False
            return
        sector = self.reader.read(self.base + fsinfo_sector * res.bytes_per_sector, res.bytes_per_sector or 512)
        if len(sector) < 512:
            res.fsinfo_valid = False
            return
        lead_sig = struct.unpack("<I", sector[0:4])[0]
        struct_sig = struct.unpack("<I", sector[484:488])[0]
        trail_sig = struct.unpack("<I", sector[508:512])[0]
        free_clusters = struct.unpack("<I", sector[488:492])[0]
        valid = lead_sig == 0x41615252 and struct_sig == 0x61417272 and trail_sig == 0xAA550000
        res.fsinfo_valid = valid
        res.fsinfo_free_clusters = None if free_clusters == 0xFFFFFFFF else free_clusters
        if not valid:
            res.issues.append(
                TableIssue(
                    "warning",
                    "FAT32_FSINFO_INVALID",
                    "FSInfo signatures are damaged — free-space reporting may be wrong until repaired.",
                    repairable=True,
                )
            )
            if res.fsinfo_free_clusters is not None and res.cluster_count and res.fsinfo_free_clusters > res.cluster_count:
                res.issues.append(
                    TableIssue(
                        "warning",
                        "FAT32_FSINFO_IMPLAUSIBLE",
                        f"FSInfo claims {res.fsinfo_free_clusters:,} free clusters but the volume only has "
                        f"{res.cluster_count:,}.",
                        repairable=True,
                    )
                )

    def _check_fat_copies(self, res: FatResult) -> None:
        if res.num_fats != 2 or not res.fat_size_sectors:
            return
        fat_bytes = min(res.fat_size_sectors * res.bytes_per_sector, 4 << 20)
        first = self.reader.read(
            self.base + res.reserved_sectors * res.bytes_per_sector, fat_bytes
        )
        second = self.reader.read(
            self.base + (res.reserved_sectors + res.fat_size_sectors) * res.bytes_per_sector, fat_bytes
        )
        if len(first) != len(second) or not first:
            res.notes.append("FAT copies could not be compared (short read).")
            return
        diffs = sum(1 for a, b in zip(first, second) if a != b)
        res.fat_copies_match = diffs == 0
        res.fat_diff_bytes = diffs
        if diffs:
            ratio = diffs / len(first)
            res.issues.append(
                TableIssue(
                    "warning" if ratio < 0.02 else "error",
                    "FAT_COPIES_DIFFER",
                    f"FAT #1 and FAT #2 differ in {diffs:,} bytes ({ratio:.2%} of the checked area).",
                    repairable=ratio < 0.25,
                    detail={"diff_bytes": diffs, "checked_bytes": len(first)},
                )
            )
            if ratio < 0.25:
                res.patches.append(
                    Patch(
                        offset=self.base + (res.reserved_sectors + res.fat_size_sectors) * res.bytes_per_sector,
                        data=first,
                        description="Rewrite the second FAT copy from the first (mirror repair)",
                        reason_code="FAT_MIRROR_SECOND",
                    )
                )

    def _walk_root(self, res: FatResult) -> None:
        """Read the root directory, detect obviously corrupt entries/loops."""
        try:
            if res.variant == "fat32":
                entries = self._read_fat32_chain(res, res.root_cluster, max_bytes=1 << 20)
            else:
                root_entries = struct.unpack("<H", self.reader.read(self.base + 17, 2))[0]
                root_start = (res.reserved_sectors + res.num_fats * res.fat_size_sectors) * res.bytes_per_sector
                entries = self.reader.read(self.base + root_start, min(root_entries * 32, 1 << 20))
        except Exception as exc:  # a read failure here is itself a finding
            res.issues.append(
                TableIssue("error", "FAT_ROOT_UNREADABLE", f"Root directory could not be read: {exc}")
            )
            return
        if not entries:
            res.issues.append(TableIssue("error", "FAT_ROOT_EMPTY", "Root directory is empty or unreadable."))
            return
        count = 0
        bad = 0
        names: list[str] = []
        valid_names = b"ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789$%'-_@~`!(){}^#&"
        for off in range(0, len(entries), 32):
            raw = entries[off : off + 32]
            if len(raw) < 32 or raw[0] == 0x00:
                break
            if raw[11] & 0x3F == 0x0F:
                continue
            if raw[0] == 0xE5:
                count += 1
                continue
            if raw[11] & 0x3F == 0x3F:
                bad += 1
                continue
            for ch in raw[0:11]:
                if ch not in valid_names and ch != 0x20:
                    bad += 1
                    break
            count += 1
            if len(names) < 40:
                entry = parse_dirent(raw, variant=res.variant)
                if entry and not entry.is_volume_label:
                    names.append(entry.name + ("/" if entry.is_dir else ""))
        res.boot = res.boot or {}
        res.boot["root_entries_observed"] = count
        res.boot["root_names_sample"] = names
        if count == 0:
            res.issues.append(TableIssue("warning", "FAT_ROOT_NO_ENTRIES", "Root directory contains no entries."))
        if bad and count and bad / max(1, count) > 0.05:
            res.issues.append(
                TableIssue(
                    "warning",
                    "FAT_ROOT_BAD_ENTRIES",
                    f"{bad} of {count} root-directory entries have invalid name bytes.",
                    detail={"bad": bad, "count": count},
                )
            )

    def _read_fat32_chain(self, res: FatResult, start_cluster: int, *, max_bytes: int) -> bytes:
        if start_cluster < 2:
            return b""
        cluster = res.cluster_size or 4096
        fat_offset = self.base + res.reserved_sectors * res.bytes_per_sector
        data_offset = self.base + (res.reserved_sectors + res.num_fats * res.fat_size_sectors) * res.bytes_per_sector
        chunks: list[bytes] = []
        seen: set[int] = set()
        current = start_cluster
        total = 0
        while 2 <= current <= res.cluster_count + 1 and len(chunks) < max_bytes // max(1, cluster):
            if current in seen:
                res.issues.append(
                    TableIssue(
                        "error",
                        "FAT_CHAIN_LOOP",
                        f"Cluster chain loops at cluster {current} — directory is corrupt.",
                    )
                )
                break
            seen.add(current)
            entry_off = fat_offset + current * 4
            next_raw = struct.unpack("<I", self.reader.read(entry_off, 4))[0] & 0x0FFFFFFF
            data = self.reader.read(data_offset + (current - 2) * cluster, cluster)
            if not data:
                break
            chunks.append(data)
            total += len(data)
            if next_raw >= 0x0FFFFFF8:
                break
            current = next_raw
        return b"".join(chunks)


def _invalid(code: str, message: str) -> FatResult:
    res = FatResult(
        variant="invalid",
        bytes_per_sector=512,
        sectors_per_cluster=1,
        reserved_sectors=0,
        num_fats=0,
        fat_size_sectors=0,
        total_sectors=0,
        root_cluster=0,
    )
    res.issues.append(TableIssue("critical", code, message))
    return res


def analyze_fat(reader: DiskReader, base_offset: int = 0, volume_size: Optional[int] = None) -> FatResult:
    return FatAnalyzer(reader, base_offset, volume_size).analyze()
