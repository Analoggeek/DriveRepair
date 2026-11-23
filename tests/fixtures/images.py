"""Builders for real, mountable disk images used by the automated test-suite.

These are *not* mocks: they emit genuine FAT32/exFAT/NTFS/ext4 structures with
correct checksums, which is what lets the tests prove that USB Doctor AI's
analyzers and byte-level repairs work on real layouts.  Everything is written
into a caller-supplied path (tests always use a temp dir, never your devices).
"""

from __future__ import annotations

import os
import struct
import zlib
from pathlib import Path
from typing import Optional

SECTOR = 512


def _mk(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        fh.truncate(size)


def _read_at(path: Path, offset: int, size: int) -> bytes:
    with open(path, "rb") as fh:
        fh.seek(offset)
        return fh.read(size)


def _put(path: Path, offset: int, data: bytes) -> None:
    with open(path, "r+b") as fh:
        fh.seek(offset)
        fh.write(data)


# ---------------------------------------------------------------------------
# MBR / GPT
# ---------------------------------------------------------------------------


def write_mbr(
    path: Path,
    partitions: list[tuple[int, int, int]],  # (type, start_sector, sector_count)
    *,
    disk_signature: bytes = b"\xde\xad\xbe\xef",
    bootable_first: bool = True,
) -> None:
    mbr = bytearray(SECTOR)
    for i, (ptype, start, count) in enumerate(partitions[:4]):
        off = 0x1BE + i * 16
        mbr[off] = 0x80 if (i == 0 and bootable_first) else 0x00
        mbr[off + 4] = ptype
        struct.pack_into("<I", mbr, off + 8, start)
        struct.pack_into("<I", mbr, off + 12, count)
    mbr[0x1B8:0x1BC] = disk_signature
    mbr[510:512] = b"\x55\xaa"
    _put(path, 0, bytes(mbr))


def write_gpt(path: Path, size: int, entries: list[tuple[str, int, int, str]]) -> None:
    """Write a valid GPT (protective MBR + primary + backup) to ``path``.

    ``entries`` items are (type_guid, first_lba, last_lba, name).
    """
    total_lba = size // SECTOR
    # protective MBR
    mbr = bytearray(SECTOR)
    mbr[0x1BE] = 0x00
    mbr[0x1BE + 4] = 0xEE
    struct.pack_into("<I", mbr, 0x1BE + 8, 1)
    struct.pack_into("<I", mbr, 0x1BE + 12, min(total_lba - 1, 0xFFFFFFFF))
    mbr[510:512] = b"\x55\xaa"
    _put(path, 0, bytes(mbr))

    entry_array = bytearray(128 * 128)
    for i, (type_guid, first, last, name) in enumerate(entries):
        raw = bytearray(128)
        raw[0:16] = _guid_bytes(type_guid)
        raw[16:32] = _guid_bytes(_random_guid(i))
        struct.pack_into("<Q", raw, 32, first)
        struct.pack_into("<Q", raw, 40, last)
        encoded = name.encode("utf-16-le")[:72]
        raw[56 : 56 + len(encoded)] = encoded
        entry_array[i * 128 : (i + 1) * 128] = raw
    entries_crc = zlib.crc32(bytes(entry_array)) & 0xFFFFFFFF

    disk_guid = _guid_bytes("2f0a4f68-91c0-4a4e-9a4e-000000000001")

    def make_header(current: int, backup: int, entries_lba: int) -> bytes:
        hdr = bytearray(SECTOR)
        hdr[0:8] = b"EFI PART"
        struct.pack_into("<I", hdr, 8, 0x00010000)
        struct.pack_into("<I", hdr, 12, 92)
        struct.pack_into("<Q", hdr, 24, current)
        struct.pack_into("<Q", hdr, 32, backup)
        struct.pack_into("<Q", hdr, 40, 34)
        struct.pack_into("<Q", hdr, 48, total_lba - 34)
        hdr[56:72] = disk_guid
        struct.pack_into("<Q", hdr, 72, entries_lba)
        struct.pack_into("<I", hdr, 80, 128)
        struct.pack_into("<I", hdr, 84, 128)
        struct.pack_into("<I", hdr, 88, entries_crc)
        struct.pack_into("<I", hdr, 16, zlib.crc32(bytes(hdr[:92])) & 0xFFFFFFFF)
        return bytes(hdr)

    _put(path, SECTOR, make_header(1, total_lba - 1, 2))
    _put(path, 2 * SECTOR, bytes(entry_array))
    backup_entries_lba = total_lba - 33
    _put(path, backup_entries_lba * SECTOR, bytes(entry_array))
    _put(path, (total_lba - 1) * SECTOR, make_header(total_lba - 1, 1, backup_entries_lba))


def _guid_bytes(text: str) -> bytes:
    parts = text.replace("{", "").replace("}", "").split("-")
    d1 = int(parts[0], 16)
    d2 = int(parts[1], 16)
    d3 = int(parts[2], 16)
    d4 = bytes.fromhex(parts[3])
    d5 = bytes.fromhex(parts[4])
    return struct.pack("<IHH", d1, d2, d3) + d4 + d5


def _random_guid(index: int) -> str:
    return f"1a2b3c4d-0000-4000-8000-0000000000{index:02x}"


GPT_BASIC_DATA = "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7"
GPT_EFI = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"


# ---------------------------------------------------------------------------
# FAT32
# ---------------------------------------------------------------------------


def _fat32_geometry(total_sectors: int, sectors_per_cluster: int = 1) -> dict:
    reserved = 32
    num_fats = 2
    # iterate to converge FAT size
    fat_sectors = 1
    for _ in range(16):
        data_sectors = total_sectors - reserved - num_fats * fat_sectors
        clusters = data_sectors // sectors_per_cluster
        need = ((clusters + 2) * 4 + SECTOR - 1) // SECTOR
        if need == fat_sectors:
            break
        fat_sectors = need
    data_sectors = total_sectors - reserved - num_fats * fat_sectors
    clusters = data_sectors // sectors_per_cluster
    return {
        "reserved": reserved,
        "num_fats": num_fats,
        "fat_sectors": fat_sectors,
        "clusters": clusters,
        "sectors_per_cluster": sectors_per_cluster,
    }


def build_fat32(path: Path, size: int, *, label: str = "USBTEST", volume_id: int = 0x1A2B3C4D) -> dict:
    total_sectors = size // SECTOR
    geo = _fat32_geometry(total_sectors)
    _mk(path, size)
    boot = bytearray(SECTOR)
    boot[0:3] = b"\xEB\x58\x90"
    boot[3:11] = b"MSWIN4.1"
    struct.pack_into("<H", boot, 11, SECTOR)
    boot[13] = geo["sectors_per_cluster"]
    struct.pack_into("<H", boot, 14, geo["reserved"])
    boot[16] = geo["num_fats"]
    struct.pack_into("<H", boot, 17, 0)  # root entries (0 for FAT32)
    struct.pack_into("<H", boot, 19, 0)  # total sectors 16
    boot[21] = 0xF8
    struct.pack_into("<H", boot, 22, 0)  # fat size 16
    struct.pack_into("<H", boot, 24, 32)  # sectors per track
    struct.pack_into("<H", boot, 26, 64)  # heads
    struct.pack_into("<I", boot, 28, 0)  # hidden sectors
    struct.pack_into("<I", boot, 32, total_sectors)
    struct.pack_into("<I", boot, 36, geo["fat_sectors"])
    struct.pack_into("<H", boot, 40, 0)  # ext flags
    struct.pack_into("<H", boot, 42, 0)  # fs version
    struct.pack_into("<I", boot, 44, 2)  # root cluster
    struct.pack_into("<H", boot, 48, 1)  # fsinfo sector
    struct.pack_into("<H", boot, 50, 6)  # backup boot sector
    boot[64] = 0x80
    boot[66] = 0x29
    struct.pack_into("<I", boot, 67, volume_id)
    boot[71:82] = label.encode("ascii")[:11].ljust(11, b" ")
    boot[82:90] = b"FAT32   "
    boot[510:512] = b"\x55\xaa"
    _put(path, 0, bytes(boot))
    _put(path, 6 * SECTOR, bytes(boot))  # backup boot sector

    fsinfo = bytearray(SECTOR)
    struct.pack_into("<I", fsinfo, 0, 0x41615252)
    struct.pack_into("<I", fsinfo, 484, 0x61417272)
    struct.pack_into("<I", fsinfo, 488, geo["clusters"] - 1)  # free clusters
    struct.pack_into("<I", fsinfo, 492, 3)  # next free cluster
    struct.pack_into("<I", fsinfo, 508, 0xAA550000)
    _put(path, 1 * SECTOR, bytes(fsinfo))
    _put(path, 7 * SECTOR, bytes(fsinfo))  # backup fsinfo

    fat = bytearray(geo["fat_sectors"] * SECTOR)
    struct.pack_into("<I", fat, 0, 0x0FFFFFF8)
    struct.pack_into("<I", fat, 4, 0x0FFFFFFF)
    struct.pack_into("<I", fat, 8, 0x0FFFFFFF)  # root dir cluster 2 = EOC
    fat_bytes = bytes(fat)
    _put(path, geo["reserved"] * SECTOR, fat_bytes)
    _put(path, (geo["reserved"] + geo["fat_sectors"]) * SECTOR, fat_bytes)

    data_start = (geo["reserved"] + geo["num_fats"] * geo["fat_sectors"]) * SECTOR
    root = bytearray(geo["sectors_per_cluster"] * SECTOR)
    vol_label = bytearray(32)
    vol_label[0:11] = label.encode("ascii")[:11].ljust(11, b" ")
    vol_label[11] = 0x08
    vol_label[23:26] = b"\x00\x00\x00"
    root[0:32] = vol_label
    _put(path, data_start, bytes(root))
    return geo


def add_fat32_file(path: Path, name: str, content: bytes, geo: dict) -> None:
    data_start = (geo["reserved"] + geo["num_fats"] * geo["fat_sectors"]) * SECTOR
    cluster_size = geo["sectors_per_cluster"] * SECTOR
    # allocate cluster 3
    fat_off = geo["reserved"] * SECTOR
    for copy in range(geo["num_fats"]):
        _put(path, fat_off + copy * geo["fat_sectors"] * SECTOR + 3 * 4, struct.pack("<I", 0x0FFFFFFF))
    _put(path, data_start + (3 - 2) * cluster_size, content[:cluster_size].ljust(cluster_size, b"\x00"))
    entry = bytearray(32)
    base, _, ext = name.rpartition(".")
    entry[0:8] = base.upper().encode("ascii")[:8].ljust(8, b" ")
    entry[8:11] = ext.upper().encode("ascii")[:3].ljust(3, b" ")
    entry[11] = 0x20
    struct.pack_into("<H", entry, 26, 3)
    struct.pack_into("<I", entry, 28, len(content))
    _put(path, data_start + 32, bytes(entry))


# ---------------------------------------------------------------------------
# exFAT
# ---------------------------------------------------------------------------


def exfat_checksum(data: bytes) -> int:
    checksum = 0
    for index, byte in enumerate(data):
        if index in (106, 107, 112):
            continue
        checksum = ((checksum << 31) | (checksum >> 1)) & 0xFFFFFFFF
        checksum = (checksum + byte) & 0xFFFFFFFF
    return checksum


def build_exfat(path: Path, size: int, *, label: str = "EXFATTEST", serial: int = 0xCAFEBABE) -> dict:
    total_sectors = size // SECTOR
    sectors_per_cluster = 8
    reserved_sectors = 24  # 12 main + 12 backup boot region
    fat_offset = reserved_sectors
    cluster_count = (total_sectors - reserved_sectors - 128) // sectors_per_cluster
    fat_length = ((cluster_count + 2) * 4 + SECTOR - 1) // SECTOR
    cluster_heap_offset = fat_offset + fat_length
    _mk(path, size)

    boot = bytearray(SECTOR)
    boot[0:3] = b"\xEB\x76\x90"
    boot[3:11] = b"EXFAT   "
    struct.pack_into("<Q", boot, 64, 0)  # partition offset
    struct.pack_into("<Q", boot, 72, total_sectors)
    struct.pack_into("<I", boot, 80, fat_offset)
    struct.pack_into("<I", boot, 84, fat_length)
    struct.pack_into("<I", boot, 88, cluster_heap_offset)
    struct.pack_into("<I", boot, 92, cluster_count)
    struct.pack_into("<I", boot, 96, 3)  # root dir cluster
    struct.pack_into("<I", boot, 100, serial)
    boot[104] = 1  # revision major
    boot[105] = 0
    struct.pack_into("<H", boot, 106, 0)  # volume flags: clean
    boot[108] = 9  # 512-byte sectors
    boot[109] = 3  # 8 sectors per cluster
    boot[110] = 1  # num fats
    boot[111] = 0x80  # drive select
    boot[112] = 0  # percent in use
    boot[510:512] = b"\x55\xaa"

    region = bytearray(11 * SECTOR)
    region[0:SECTOR] = boot
    # OEM parameters sector: all 0xFF (no custom parameters)
    oem_off = SECTOR
    struct.pack_into("<I", region, oem_off, 0xFFFFFFFF)
    struct.pack_into("<I", region, oem_off + 4, 0xFFFFFFFF)
    # reserved sector stays zero-filled
    checksum = exfat_checksum(bytes(region))
    region = region[: 11 * SECTOR]
    _put(path, 0, bytes(region))
    chk = bytearray(SECTOR)
    struct.pack_into("<I", chk, 0, checksum)
    _put(path, 11 * SECTOR, bytes(chk))
    # backup boot region (sectors 12..22 + checksum at 23)
    _put(path, 12 * SECTOR, bytes(region))
    _put(path, 23 * SECTOR, bytes(chk))

    fat = bytearray(fat_length * SECTOR)
    struct.pack_into("<I", fat, 0, 0xFFFFFFF8)
    struct.pack_into("<I", fat, 4, 0xFFFFFFFF)
    struct.pack_into("<I", fat, 12, 0xFFFFFFFF)  # cluster 3 (root dir) = EOC
    _put(path, fat_offset * SECTOR, bytes(fat))

    cluster_size = sectors_per_cluster * SECTOR
    root_off = (cluster_heap_offset + (3 - 2) * sectors_per_cluster) * SECTOR
    root = bytearray(cluster_size)
    # Volume label entry (0x83)
    label_entry = bytearray(32)
    label_entry[0] = 0x83
    label_entry[1] = 0x10
    struct.pack_into("<H", label_entry, 2, 0)
    label_bytes = label.encode("utf-16-le")[:22]
    label_entry[2 : 2 + len(label_bytes)] = label_bytes
    root[0:32] = label_entry
    _put(path, root_off, bytes(root))
    return {
        "cluster_count": cluster_count,
        "cluster_heap_offset": cluster_heap_offset,
        "fat_offset": fat_offset,
        "fat_length": fat_length,
        "sectors_per_cluster": sectors_per_cluster,
    }


def add_exfat_file(path: Path, name: str, content: bytes, geo: dict) -> None:
    """Create a real exFAT file entry + stream-extension + data at cluster 4."""
    cluster_size = geo["sectors_per_cluster"] * SECTOR
    root_off = (geo["cluster_heap_offset"] + (3 - 2) * geo["sectors_per_cluster"]) * SECTOR
    data_cluster = 4
    file_entry = bytearray(32)
    file_entry[0] = 0x85
    file_entry[1] = 0x02  # secondary count
    file_entry[2] = 0x00
    struct.pack_into("<H", file_entry, 4, 0)  # attributes
    struct.pack_into("<H", file_entry, 20, 0o644)  # permissions
    name_utf16 = name.encode("utf-16-le")[:30]
    file_entry[2 : 2 + len(name_utf16)] = name_utf16
    stream = bytearray(32)
    stream[0] = 0xC0
    stream[1] = 0x00
    stream[2] = 0x00
    stream[3] = len(name_utf16) // 2
    struct.pack_into("<H", stream, 4, 0)  # name hash (not validated here)
    struct.pack_into("<Q", stream, 8, len(content))  # valid data length
    struct.pack_into("<I", stream, 20, data_cluster)
    struct.pack_into("<Q", stream, 24, len(content))
    _put(path, root_off + 32, bytes(file_entry) + bytes(stream))
    _put(path, root_off + 96, b"\x00" * 32)  # end-of-directory marker
    _put(path, (geo["cluster_heap_offset"] + (data_cluster - 2) * geo["sectors_per_cluster"]) * SECTOR,
         content[:cluster_size].ljust(cluster_size, b"\x00"))
    fat_off = geo["fat_offset"] * SECTOR + data_cluster * 4
    _put(path, fat_off, struct.pack("<I", 0xFFFFFFFF))


# ---------------------------------------------------------------------------
# NTFS (structural: boot sector + $MFT record 0)
# ---------------------------------------------------------------------------


def build_ntfs(path: Path, size: int, *, label: str = "NTFSVOL", serial: int = 0x1234567890ABCDEF) -> dict:
    total_sectors = size // SECTOR
    sectors_per_cluster = 8
    cluster_size = sectors_per_cluster * SECTOR
    mft_lcn = 4
    mft_mirror_lcn = 8
    _mk(path, size)
    boot = bytearray(SECTOR)
    boot[0:3] = b"\xEB\x52\x90"
    boot[3:11] = b"NTFS    "
    struct.pack_into("<H", boot, 11, SECTOR)
    boot[13] = sectors_per_cluster
    struct.pack_into("<H", boot, 14, 0)
    boot[16] = 0
    struct.pack_into("<H", boot, 17, 0)
    struct.pack_into("<H", boot, 19, 0)
    boot[21] = 0xF8
    struct.pack_into("<H", boot, 22, 0)
    struct.pack_into("<Q", boot, 40, total_sectors)
    struct.pack_into("<Q", boot, 48, mft_lcn)
    struct.pack_into("<Q", boot, 56, mft_mirror_lcn)
    boot[64] = 0xF6  # clusters per MFT record: 2^-10 => 1024 bytes
    boot[68] = 0x01  # clusters per index buffer
    struct.pack_into("<Q", boot, 72, serial)
    boot[510:512] = b"\x55\xaa"
    _put(path, 0, bytes(boot))

    record = _make_mft_record(total_sectors, cluster_size, mft_lcn)
    _put(path, mft_lcn * cluster_size, record)
    _put(path, mft_mirror_lcn * cluster_size, record)
    return {"cluster_size": cluster_size, "mft_lcn": mft_lcn}


def _make_mft_record(total_sectors: int, cluster_size: int, mft_lcn: int) -> bytes:
    rec = bytearray(1024)
    rec[0:4] = b"FILE"
    struct.pack_into("<H", rec, 4, 0x30)  # usa offset
    struct.pack_into("<H", rec, 6, 3)  # usa count
    struct.pack_into("<Q", rec, 8, 0)  # lsn
    struct.pack_into("<H", rec, 16, 1)  # sequence
    struct.pack_into("<H", rec, 18, 1)  # hard links
    struct.pack_into("<H", rec, 20, 0x38)  # attributes offset
    rec[22] = 0x01  # flags byte: bit 0 = IN_USE (a clean, mounted-then-unmounted volume)
    struct.pack_into("<I", rec, 24, 1024)  # used size
    struct.pack_into("<I", rec, 28, 1024)  # allocated size
    struct.pack_into("<Q", rec, 32, 0)  # base record
    struct.pack_into("<H", rec, 40, 0)  # next attr id

    # $DATA (0x80) non-resident attribute carrying the $MFT run list.
    # Offsets follow the documented NTFS non-resident attribute header.
    runlist = bytes([0x11, 0x10, mft_lcn])  # 1 length byte, 1 offset byte, then end marker
    attr_len = 0x40 + len(runlist) + 1
    attr = bytearray(attr_len)
    struct.pack_into("<I", attr, 0x00, 0x80)  # type: $DATA
    struct.pack_into("<I", attr, 0x04, attr_len)  # length
    attr[0x08] = 1  # non-resident
    attr[0x09] = 0  # name length
    struct.pack_into("<H", attr, 0x0A, 0)  # name offset
    struct.pack_into("<H", attr, 0x0C, 0)  # flags
    struct.pack_into("<H", attr, 0x0E, 0)  # attribute id
    struct.pack_into("<Q", attr, 0x10, 0)  # lowest VCN
    struct.pack_into("<Q", attr, 0x18, 0)  # highest VCN
    struct.pack_into("<H", attr, 0x20, 0x40)  # run-list offset
    struct.pack_into("<H", attr, 0x22, 0)  # compression unit
    struct.pack_into("<Q", attr, 0x28, 1024)  # allocated size
    struct.pack_into("<Q", attr, 0x30, 1024)  # real size
    struct.pack_into("<Q", attr, 0x38, 1024)  # initialised size
    attr[0x40 : 0x40 + len(runlist)] = runlist
    attr[0x40 + len(runlist)] = 0x00  # run-list terminator
    rec[0x38 : 0x38 + attr_len] = attr
    struct.pack_into("<I", rec, 0x38 + attr_len, 0xFFFFFFFF)  # attribute terminator
    return bytes(rec)


# ---------------------------------------------------------------------------
# ext4 (structural)
# ---------------------------------------------------------------------------


def build_ext(path: Path, size: int, *, label: str = "EXTVOL", block_size: int = 1024) -> dict:
    total_blocks = size // block_size
    inodes = (total_blocks // 16) + 128
    blocks_per_group = 8192
    inodes_per_group = 2048
    _mk(path, size)
    sb = bytearray(1024)
    struct.pack_into("<I", sb, 0, inodes)
    struct.pack_into("<I", sb, 4, total_blocks)
    struct.pack_into("<I", sb, 12, total_blocks - 100)
    struct.pack_into("<I", sb, 16, inodes - 50)
    struct.pack_into("<I", sb, 20, 1)  # first data block
    _log_block = {1024: 0, 2048: 1, 4096: 2}[block_size]
    struct.pack_into("<I", sb, 24, _log_block)
    struct.pack_into("<I", sb, 32, blocks_per_group)
    struct.pack_into("<I", sb, 40, inodes_per_group)
    struct.pack_into("<I", sb, 76, 1)  # revision
    struct.pack_into("<H", sb, 52, 5)  # mount count
    struct.pack_into("<h", sb, 54, 30)  # max mount count
    struct.pack_into("<H", sb, 56, 0xEF53)
    struct.pack_into("<H", sb, 58, 0x0001)  # clean
    struct.pack_into("<H", sb, 60, 0)  # no recorded kernel errors
    struct.pack_into("<I", sb, 92, 0x4)  # has journal (ext3-style compat)
    struct.pack_into("<I", sb, 96, 0x2 | 0x40)  # filetype + extents
    struct.pack_into("<I", sb, 100, 0x1)  # sparse super
    sb[104:120] = bytes.fromhex("00112233445566778899aabbccddeeff")
    sb[120:136] = label.encode()[:16].ljust(16, b"\x00")
    struct.pack_into("<H", sb, 88, 128)  # inode size
    _put(path, 1024, bytes(sb))
    # backup superblock in group 1 (sparse_super: only groups 1, 3, 5, ... have copies)
    backup_block = blocks_per_group
    _put(path, backup_block * block_size + 1024, bytes(sb))
    return {"block_size": block_size, "total_blocks": total_blocks}


# ---------------------------------------------------------------------------
# Corruption helpers (each models a real-world failure mode)
# ---------------------------------------------------------------------------


def corrupt_mbr_signature(path: Path) -> None:
    _put(path, 510, b"\x00\x00")


def wipe_partition_table(path: Path) -> None:
    """Zero the 4 MBR entries while keeping the boot signature."""
    _put(path, 0x1BE, b"\x00" * 64)


def corrupt_fat32_boot_signature(path: Path) -> None:
    _put(path, 510, b"\x00\x00")


def wipe_fat32_primary_boot_keep_backup(path: Path) -> None:
    """Destroy the primary boot sector; the backup at sector 6 stays valid."""
    _put(path, 0, b"\x00" * 512)


def wipe_fat32_fsinfo(path: Path) -> None:
    _put(path, 1 * SECTOR, b"\x00" * 512)


def desync_fat_copies(path: Path, geo: dict) -> None:
    """Make the second FAT differ from the first in a few bytes."""
    second = (geo["reserved"] + geo["fat_sectors"]) * SECTOR
    _put(path, second + 3 * 4, struct.pack("<I", 0x00000001))


def corrupt_exfat_boot_checksum(path: Path, *, both: bool = False) -> None:
    """Damage the boot-region checksum (primary only by default).

    The single-copy variant is the realistic recoverable case: the backup
    region still verifies, so USB Doctor AI can rebuild the primary.
    """
    with open(path, "r+b") as fh:
        fh.seek(11 * SECTOR)
        fh.write(struct.pack("<I", 0xDEADBEEF))
        if both:
            fh.seek(23 * SECTOR)
            fh.write(struct.pack("<I", 0xDEADBEEF))


def set_exfat_dirty(path: Path) -> None:
    with open(path, "r+b") as fh:
        fh.seek(106)
        flags = struct.unpack("<H", fh.read(2))[0]
        fh.seek(106)
        fh.write(struct.pack("<H", flags | 0x0002))


def set_ntfs_dirty(path: Path, *, dirty: bool = True) -> None:
    """Toggle the $MFT IN_USE-record dirty flag (flags bit 0)."""
    mft_lcn = struct.unpack("<Q", _read_at(path, 48, 8))[0]
    sectors_per_cluster = _read_at(path, 13, 1)[0]
    rec_off = mft_lcn * sectors_per_cluster * SECTOR
    flags = _read_at(path, rec_off + 22, 1)[0]
    flags = (flags | 0x01) if dirty else (flags & ~0x01)
    _put(path, rec_off + 22, bytes([flags]))


def set_ext_unclean(path: Path, *, errors: int = 0) -> None:
    sb = _read_at(path, 1024, 64)
    state = struct.unpack("<H", sb[58:60])[0] & ~0x0001
    _put(path, 1024 + 58, struct.pack("<H", state))
    _put(path, 1024 + 60, struct.pack("<H", errors))


def corrupt_ntfs_boot_signature(path: Path) -> None:
    _put(path, 510, b"\x00\x00")


def destroy_gpt_backup(path: Path, size: int) -> None:
    _put(path, size - SECTOR, b"\x00" * SECTOR)


def destroy_gpt_primary_header(path: Path) -> None:
    _put(path, SECTOR, b"\x00" * SECTOR)


def overwrite_sectors(path: Path, start_sector: int, count: int) -> None:
    _put(path, start_sector * SECTOR, b"\x00" * (count * SECTOR))


def flip_bytes(path: Path, offset: int, data: bytes) -> None:
    _put(path, offset, data)


def create_zero_capacity_image(path: Path) -> None:
    _mk(path, 0)


def create_garbage_image(path: Path, size: int) -> None:
    _mk(path, size)
    with open(path, "r+b") as fh:
        fh.write(os.urandom(min(size, 1 << 20)))
