"""Metadata repairs implemented in-process (no external tools required).

Each handler: (1) re-reads the live device, (2) builds the exact patches,
(3) applies them through the guarded writer, (4) re-parses to prove success.
"""

from __future__ import annotations

import struct
from typing import Any

from usbdoctor.analysis.filesystems.apfs import analyze_apfs
from usbdoctor.analysis.filesystems.detect import probe_boot_signatures
from usbdoctor.analysis.filesystems.exfat import ExfatAnalyzer, boot_checksum
from usbdoctor.analysis.filesystems.fat import FatAnalyzer
from usbdoctor.analysis.filesystems.ntfs import analyze_ntfs
from usbdoctor.analysis.partitions.tables import (
    MBR_SIGNATURE,
    analyze_gpt,
    find_lost_partitions,
    parse_gpt_entries,
    parse_gpt_header,
    parse_mbr,
)
from usbdoctor.core.io import DiskReader
from usbdoctor.repair.base import RepairResult, RepairStatus, RunContext
from usbdoctor.repair.patches import PatchRecord, apply_patches


def _volume_base(ctx: RunContext) -> int:
    """Absolute byte offset where this operation's volume begins.

    Attached drives are almost always partitioned, so the file system starts at
    the partition's offset rather than at LBA 0. The scan records where it
    analysed the volume and hands it to the runner; without this every repair
    would write at offset 0 and corrupt the partition table of a healthy disk.
    """
    return int(ctx.options.get("volume_base_offset") or 0)


def _volume_reader(ctx: RunContext) -> DiskReader:
    reader = DiskReader(ctx.device.path, sector_size=ctx.device.sector_size)
    reader.open()
    return reader


# ---------------------------------------------------------------------------
# 1. MBR boot signature
# ---------------------------------------------------------------------------


def repair_mbr_signature(ctx: RunContext) -> RepairResult:
    with _volume_reader(ctx) as reader:
        sector = reader.read(0, 512)
        if len(sector) < 512:
            return RepairResult.failure("The first sector of the device could not be read.")
        if sector[510:512] == MBR_SIGNATURE:
            return RepairResult.skipped("The boot signature is already present.")
        table_intact = any(sector[0x1BE + i * 16 + 4] for i in range(4))
        patches = [PatchRecord(510, MBR_SIGNATURE, "Restore the 0x55AA boot signature", "MBR_SIGNATURE")]
        if not table_intact:
            ctx.log(
                "The partition table itself is empty; restoring only the signature will still "
                "leave the volume RAW. Chain partition-table reconstruction as the next step.",
                level="warning",
            )

    def verify() -> tuple[bool, str]:
        with _volume_reader(ctx) as reader:
            sector = reader.read(0, 512)
        ok = sector[510:512] == MBR_SIGNATURE
        found = parse_mbr(sector, ctx.device.size_bytes or 0)
        if not ok:
            return False, "Boot signature did not persist."
        if not found.entries:
            return False, "Signature restored, but no partitions are defined yet (table is still empty)."
        return True, f"Signature restored; {len(found.entries)} partition(s) now recognised."

    return apply_patches(ctx, patches, operation="repair-mbr-signature", verify=verify)


# ---------------------------------------------------------------------------
# 2. Partition-table reconstruction
# ---------------------------------------------------------------------------


def repair_partition_table(ctx: RunContext) -> RepairResult:
    """Rebuild the MBR from surviving on-disk evidence, never from guesses."""
    size = ctx.device.size_bytes or 0
    with _volume_reader(ctx) as reader:
        sector = reader.read(0, 512)
        candidates = find_lost_partitions(sector, size, sector_size=reader.sector_size)
        # Also look for a valid file-system boot sector at the documented probe
        # offsets: if the data starts at 1 MiB with nothing in the table, we can
        # reconstruct an entry that points there.
        if not candidates:
            for cand in probe_boot_signatures(reader, 0, sector_size=reader.sector_size):
                if cand.offset < 1024 * 1024:
                    continue
                type_byte = _mbr_type_for(cand.filesystem)
                if type_byte is None:
                    continue
                length = _estimate_partition_size(reader, cand.offset, size, cand.filesystem)
                candidates.append(
                    {
                        "index": 1,
                        "offset": cand.offset,
                        "size": length,
                        "type_id": f"0x{type_byte:02X}",
                        "type_label": cand.filesystem,
                        "source": "boot_probe",
                    }
                )
                break

    if not candidates:
        return RepairResult(
            status=RepairStatus.NOT_APPLICABLE,
            message="No recoverable partition geometry was found on this device. "
            "File-level recovery is the better path than table reconstruction.",
        )

    table = bytearray(sector[:512])
    for i in range(4):
        table[0x1BE + i * 16 : 0x1BE + (i + 1) * 16] = b"\x00" * 16
    for index, entry in enumerate(candidates[:4]):
        off = 0x1BE + index * 16
        ptype = int(str(entry["type_id"]).replace("0x", ""), 16)
        start_sector = entry["offset"] // 512
        count_sectors = entry["size"] // 512
        table[off] = 0x80 if index == 0 else 0x00
        table[off + 4] = ptype
        struct.pack_into("<I", table, off + 8, start_sector)
        struct.pack_into("<I", table, off + 12, count_sectors)
    table[510:512] = MBR_SIGNATURE
    patches = [
        PatchRecord(
            0,
            bytes(table),
            f"Reconstruct MBR with {len(candidates)} partition entr{'y' if len(candidates)==1 else 'ies'}",
            "MBR_RECONSTRUCT",
        )
    ]

    def verify() -> tuple[bool, str]:
        with _volume_reader(ctx) as reader:
            sector_now = reader.read(0, 512)
        result = parse_mbr(sector_now, size)
        if not result.entries:
            return False, "Rebuilt table did not parse."
        for entry in result.entries:
            boot = reader.read(entry.offset, 512)
            from usbdoctor.analysis.filesystems.detect import sniff

            found = sniff(boot)
            if found.filesystem == "unknown":
                return False, f"Partition {entry.index} at {entry.offset:,} has no valid file system."
        return True, f"{len(result.entries)} partition(s) rebuilt and each one verified."

    return apply_patches(ctx, patches, operation="repair-partition-table", verify=verify)


def _mbr_type_for(filesystem: str) -> int | None:
    table = {
        "fat32": 0x0C,
        "fat16": 0x06,
        "fat12": 0x01,
        "exfat": 0x07,
        "ntfs": 0x07,
        "apfs": 0xAF,
        "hfs+": 0xAF,
        "ext2/3/4": 0x83,
    }
    for key, value in table.items():
        if filesystem.startswith(key.split("/")[0]):
            return value
    return table.get(filesystem)


def _estimate_partition_size(reader: DiskReader, offset: int, disk_size: int, filesystem: str) -> int:
    """Read the volume's own geometry to size the partition entry exactly."""
    sector = reader.read(offset, 512)
    try:
        if filesystem.startswith("fat"):
            bps = struct.unpack("<H", sector[11:13])[0]
            total16 = struct.unpack("<H", sector[19:21])[0]
            total32 = struct.unpack("<I", sector[32:36])[0]
            total = total16 or total32
            if bps and total:
                return min(total * bps, disk_size - offset)
        elif filesystem == "exfat":
            bps_shift = sector[108]
            bps = 1 << bps_shift if bps_shift <= 12 else 512
            volume_length = struct.unpack("<Q", sector[72:80])[0]
            if volume_length:
                return min(volume_length * bps, disk_size - offset)
        elif filesystem == "ntfs":
            return max(0, disk_size - offset)
    except struct.error:
        pass
    return max(0, disk_size - offset)


# ---------------------------------------------------------------------------
# 3. FAT32 boot-sector restore / FAT mirror / FSInfo
# ---------------------------------------------------------------------------


FAT_BOOT_REASON_CODES = (
    "FAT32_RESTORE_PRIMARY_BOOT",
    "FAT_BOOT_RESTORE",
    "FAT_BOOT_SECTOR_DESTROYED",
    "EXFAT_RESTORE_BOOT_REGION",
    "EXFAT_BOOT_REGION_DESTROYED",
)


def repair_fat_boot_sector(ctx: RunContext) -> RepairResult:
    base = _volume_base(ctx)
    with _volume_reader(ctx) as reader:
        sector_size = reader.sector_size or 512
        analysis = FatAnalyzer(reader, base).analyze()
        patches: list[PatchRecord] = []
        if analysis.variant == "fat32":
            backup_ptr = struct.unpack("<H", reader.read(base + 50, 2))[0]
            for candidate_sector in (backup_ptr or 6, 6):
                if not candidate_sector:
                    continue
                backup = reader.read(base + candidate_sector * sector_size, sector_size)
                if len(backup) >= sector_size and backup[510:512] == MBR_SIGNATURE:
                    patches.append(
                        PatchRecord(
                            base,
                            backup,
                            f"Restore primary boot sector from verified backup at sector {candidate_sector}",
                            "FAT32_RESTORE_PRIMARY_BOOT",
                        )
                    )
                    break
        if not patches:
            candidates = probe_boot_signatures(reader, base, sector_size=sector_size)
            for cand in candidates:
                relative = cand.offset - base
                # The primary is being written at ``base``, so the source must be
                # the backup belonging to *this* volume. Never accept a backup
                # from elsewhere on the disk (that is how a FAT boot sector ends
                # up written over an NTFS one).
                if relative == 12 * sector_size and cand.filesystem.startswith("exfat"):
                    region = reader.read(cand.offset, 12 * sector_size)
                    if (
                        len(region) == 12 * sector_size
                        and region[3:11] == b"EXFAT   "
                        and region[510:512] == MBR_SIGNATURE
                    ):
                        patches.append(
                            PatchRecord(
                                base,
                                region,
                                "Restore the exFAT boot region from its backup at sector 12",
                                "EXFAT_RESTORE_BOOT_REGION",
                            )
                        )
                        break
                if relative == 6 * sector_size and cand.filesystem.startswith("fat"):
                    data = reader.read(cand.offset, sector_size)
                    if (
                        len(data) == sector_size
                        and data[510:512] == MBR_SIGNATURE
                        and (data[3:11] == b"MSWIN4.1" or data[82:90] == b"FAT32   ")
                    ):
                        patches.append(
                            PatchRecord(base, data, "Restore boot sector from backup at sector 6", "FAT_BOOT_RESTORE")
                        )
                        break
    if not patches:
        return RepairResult(
            status=RepairStatus.NOT_APPLICABLE,
            message="No valid backup boot sector exists on this volume, so it cannot be restored "
            "safely. Use file-level recovery instead of formatting.",
        )

    def verify() -> tuple[bool, str]:
        with _volume_reader(ctx) as reader:
            again = FatAnalyzer(reader, base).analyze()
        if again.variant.startswith("fat"):
            return True, f"Boot sector restored; volume now identifies as {again.variant.upper()}."
        return False, "Boot sector restored but the volume still does not parse as FAT."

    return apply_patches(ctx, patches, operation="repair-fat-boot", verify=verify)


def repair_fat_mirror(ctx: RunContext) -> RepairResult:
    base = _volume_base(ctx)
    with _volume_reader(ctx) as reader:
        analysis = FatAnalyzer(reader, base).analyze()
        bps = analysis.bytes_per_sector
        if analysis.num_fats != 2:
            return RepairResult.skipped("This volume has a single FAT copy; nothing to synchronise.")
        fat_start = base + analysis.reserved_sectors * bps
        fat_bytes = analysis.fat_size_sectors * bps
        if fat_bytes > 64 << 20:
            return RepairResult.failure(
                f"FAT is {fat_bytes:,} bytes, larger than this routine will rewrite in one pass."
            )
        first = reader.read(fat_start, fat_bytes)
        second = reader.read(fat_start + fat_bytes, fat_bytes)
        if first == second:
            return RepairResult.skipped("FAT copies already match.")
        diffs = sum(1 for a, b in zip(first, second) if a != b)
        ctx.log(f"FAT copies differ in {diffs:,} bytes; mirroring copy #1 to copy #2.")
        patches = [
            PatchRecord(
                fat_start + fat_bytes,
                first,
                "Rewrite FAT copy #2 from FAT copy #1",
                "FAT_MIRROR_SECOND",
            )
        ]

    def verify() -> tuple[bool, str]:
        with _volume_reader(ctx) as reader:
            again = FatAnalyzer(reader, base).analyze()
        if again.fat_copies_match:
            return True, "Both FAT copies now match."
        return False, f"FAT copies still differ in {again.fat_diff_bytes} bytes."

    return apply_patches(ctx, patches, operation="repair-fat-mirror", verify=verify)


def repair_fat_fsinfo(ctx: RunContext) -> RepairResult:
    base = _volume_base(ctx)
    with _volume_reader(ctx) as reader:
        analysis = FatAnalyzer(reader, base).analyze()
        if analysis.variant != "fat32":
            return RepairResult.skipped("FSInfo only exists on FAT32 volumes.")
        sector_size = analysis.bytes_per_sector
        fsinfo_sector = struct.unpack("<H", reader.read(base + 48, 2))[0]
        if not fsinfo_sector:
            return RepairResult.failure("The boot sector does not point at an FSInfo sector.")
        current = bytearray(reader.read(base + fsinfo_sector * sector_size, sector_size))
        if len(current) < sector_size:
            return RepairResult.failure("FSInfo sector could not be read.")
        struct.pack_into("<I", current, 0, 0x41615252)
        struct.pack_into("<I", current, 484, 0x61417272)
        struct.pack_into("<I", current, 488, 0xFFFFFFFF)  # let the driver recount
        struct.pack_into("<I", current, 492, 0xFFFFFFFF)
        struct.pack_into("<I", current, 508, 0xAA550000)
        patches = [
            PatchRecord(
                base + fsinfo_sector * sector_size,
                bytes(current),
                "Rewrite FSInfo signatures and invalidate cached free-space counters",
                "FAT32_FSINFO_REBUILD",
            )
        ]

    def verify() -> tuple[bool, str]:
        with _volume_reader(ctx) as reader:
            again = FatAnalyzer(reader, base).analyze()
        if again.fsinfo_valid:
            return True, "FSInfo signatures now verify."
        return False, "FSInfo still does not verify."

    return apply_patches(ctx, patches, operation="repair-fat-fsinfo", verify=verify)


# ---------------------------------------------------------------------------
# 4. exFAT boot region + dirty flag
# ---------------------------------------------------------------------------


def repair_exfat_boot(ctx: RunContext) -> RepairResult:
    base = _volume_base(ctx)
    with _volume_reader(ctx) as reader:
        sector_size = reader.sector_size or 512
        primary = reader.read(base, 12 * sector_size)
        backup = reader.read(base + 12 * sector_size, 12 * sector_size)
        if len(backup) < 12 * sector_size or backup[3:11] != b"EXFAT   ":
            return RepairResult(
                status=RepairStatus.NOT_APPLICABLE,
                message="The exFAT backup boot region is missing or invalid; the primary cannot be "
                "restored from it. File-level recovery is recommended.",
            )
        stored = struct.unpack("<I", backup[11 * sector_size : 11 * sector_size + 4])[0]
        if stored != boot_checksum(backup[: 11 * sector_size]):
            return RepairResult(
                status=RepairStatus.NOT_APPLICABLE,
                message="The exFAT backup boot region fails its own checksum, so it is not a safe "
                "source for restoration.",
            )
        needs = struct.unpack("<I", primary[11 * sector_size : 11 * sector_size + 4])[0] != boot_checksum(
            primary[: 11 * sector_size]
        )
        if not needs:
            return RepairResult.skipped("The exFAT primary boot region already verifies.")
        patches = [
            PatchRecord(base, backup, "Restore exFAT boot region from its verified backup copy", "EXFAT_RESTORE_BOOT_REGION")
        ]

    def verify() -> tuple[bool, str]:
        with _volume_reader(ctx) as reader:
            again = ExfatAnalyzer(reader, base).analyze()
        if again.boot_checksum_valid:
            return True, "exFAT boot region checksum now verifies."
        return False, "exFAT boot region still fails checksum verification."

    return apply_patches(ctx, patches, operation="repair-exfat-boot", verify=verify)


def repair_exfat_dirty(ctx: RunContext) -> RepairResult:
    base = _volume_base(ctx)
    with _volume_reader(ctx) as reader:
        sector = reader.read(base, 512)
        if sector[3:11] != b"EXFAT   ":
            return RepairResult.skipped("This volume is not exFAT.")
        flags = struct.unpack("<H", sector[106:108])[0]
        if not flags & 0x0002:
            return RepairResult.skipped("The exFAT volume is not marked dirty.")
        if flags & 0x0004:
            return RepairResult.failure(
                "The volume is flagged with a media failure as well as dirty; clearing the flag "
                "would hide a real hardware problem. Recover data instead."
            )
        new_flags = flags & ~0x0002
        patches = [
            PatchRecord(
                base + 106,
                struct.pack("<H", new_flags),
                f"Clear the exFAT dirty flag (0x{flags:04X} → 0x{new_flags:04X})",
                "EXFAT_CLEAR_DIRTY",
            ),
            PatchRecord(base + 24, struct.pack("<H", new_flags), "Mirror the flag update in the backup boot region", "EXFAT_CLEAR_DIRTY_BACKUP"),
        ]

    def verify() -> tuple[bool, str]:
        with _volume_reader(ctx) as reader:
            again = ExfatAnalyzer(reader, base).analyze()
        if not again.dirty:
            return True, "The dirty flag is cleared."
        return False, "The dirty flag is still set."

    return apply_patches(ctx, patches, operation="repair-exfat-dirty", verify=verify)


# ---------------------------------------------------------------------------
# 5. NTFS boot-sector signature
# ---------------------------------------------------------------------------


def repair_ntfs_boot(ctx: RunContext) -> RepairResult:
    base = _volume_base(ctx)
    with _volume_reader(ctx) as reader:
        sector = reader.read(base, 512)
        if sector[3:11] != b"NTFS    ":
            return RepairResult.skipped("This volume is not NTFS.")
        if sector[510:512] == MBR_SIGNATURE:
            return RepairResult.skipped("NTFS boot signature already present.")
        patches = [
            PatchRecord(
                base + 510,
                MBR_SIGNATURE,
                "Restore the NTFS boot-sector signature 0x55AA",
                "NTFS_BOOT_SIGNATURE",
            )
        ]

    def verify() -> tuple[bool, str]:
        with _volume_reader(ctx) as reader:
            again = analyze_ntfs(reader, base)
        if any(i.code == "NTFS_BOOT_SIGNATURE_MISSING" for i in again.issues):
            return False, "The NTFS signature still does not verify."
        if not again.mft_runs:
            return False, "Signature restored, but $MFT is still unreadable — run CHKDSK next."
        return True, "NTFS boot sector restored and $MFT is readable."

    return apply_patches(ctx, patches, operation="repair-ntfs-boot", verify=verify)


# ---------------------------------------------------------------------------
# 6. GPT backup header rebuild
# ---------------------------------------------------------------------------


def repair_gpt_backup(ctx: RunContext) -> RepairResult:
    size = ctx.device.size_bytes or 0
    with _volume_reader(ctx) as reader:
        sector = reader.sector_size or 512
        primary = reader.read(sector, sector)
        header = parse_gpt_header(primary)
        if header is None:
            return RepairResult(
                status=RepairStatus.NOT_APPLICABLE,
                message="The primary GPT header is missing or invalid; rebuilding from a corrupt "
                "primary would require partition-table reconstruction instead.",
            )
        total_sectors = size // sector
        entries_data = reader.read(header["entries_lba"] * sector, header["num_entries"] * header["entry_size"])
        entries, crc_ok = parse_gpt_entries(entries_data, header)
        if not crc_ok:
            return RepairResult.failure(
                "The primary GPT entry array fails its CRC; it is not a safe source for the backup."
            )
        patches: list[PatchRecord] = []
        backup_entries_lba = total_sectors - 33
        backup_header_lba = total_sectors - 1
        import zlib

        new_header = bytearray(primary[: max(92, header["header_size"])])
        struct.pack_into("<Q", new_header, 24, backup_header_lba)
        struct.pack_into("<Q", new_header, 32, 1)
        struct.pack_into("<Q", new_header, 48, total_sectors - 34)
        struct.pack_into("<Q", new_header, 72, backup_entries_lba)
        struct.pack_into("<I", new_header, 16, 0)
        struct.pack_into("<I", new_header, 16, zlib.crc32(bytes(new_header)) & 0xFFFFFFFF)
        patches.append(
            PatchRecord(
                backup_entries_lba * sector,
                entries_data,
                "Write the backup GPT partition array from the verified primary",
                "GPT_BACKUP_ENTRIES",
            )
        )
        patches.append(
            PatchRecord(
                backup_header_lba * sector,
                bytes(new_header).ljust(sector, b"\x00"),
                "Write the backup GPT header with corrected LBAs and CRC",
                "GPT_BACKUP_HEADER",
            )
        )

    def verify() -> tuple[bool, str]:
        with _volume_reader(ctx) as reader:
            sector_now = reader.sector_size or 512
            primary_h = parse_gpt_header(reader.read(sector_now, sector_now))
            backup_h = parse_gpt_header(reader.read(size - sector_now, sector_now))
            if backup_h is None:
                return False, "The backup header was not written."
            if primary_h and backup_h.get("disk_guid") != primary_h.get("disk_guid"):
                return False, "Backup header GUID does not match the primary."
            p_entries, p_ok = parse_gpt_entries(
                reader.read(primary_h["entries_lba"] * sector_now, 128 * 128), primary_h
            ) if primary_h else ([], False)
            b_entries, b_ok = parse_gpt_entries(
                reader.read(backup_h["entries_lba"] * sector_now, 128 * 128), backup_h
            )
            if not b_ok:
                return False, "Backup entry array CRC is invalid."
            if len(p_entries) != len(b_entries):
                return False, "Backup partition count differs from primary."
            return True, f"Backup GPT rebuilt; {len(b_entries)} entries verified."

    return apply_patches(ctx, patches, operation="repair-gpt-backup", verify=verify)


# ---------------------------------------------------------------------------
# 7. Cross-check helpers used by the planner
# ---------------------------------------------------------------------------


def gpt_needs_repair(reader: DiskReader, size: int) -> tuple[bool, str]:
    sector = reader.sector_size or 512
    primary = parse_gpt_header(reader.read(sector, sector))
    if primary is None:
        return False, "no primary GPT"
    backup = parse_gpt_header(reader.read(size - sector, sector)) if size >= 2 * sector else None
    if backup is None:
        return True, "backup GPT header missing"
    return False, "backup GPT present"


def filesystem_patches_available(reader: DiskReader) -> dict[str, Any]:
    """Summarise what the metadata repair engine could fix, for planning."""
    from usbdoctor.analysis.filesystems.detect import analyze_volume

    analysis = analyze_volume(reader, 0)
    patches = getattr(analysis.detail, "patches", [])
    return {
        "filesystem": analysis.filesystem,
        "issue_codes": [i.code for i in analysis.issues],
        "patch_count": len(patches),
        "patches": [p.to_dict() for p in patches],
    }
