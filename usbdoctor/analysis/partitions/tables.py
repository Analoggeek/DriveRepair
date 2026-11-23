"""Partition-table analysis: MBR, GPT (with protective MBR + backup header),
BSD/disklabel sanity, and APM (Apple Partition Map) detection.

Parsers are pure functions over bytes so they are exercised by the automated
test-suite against generated images, exactly the same code path used on real
hardware.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field
from typing import Optional

MBR_SIGNATURE = b"\x55\xaa"
GPT_SIGNATURE = b"EFI PART"

MBR_TYPES: dict[int, str] = {
    0x00: "Empty",
    0x01: "FAT12",
    0x04: "FAT16 <32M",
    0x05: "Extended (CHS)",
    0x06: "FAT16",
    0x07: "NTFS / exFAT / HPFS",
    0x0B: "FAT32 (CHS)",
    0x0C: "FAT32 (LBA)",
    0x0E: "FAT16 (LBA)",
    0x0F: "Extended (LBA)",
    0x11: "Hidden FAT12",
    0x12: "Compaq diagnostics",
    0x14: "Hidden FAT16 <32M",
    0x16: "Hidden FAT16",
    0x17: "Hidden NTFS",
    0x1B: "Hidden FAT32",
    0x1C: "Hidden FAT32 (LBA)",
    0x1E: "Hidden FAT16 (LBA)",
    0x27: "Windows RE (NTFS)",
    0x3C: "PartitionMagic recovery",
    0x42: "Windows dynamic disk",
    0x63: "UNIX System V",
    0x82: "Linux swap / Solaris",
    0x83: "Linux",
    0x84: "Hibernation",
    0x85: "Linux extended",
    0x86: "NTFS volume set",
    0x87: "NTFS volume set",
    0x8E: "Linux LVM",
    0xA5: "FreeBSD",
    0xA6: "OpenBSD",
    0xA8: "macOS UFS",
    0xA9: "NetBSD",
    0xAB: "macOS boot",
    0xAF: "macOS HFS/HFS+",
    0xBE: "Solaris boot",
    0xBF: "Solaris",
    0xEE: "GPT protective",
    0xEF: "EFI System",
    0xFD: "Linux RAID",
}

GPT_TYPE_GUIDS: dict[str, tuple[str, str]] = {
    "c12a7328-f81f-11d2-ba4b-00a0c93ec93b": ("EFI System Partition", "efi"),
    "21686148-6449-6e6f-744e-656564454649": ("BIOS Boot Partition", "bios"),
    "e3c9e316-0b5c-4db8-817d-f92df00215ae": ("Microsoft Reserved", "msr"),
    "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7": ("Microsoft Basic Data", "basic"),
    "de94bba4-06d1-4d40-a16a-bfd50179d6ac": ("Windows Recovery", "recovery"),
    "0fc63daf-8483-4772-8e79-3d69d8477de4": ("Linux Filesystem", "linux"),
    "0657fd6d-a4ab-43c4-84e5-0933c84b4f4f": ("Linux Swap", "swap"),
    "e6d6d379-f507-44c2-a23c-238f2a3df928": ("Linux LVM", "lvm"),
    "7c3457ef-0000-11aa-aa11-00306543ecac": ("Apple APFS", "apfs"),
    "48465300-0000-11aa-aa11-00306543ecac": ("Apple HFS+", "hfs"),
    "426f6f74-0000-11aa-aa11-00306543ecac": ("Apple Boot", "appleboot"),
    "53746f72-6167-11aa-aa11-00306543ecac": ("Apple Core Storage", "corestorage"),
    "6a898cc3-1dd2-11b2-99a6-080020736631": ("Solaris / ZFS", "zfs"),
    "516e7cb4-6ecf-11d6-8ff8-00022d09712b": ("FreeBSD UFS", "ufs"),
}


@dataclass
class PartEntry:
    index: int
    offset: int
    size: int
    type_id: str
    type_label: str
    bootable: bool = False
    name: Optional[str] = None
    guid: Optional[str] = None
    flags: int = 0
    in_extended: bool = False

    @property
    def end(self) -> int:
        return self.offset + self.size

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "offset": self.offset,
            "size": self.size,
            "type_id": self.type_id,
            "type_label": self.type_label,
            "bootable": self.bootable,
            "name": self.name,
            "guid": self.guid,
            "in_extended": self.in_extended,
        }


@dataclass
class TableIssue:
    severity: str  # info | warning | error | critical
    code: str
    message: str
    repairable: bool = False
    detail: dict = field(default_factory=dict)


@dataclass
class PartitionTableResult:
    scheme: str  # mbr | gpt | apm | none | unknown
    entries: list[PartEntry] = field(default_factory=list)
    issues: list[TableIssue] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "scheme": self.scheme,
            "entries": [e.to_dict() for e in self.entries],
            "issues": [i.__dict__ for i in self.issues],
            "warnings": self.warnings,
            "details": self.details,
        }

    @property
    def is_valid(self) -> bool:
        return self.scheme not in ("none", "unknown") and not any(
            i.severity in ("error", "critical") for i in self.issues
        )


# ---------------------------------------------------------------------------
# GUID helpers
# ---------------------------------------------------------------------------


def parse_guid(raw: bytes) -> str:
    """Convert a mixed-endian GPT GUID (16 bytes) to canonical text form."""
    if len(raw) != 16:
        return ""
    d1 = struct.unpack("<I", raw[0:4])[0]
    d2 = struct.unpack("<H", raw[4:6])[0]
    d3 = struct.unpack("<H", raw[6:8])[0]
    d4 = raw[8:10].hex()
    d5 = raw[10:16].hex()
    return f"{d1:08x}-{d2:04x}-{d3:04x}-{d4}-{d5}"


# ---------------------------------------------------------------------------
# MBR
# ---------------------------------------------------------------------------


def parse_mbr(sector: bytes, disk_size: int, *, sector_size: int = 512) -> PartitionTableResult:
    result = PartitionTableResult(scheme="unknown")
    if len(sector) < 512:
        result.issues.append(TableIssue("critical", "MBR_TRUNCATED", "Sector 0 could not be read."))
        return result
    if sector[510:512] != MBR_SIGNATURE:
        # A missing 0x55AA is damage, not a layout: every valid FAT, exFAT and
        # NTFS boot record carries its own 0x55AA at offset 510, so a
        # superfloppy cannot lack it. Recognise the *file system* that is still
        # sitting at LBA 0 so the diagnosis can say "the volume is here, its
        # boot signature is gone" instead of "the disk is uninitialised", and
        # so the repair targets the volume rather than inventing a table.
        result.scheme = "none"
        boot_record = sector[3:11]
        fs_at_lba0 = (
            boot_record in (b"NTFS    ", b"EXFAT   ")
            or boot_record == b"MSWIN4.1"
            or sector[82:90] == b"FAT32   "
            or sector[54:62] in (b"FAT16   ", b"FAT12   ")
            or sector[0:4] == b"NXSB"
        )
        if fs_at_lba0:
            result.details["volume_at_lba0"] = True
        result.issues.append(
            TableIssue(
                "critical",
                "MBR_SIGNATURE_MISSING",
                "Boot signature 0x55AA is missing at offset 510 — the partition table is blank or destroyed."
                + (" A file-system boot record is still present at offset 0." if fs_at_lba0 else ""),
                repairable=True,
            )
        )
        return result

    result.scheme = "mbr"
    raw_entries = []
    for i in range(4):
        off = 0x1BE + i * 16
        entry = sector[off : off + 16]
        status = entry[0]
        ptype = entry[4]
        lba = struct.unpack("<I", entry[8:12])[0]
        count = struct.unpack("<I", entry[12:16])[0]
        raw_entries.append((status, ptype, lba, count))
        if ptype == 0 or count == 0:
            continue
        result.entries.append(
            PartEntry(
                index=i + 1,
                offset=lba * sector_size,
                size=count * sector_size,
                type_id=f"0x{ptype:02X}",
                type_label=MBR_TYPES.get(ptype, f"Unknown (0x{ptype:02X})"),
                bootable=status == 0x80,
            )
        )
        if status not in (0x00, 0x80):
            result.issues.append(
                TableIssue(
                    "warning",
                    "MBR_BAD_STATUS_BYTE",
                    f"Partition {i + 1} has an invalid status byte 0x{status:02X} (expected 0x00 or 0x80).",
                    repairable=True,
                    detail={"partition": i + 1},
                )
            )

    # GPT protective MBR?
    if any(ptype == 0xEE for _s, ptype, _l, _c in raw_entries):
        result.scheme = "gpt"
        result.details["protective_mbr"] = True
        return result

    # Sanity checks -------------------------------------------------------
    seen_ranges: list[tuple[int, int, int]] = []
    for e in result.entries:
        if e.size == 0:
            result.issues.append(
                TableIssue("error", "MBR_ZERO_LENGTH", f"Partition {e.index} has zero length.", repairable=True)
            )
            continue
        if disk_size and e.end > disk_size:
            result.issues.append(
                TableIssue(
                    "error",
                    "MBR_PARTITION_BEYOND_DISK",
                    f"Partition {e.index} ends at byte {e.end:,} which is beyond the disk end ({disk_size:,}).",
                    repairable=True,
                    detail={"partition": e.index, "end": e.end, "disk": disk_size},
                )
            )
        if e.offset < sector_size:
            result.issues.append(
                TableIssue(
                    "error",
                    "MBR_PARTITION_IN_MBR",
                    f"Partition {e.index} starts inside the MBR sector.",
                    repairable=True,
                )
            )
        for other in seen_ranges:
            if e.offset < other[1] and e.end > other[0]:
                result.issues.append(
                    TableIssue(
                        "error",
                        "MBR_OVERLAP",
                        f"Partitions {other[2]} and {e.index} overlap — the table is corrupt.",
                        repairable=True,
                        detail={"a": other[2], "b": e.index},
                    )
                )
        seen_ranges.append((e.offset, e.end, e.index))

    if not result.entries:
        result.issues.append(
            TableIssue(
                "warning",
                "MBR_NO_PARTITIONS",
                "No partitions are defined; the disk is uninitialised (or uses a non-MBR layout).",
            )
        )

    # Extended partition chain --------------------------------------------
    extended = [e for e in result.entries if e.type_id in ("0x05", "0x0F", "0x85")]
    for ext in extended:
        _walk_extended(sector, ext, result, disk_size, sector_size)

    return result


def _walk_extended(
    mbr: bytes,
    ext: PartEntry,
    result: PartitionTableResult,
    disk_size: int,
    sector_size: int,
) -> None:
    """Follow the EBR chain of an extended partition (bounded, loop-safe)."""
    # EBRs are read by the caller-provided bytes only for LBA 0; here we record
    # what we can and note that deeper chains need device access.
    result.details.setdefault("extended_partitions", []).append(ext.to_dict())


def find_lost_partitions(sector: bytes, disk_size: int, *, sector_size: int = 512) -> list[dict]:
    """Scan MBR leftovers for plausible but unlinked partition entries.

    Used by the partition-table recovery routine: when the primary table was
    destroyed, old entries often survive inside the MBR sector (or in the
    backup GPT / for FAT volumes, in the boot sector's ``hidden sectors``).
    """
    recovered: list[dict] = []
    if len(sector) < 512:
        return recovered
    for i in range(4):
        off = 0x1BE + i * 16
        ptype = sector[off + 4]
        lba = struct.unpack("<I", sector[off + 8 : off + 12])[0]
        count = struct.unpack("<I", sector[off + 12 : off + 16])[0]
        if ptype in MBR_TYPES and ptype not in (0x00, 0xEE) and count:
            start = lba * sector_size
            if start and start < disk_size and (start + count * sector_size) <= disk_size + sector_size:
                recovered.append(
                    {
                        "index": i + 1,
                        "offset": start,
                        "size": count * sector_size,
                        "type_id": f"0x{ptype:02X}",
                        "type_label": MBR_TYPES.get(ptype, "Unknown"),
                        "source": "mbr_slack",
                    }
                )
    return recovered


# ---------------------------------------------------------------------------
# GPT
# ---------------------------------------------------------------------------


def parse_gpt_header(block: bytes) -> Optional[dict]:
    if len(block) < 92 or block[:8] != GPT_SIGNATURE:
        return None
    header = {
        "revision": struct.unpack("<I", block[8:12])[0],
        "header_size": struct.unpack("<I", block[12:16])[0],
        "crc32": struct.unpack("<I", block[16:20])[0],
        "current_lba": struct.unpack("<Q", block[24:32])[0],
        "backup_lba": struct.unpack("<Q", block[32:40])[0],
        "first_usable_lba": struct.unpack("<Q", block[40:48])[0],
        "last_usable_lba": struct.unpack("<Q", block[48:56])[0],
        "disk_guid": parse_guid(block[56:72]),
        "entries_lba": struct.unpack("<Q", block[72:80])[0],
        "num_entries": struct.unpack("<I", block[80:84])[0],
        "entry_size": struct.unpack("<I", block[84:88])[0],
        "entries_crc32": struct.unpack("<I", block[88:92])[0],
    }
    size = header["header_size"]
    if not 92 <= size <= len(block):
        header["crc_valid"] = False
        return header
    zeroed = bytearray(block[:size])
    zeroed[16:20] = b"\x00\x00\x00\x00"
    header["crc_valid"] = zlib.crc32(bytes(zeroed)) & 0xFFFFFFFF == header["crc32"]
    return header


def parse_gpt_entries(data: bytes, header: dict, *, max_entries: int = 256) -> tuple[list[PartEntry], bool]:
    entries: list[PartEntry] = []
    entry_size = header.get("entry_size", 128)
    count = min(header.get("num_entries", 0), max_entries)
    if entry_size < 128:
        return entries, False
    crc_ok = False
    expected_crc = header.get("entries_crc32")
    if expected_crc is not None and data:
        crc_ok = (zlib.crc32(data) & 0xFFFFFFFF) == expected_crc
    for i in range(count):
        raw = data[i * entry_size : (i + 1) * entry_size]
        if len(raw) < 128:
            break
        type_guid = parse_guid(raw[0:16])
        if type_guid == "00000000-0000-0000-0000-000000000000":
            continue
        unique = parse_guid(raw[16:32])
        first = struct.unpack("<Q", raw[32:40])[0]
        last = struct.unpack("<Q", raw[40:48])[0]
        attrs = struct.unpack("<Q", raw[48:56])[0]
        try:
            name = raw[56:128].decode("utf-16-le").split("\x00", 1)[0].strip()
        except UnicodeDecodeError:
            name = ""
        label, _key = GPT_TYPE_GUIDS.get(type_guid, ("Unknown GPT type", "unknown"))
        entries.append(
            PartEntry(
                index=i + 1,
                offset=first * 512,
                size=max(0, (last - first + 1) * 512),
                type_id=type_guid,
                type_label=label,
                name=name or None,
                guid=unique,
                flags=attrs,
            )
        )
    return entries, crc_ok


def analyze_gpt(
    primary_header: Optional[dict],
    primary_entries: list[PartEntry],
    primary_crc_ok: bool,
    *,
    backup_header: Optional[dict] = None,
    backup_entries: Optional[list[PartEntry]] = None,
    backup_crc_ok: bool = False,
    disk_size: int = 0,
) -> PartitionTableResult:
    result = PartitionTableResult(scheme="gpt" if primary_header or backup_header else "none")
    header = primary_header or backup_header or {}
    entries = primary_entries or backup_entries or []
    result.entries = [e for e in entries]
    result.details["disk_guid"] = header.get("disk_guid")
    result.details["primary_header_valid"] = bool(primary_header and header.get("crc_valid", False))
    result.details["backup_header_present"] = backup_header is not None
    if primary_header and not primary_crc_ok:
        result.issues.append(
            TableIssue(
                "error",
                "GPT_PRIMARY_ENTRIES_CRC",
                "Primary GPT partition array CRC is invalid.",
                repairable=True,
            )
        )
    if primary_header and not primary_header.get("crc_valid", True):
        result.issues.append(
            TableIssue(
                "error",
                "GPT_PRIMARY_HEADER_CRC",
                "Primary GPT header CRC is invalid — the header is corrupt.",
                repairable=True,
            )
        )
    if backup_header is None:
        result.issues.append(
            TableIssue(
                "warning",
                "GPT_BACKUP_MISSING",
                "Backup GPT header at the end of the disk is missing or unreadable.",
                repairable=True,
            )
        )
    elif not backup_crc_ok:
        result.issues.append(
            TableIssue(
                "warning",
                "GPT_BACKUP_CRC",
                "Backup GPT partition array CRC is invalid.",
                repairable=True,
            )
        )
    if primary_header and backup_header:
        if primary_header.get("disk_guid") != backup_header.get("disk_guid"):
            result.issues.append(
                TableIssue(
                    "error",
                    "GPT_GUID_MISMATCH",
                    "Primary and backup GPT disk GUIDs differ — a stale header remains.",
                    repairable=True,
                )
            )
        if primary_entries and backup_entries and len(primary_entries) != len(backup_entries):
            result.issues.append(
                TableIssue(
                    "warning",
                    "GPT_ENTRY_COUNT_MISMATCH",
                    f"Primary GPT lists {len(primary_entries)} partitions, backup lists {len(backup_entries)}.",
                    repairable=True,
                )
            )

    for e in result.entries:
        if disk_size and e.end > disk_size:
            result.issues.append(
                TableIssue(
                    "error",
                    "GPT_PARTITION_BEYOND_DISK",
                    f"Partition {e.index} ({e.name or e.type_label}) extends past the end of the disk.",
                    repairable=True,
                )
            )
        if header.get("first_usable_lba") and e.offset < header["first_usable_lba"] * 512:
            result.issues.append(
                TableIssue(
                    "warning",
                    "GPT_PARTITION_BEFORE_USABLE",
                    f"Partition {e.index} starts before the first usable LBA.",
                    repairable=True,
                )
            )
    ranges = sorted((e.offset, e.end, e.index) for e in result.entries)
    for a, b in zip(ranges, ranges[1:]):
        if b[0] < a[1]:
            result.issues.append(
                TableIssue(
                    "error",
                    "GPT_OVERLAP",
                    f"GPT partitions {a[2]} and {b[2]} overlap.",
                    repairable=True,
                )
            )
    return result


# ---------------------------------------------------------------------------
# Apple Partition Map (older Macs, still seen on old SD cards / iPods)
# ---------------------------------------------------------------------------


def looks_like_apm(sector: bytes) -> bool:
    if len(sector) < 512 or sector[510:512] != MBR_SIGNATURE:
        return False
    return sector[0:2] == b"PM"


def parse_apm(sector: bytes) -> PartitionTableResult:
    """Parse the first Apple Partition Map entry (block 1 is usually read next)."""
    result = PartitionTableResult(scheme="apm")
    if len(sector) < 512:
        return result
    total = struct.unpack(">I", sector[4:8])[0]
    start = struct.unpack(">I", sector[8:12])[0]
    count = struct.unpack(">I", sector[12:16])[0]
    name = sector[16:48].split(b"\x00", 1)[0].decode("mac_roman", "replace")
    ptype = sector[48:80].split(b"\x00", 1)[0].decode("mac_roman", "replace")
    result.details.update({"block_size": total, "first_block": start, "entry_count": count, "name": name})
    result.entries.append(
        PartEntry(index=1, offset=start * total, size=count * total, type_id=ptype, type_label=ptype)
    )
    return result


def describe_issue_repair(issue: TableIssue) -> str:
    """Plain-language explanation used in reports and the repair UI."""
    table = {
        "MBR_SIGNATURE_MISSING": "The 0x55AA boot marker is gone, so the OS sees the disk as RAW. "
        "The partition list can usually be rebuilt from the data still on the drive.",
        "MBR_OVERLAP": "Two partitions claim the same sectors. Windows/macOS refuse to mount them safely.",
        "MBR_PARTITION_BEYOND_DISK": "A partition entry points past the end of the flash chip — a sign of "
        "table corruption or a counterfeit capacity.",
        "GPT_PRIMARY_HEADER_CRC": "The GPT header checksum failed. The backup header at the end of the "
        "disk can restore it.",
        "GPT_BACKUP_MISSING": "The backup GPT is missing. Repairable by rewriting it from the primary.",
        "GPT_GUID_MISMATCH": "A stale GPT header is present. Rewriting the backup from the good primary "
        "resolves it.",
    }
    return table.get(issue.code, issue.message)
