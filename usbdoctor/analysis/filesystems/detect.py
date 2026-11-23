"""File-system identification from raw bytes plus the cross-format dispatcher.

``sniff`` is the workhorse used by the RAW-volume detector: when the partition
table reports no file system, we look for the actual on-disk metadata and tell
the user what is *really* there (e.g. "the partition table says RAW, but a
complete FAT32 boot sector is intact — data is recoverable").
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from usbdoctor.analysis.filesystems.apfs import analyze_apfs
from usbdoctor.analysis.filesystems.exfat import analyze_exfat
from usbdoctor.analysis.filesystems.extfs import analyze_ext
from usbdoctor.analysis.filesystems.fat import analyze_fat
from usbdoctor.analysis.filesystems.hfs import analyze_hfs
from usbdoctor.analysis.filesystems.ntfs import analyze_ntfs
from usbdoctor.analysis.partitions.tables import TableIssue
from usbdoctor.core.io import DiskReader

SUPPORTED = ("fat12", "fat16", "fat32", "exfat", "ntfs", "apfs", "hfs+", "ext2", "ext3", "ext4")


@dataclass
class SniffResult:
    filesystem: str  # canonical lowercase name or "unknown"
    confidence: str  # certain | likely | none
    evidence: list[str]
    volume_size: Optional[int] = None


def sniff(boot: bytes) -> SniffResult:
    """Classify a volume from its first 512 bytes (no I/O)."""
    if len(boot) < 512:
        return SniffResult("unknown", "none", ["boot sector unreadable"])
    try:
        if boot[3:11] == b"NTFS    ":
            return SniffResult("ntfs", "certain", ["OEM string 'NTFS    ' at offset 3"])
        if boot[3:11] == b"EXFAT   ":
            return SniffResult("exfat", "certain", ["OEM string 'EXFAT   ' at offset 3"])
        if boot[0] in (0xEB, 0xE9) and boot[54:57] in (b"FAT",) or (
            boot[0] in (0xEB, 0xE9) and boot[82:85] == b"FAT"
        ):
            name = boot[82:90].decode("ascii", "replace").strip()
            if name.startswith("FAT32"):
                return SniffResult("fat32", "certain", ["FAT32 type string in extended boot record"])
            if name.startswith("FAT16"):
                return SniffResult("fat16", "certain", ["FAT16 type string in extended boot record"])
            if name.startswith("FAT12"):
                return SniffResult("fat12", "certain", ["FAT12 type string in extended boot record"])
            if name.startswith("FAT"):
                return SniffResult(name.lower(), "likely", [f"FAT type string '{name}'"])
        if boot[32:36] == b"NXSB":
            return SniffResult("apfs", "certain", ["APFS container magic 'NXSB'"])
        if boot[0:2] in (b"H+", b"HX"):
            return SniffResult("hfs+", "certain", ["HFS+ volume header signature"])
        if boot[1024:1024 + 2] and boot[1080:1082] == b"\x53\xef":
            return SniffResult("ext2/3/4", "certain", ["ext magic 0xEF53 at offset 1080"])
        if boot[510:512] == b"\x55\xaa" and boot[0] in (0xEB, 0xE9):
            # Plausible FAT whose type string was wiped: check BPB geometry.
            if boot[11:13] in (b"\x00\x02", b"\x00\x04", b"\x00\x08", b"\x00\x10") and 0 < boot[13] <= 128:
                return SniffResult("fat?", "likely", ["valid BPB geometry but FAT type string erased"])
        if boot[0:2] == b"PM":
            return SniffResult("apm", "certain", ["Apple Partition Map signature"])
    except IndexError:  # pragma: no cover
        pass
    return SniffResult("unknown", "none", ["no recognised file-system signature"])


def sniff_extended(reader: DiskReader, base_offset: int = 0) -> SniffResult:
    """Like :func:`sniff` but also probes the ext superblock at offset 1024."""
    boot = reader.read(base_offset, 512)
    result = sniff(boot)
    if result.filesystem != "unknown":
        return result
    try:
        ext_sb = reader.read(base_offset + 1024, 64)
        if len(ext_sb) >= 64 and ext_sb[56:58] == b"\x53\xef":
            return SniffResult("ext2/3/4", "certain", ["ext superblock magic at offset 1080"], None)
    except Exception:
        pass
    return result


@dataclass
class FullAnalysis:
    filesystem: str
    detail: object  # typed per-format result object
    issues: list[TableIssue]
    notes: list[str]

    def to_dict(self) -> dict:
        return {
            "filesystem": self.filesystem,
            "issues": [i.__dict__ for i in self.issues],
            "notes": self.notes,
            "detail": self.detail.to_dict() if hasattr(self.detail, "to_dict") else str(self.detail),
        }


@dataclass
class BootCandidate:
    """A boot-sector signature found somewhere other than the logical start."""

    offset: int
    filesystem: str
    confidence: str
    evidence: list[str]


#: Fixed backup-boot locations mandated by each file-system spec.
_FAT32_BACKUP_SECTOR = 6
_EXFAT_BACKUP_SECTOR = 12
#: Places a whole volume can start when the partition entry is wrong/absent.
_PROBE_OFFSETS = (
    1024,
    2048,
    4096,
    8192,
    16 * 1024,
    32 * 1024,
    64 * 1024,
    128 * 1024,
    256 * 1024,
    512 * 1024,
    1024 * 1024,
)


def probe_boot_signatures(
    reader: DiskReader,
    base_offset: int = 0,
    *,
    sector_size: int = 512,
) -> list[BootCandidate]:
    """Search for boot sectors when the volume's first sector is unreadable.

    This is the technique that turns "RAW / unrecognised" into an actionable
    diagnosis: on FAT32 the backup boot sector lives at sector 6, on exFAT at
    sector 12, and a wrong partition offset shows up as a valid boot sector at a
    round offset such as 1 MiB.
    """
    candidates: list[BootCandidate] = []
    checked: set[int] = set()

    def probe(offset: int, kind: str) -> None:
        if offset in checked:
            return
        checked.add(offset)
        try:
            data = reader.read(base_offset + offset, 512)
        except Exception:
            return
        if len(data) < 512:
            return
        found = sniff(data)
        if found.filesystem in ("unknown", "fat?", "apm"):
            return
        candidates.append(
            BootCandidate(
                offset=offset,
                filesystem=found.filesystem,
                confidence=found.confidence,
                evidence=[f"{kind}: " + "; ".join(found.evidence)],
            )
        )

    probe(_FAT32_BACKUP_SECTOR * sector_size, f"backup boot sector (sector {_FAT32_BACKUP_SECTOR})")
    probe(_EXFAT_BACKUP_SECTOR * sector_size, f"backup boot region (sector {_EXFAT_BACKUP_SECTOR})")
    for offset in _PROBE_OFFSETS:
        probe(offset, f"volume start at offset {offset:,}")
    candidates.sort(key=lambda c: c.offset)
    return candidates


def analyze_volume(
    reader: DiskReader,
    base_offset: int = 0,
    *,
    volume_size: Optional[int] = None,
    expected: Optional[str] = None,
    allow_probe: bool = True,
) -> FullAnalysis:
    """Run the full structural analysis for whichever file system is present.

    ``expected`` (as reported by the OS) is used only to prefer a parser; the
    authoritative decision always comes from the on-disk bytes.
    """
    detected = sniff_extended(reader, base_offset)
    fs = detected.filesystem

    def result(name: str, objs: object) -> FullAnalysis:
        detail = objs
        issues = list(getattr(detail, "issues", []))
        notes = list(getattr(detail, "notes", []))
        return FullAnalysis(filesystem=name, detail=detail, issues=issues, notes=notes)

    if fs == "ntfs":
        return result("ntfs", analyze_ntfs(reader, base_offset, volume_size))
    if fs == "exfat":
        return result("exfat", analyze_exfat(reader, base_offset, volume_size))
    if fs.startswith("fat"):
        fat = analyze_fat(reader, base_offset, volume_size)
        name = fat.variant if fat.variant != "invalid" else "fat"
        return result(name, fat)
    if fs == "apfs":
        return result("apfs", analyze_apfs(reader, base_offset, volume_size))
    if fs == "hfs+":
        return result("hfs+", analyze_hfs(reader, base_offset, volume_size))
    if fs.startswith("ext"):
        ext = analyze_ext(reader, base_offset, volume_size)
        return result(ext.fs_name, ext)

    # Unknown at the logical start: look for survivors before declaring RAW.
    boot = reader.read(base_offset, 512)
    zeroes = boot.count(0)
    issues: list[TableIssue] = []
    notes: list[str] = []
    candidates: list[BootCandidate] = []
    if allow_probe and len(boot) == 512:
        candidates = probe_boot_signatures(reader, base_offset, sector_size=reader.sector_size)
    detail: dict = {
        "sniff": detected.evidence,
        "probe_candidates": [c.__dict__ for c in candidates],
    }
    issues.extend(_candidate_restore(reader, base_offset, candidates, detail))

    if not candidates:
        if len(boot) == 512 and zeroes == 512:
            issues.append(
                TableIssue(
                    "warning",
                    "FS_BLANK",
                    "Boot area is all zeroes — this volume is unformatted or was wiped.",
                )
            )
        elif len(boot) == 512 and (boot[0:3] in (b"\x00\x00\x00", b"\xff\xff\xff") or zeroes > 480):
            issues.append(
                TableIssue(
                    "critical",
                    "FS_WIPED",
                    "Boot area is blank/garbage — the file system has been destroyed or the controller "
                    "is returning zeros (a common sign of a failing USB bridge).",
                )
            )
        else:
            issues.append(
                TableIssue(
                    "warning",
                    "FS_UNKNOWN",
                    "No supported file-system signature found — RAW volume or proprietary layout.",
                )
            )
    return FullAnalysis(filesystem="unknown", detail=detail, issues=issues, notes=notes)


def _candidate_restore(
    reader: DiskReader,
    base_offset: int,
    candidates: list[BootCandidate],
    detail: dict,
) -> list[TableIssue]:
    """Turn discovered backup boot sectors into a verified, repairable finding."""
    issues: list[TableIssue] = []
    from usbdoctor.analysis.filesystems.fat import Patch  # local import: avoid cycle

    sector = reader.sector_size or 512
    for cand in candidates:
        if cand.offset == _FAT32_BACKUP_SECTOR * sector and cand.filesystem.startswith("fat"):
            backup = reader.read(base_offset + cand.offset, sector)
            if len(backup) < sector or backup[510:512] != b"\x55\xaa":
                continue
            detail.setdefault("patches", []).append(
                Patch(
                    offset=base_offset,
                    data=backup,
                    description="Restore the destroyed FAT32 boot sector from backup sector 6",
                    reason_code="FAT32_RESTORE_PRIMARY_BOOT",
                ).to_dict()
            )
            detail["detected_filesystem"] = cand.filesystem
            issues.append(
                TableIssue(
                    "critical",
                    "FAT_BOOT_SECTOR_DESTROYED",
                    "The primary boot sector is destroyed, but a complete FAT32 backup boot sector "
                    "exists at sector 6. File data and directory structure are intact.",
                    repairable=True,
                    detail={"backup_offset": cand.offset},
                )
            )
            return issues
        if cand.offset == _EXFAT_BACKUP_SECTOR * sector and cand.filesystem == "exfat":
            backup_region = reader.read(base_offset + cand.offset, 12 * sector)
            if len(backup_region) < 12 * sector:
                continue
            detail.setdefault("patches", []).append(
                Patch(
                    offset=base_offset,
                    data=backup_region,
                    description="Restore the destroyed exFAT boot region from its backup (sector 12)",
                    reason_code="EXFAT_RESTORE_BOOT_REGION",
                ).to_dict()
            )
            detail["detected_filesystem"] = "exfat"
            issues.append(
                TableIssue(
                    "critical",
                    "EXFAT_BOOT_REGION_DESTROYED",
                    "The primary exFAT boot region is destroyed but the complete backup region at "
                    "sector 12 is valid and can restore it.",
                    repairable=True,
                    detail={"backup_offset": cand.offset},
                )
            )
            return issues
    # A valid boot sector at a large round offset means the *partition entry*
    # start is wrong, not the volume itself.  Report it, never auto-apply.
    for cand in candidates:
        if cand.offset >= 64 * 1024:
            detail["detected_filesystem"] = cand.filesystem
            issues.append(
                TableIssue(
                    "error",
                    "VOLUME_START_MISMATCH",
                    f"A valid {cand.filesystem.upper()} boot sector was found {cand.offset:,} bytes into "
                    "this region, but nothing at its start. The partition table entry is likely wrong.",
                    repairable=True,
                    detail={"boot_offset": cand.offset, "filesystem": cand.filesystem},
                )
            )
            break
    return issues
