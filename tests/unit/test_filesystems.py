"""Analyzer tests against genuine on-disk structures."""

from __future__ import annotations

import struct

import pytest

from tests.fixtures import images as F
from usbdoctor.analysis.filesystems import analyze_volume, sniff
from usbdoctor.analysis.filesystems.apfs import ApfsAnalyzer, fletcher64
from usbdoctor.analysis.filesystems.exfat import boot_checksum
from usbdoctor.analysis.filesystems.ntfs import decode_mft_runlist
from usbdoctor.analysis.partitions.tables import (
    analyze_gpt,
    parse_gpt_entries,
    parse_gpt_header,
    parse_guid,
    parse_mbr,
)
from usbdoctor.core.io import DiskReader, create_sparse_image


def codes(result) -> set[str]:
    return {i.code for i in result.issues}


class TestSniff:
    def test_ntfs(self):
        boot = bytearray(512)
        boot[3:11] = b"NTFS    "
        assert sniff(bytes(boot)).filesystem == "ntfs"

    def test_exfat(self):
        boot = bytearray(512)
        boot[3:11] = b"EXFAT   "
        assert sniff(bytes(boot)).filesystem == "exfat"

    def test_fat32(self):
        boot = bytearray(512)
        boot[0] = 0xEB
        boot[82:90] = b"FAT32   "
        assert sniff(bytes(boot)).filesystem == "fat32"

    def test_apfs(self):
        boot = bytearray(512)
        boot[32:36] = b"NXSB"
        assert sniff(bytes(boot)).filesystem == "apfs"

    def test_hfs(self):
        boot = bytearray(512)
        boot[0:2] = b"H+"
        assert sniff(bytes(boot)).filesystem == "hfs+"

    def test_unknown(self):
        assert sniff(b"\x00" * 512).filesystem == "unknown"

    def test_hfs_plus_inside_wrapper_is_detected(self, tmp_path):
        path = tmp_path / "hfs.img"
        create_sparse_image(path, 8 << 20)
        with DiskReader(str(path)) as reader:
            pass
        # wrapper signature at 0, real HFS+ header at 1024 (default block size 512)
        with open(path, "r+b") as fh:
            fh.write(b"BD" + b"\x00" * 18 + struct.pack(">I", 512))
            fh.seek(1024)
            header = bytearray(512)
            header[0:2] = b"H+"
            header[2:4] = struct.pack(">H", 4)
            header[40:44] = struct.pack(">I", 4096)
            header[44:48] = struct.pack(">I", 100)
            header[48:52] = struct.pack(">I", 50)
            fh.write(header)
        from usbdoctor.analysis.filesystems.hfs import analyze_hfs

        with DiskReader(str(path)) as reader:
            result = analyze_hfs(reader)
        assert result.signature == "H+"
        assert any("wrapper" in note.lower() for note in result.notes)


class TestFat:
    def test_healthy_volume_has_no_issues(self, tmp_path):
        path = tmp_path / "fat.img"
        geo = F.build_fat32(path, 32 << 20)
        F.add_fat32_file(path, "readme.txt", b"hello world", geo)
        with DiskReader(str(path)) as reader:
            result = analyze_volume(reader)
        assert result.filesystem == "fat32"
        assert result.issues == []
        assert result.detail.volume_label == "USBTEST"
        assert result.detail.fat_copies_match is True
        assert result.detail.fsinfo_valid is True
        assert "README.TXT" in result.detail.boot["root_names_sample"]

    def test_destroyed_primary_boot_is_detected_as_repairable(self, tmp_path):
        path = tmp_path / "fat.img"
        F.build_fat32(path, 16 << 20)
        F.wipe_fat32_primary_boot_keep_backup(path)
        with DiskReader(str(path)) as reader:
            result = analyze_volume(reader)
        assert "FAT_BOOT_SECTOR_DESTROYED" in codes(result)
        assert len(result.detail["patches"]) == 1
        assert result.detail["patches"][0]["reason_code"] == "FAT32_RESTORE_PRIMARY_BOOT"

    def test_fat_copies_desync_detected(self, tmp_path):
        path = tmp_path / "fat.img"
        geo = F.build_fat32(path, 16 << 20)
        F.desync_fat_copies(path, geo)
        with DiskReader(str(path)) as reader:
            result = analyze_volume(reader)
        assert "FAT_COPIES_DIFFER" in codes(result)
        assert result.detail.fat_diff_bytes > 0

    def test_fsinfo_corruption_detected(self, tmp_path):
        path = tmp_path / "fat.img"
        F.build_fat32(path, 16 << 20)
        F.wipe_fat32_fsinfo(path)
        with DiskReader(str(path)) as reader:
            result = analyze_volume(reader)
        assert "FAT32_FSINFO_INVALID" in codes(result)

    def test_volume_larger_than_device_is_flagged(self, tmp_path):
        """Counterfeit-capacity signature: geometry claims more than exists."""
        path = tmp_path / "fat.img"
        geo = F.build_fat32(path, 16 << 20)
        with open(path, "r+b") as fh:  # lie about the size in the BPB
            fh.seek(32)
            fh.write(struct.pack("<I", geo["clusters"] * 8))
        with DiskReader(str(path)) as reader:
            result = analyze_volume(reader)
        assert "FAT_VOLUME_BEYOND_DEVICE" in codes(result)

    def test_blank_volume_reports_blank(self, tmp_path):
        path = tmp_path / "blank.img"
        create_sparse_image(path, 4 << 20)
        with DiskReader(str(path)) as reader:
            result = analyze_volume(reader)
        assert result.filesystem == "unknown"
        assert from_codes(result, "FS_BLANK")


def from_codes(result, code: str) -> bool:
    issues = result.issues
    if issues and hasattr(issues[0], "code"):
        return code in {i.code for i in issues}
    return False


class TestExfat:
    def test_healthy_volume(self, tmp_path):
        path = tmp_path / "exfat.img"
        geo = F.build_exfat(path, 64 << 20)
        F.add_exfat_file(path, "photo.jpg", b"\xff\xd8\xff" + b"x" * 100, geo)
        with DiskReader(str(path)) as reader:
            result = analyze_volume(reader)
        assert result.filesystem == "exfat"
        assert result.issues == []
        assert result.detail.boot_checksum_valid is True
        assert result.detail.backup_boot_present is True
        assert result.detail.root_files == 1

    def test_checksum_corruption_detected_and_restorable(self, tmp_path):
        path = tmp_path / "exfat.img"
        F.build_exfat(path, 32 << 20)
        F.corrupt_exfat_boot_checksum(path)
        with DiskReader(str(path)) as reader:
            result = analyze_volume(reader)
        assert "EXFAT_BOOT_CHECKSUM" in codes(result)
        assert result.detail.boot_region_restorable is True

    def test_dirty_flag(self, tmp_path):
        path = tmp_path / "exfat.img"
        F.build_exfat(path, 16 << 20)
        F.set_exfat_dirty(path)
        with DiskReader(str(path)) as reader:
            result = analyze_volume(reader)
        assert "EXFAT_DIRTY" in codes(result)
        assert result.detail.dirty is True

    def test_checksum_algorithm_roundtrip(self):
        data = bytearray(b"\x00" * 512)
        data[3:11] = b"EXFAT   "
        assert boot_checksum(bytes(data)) == boot_checksum(bytes(data))


class TestNtfs:
    def test_healthy_volume(self, tmp_path):
        path = tmp_path / "ntfs.img"
        F.build_ntfs(path, 32 << 20)
        with DiskReader(str(path)) as reader:
            result = analyze_volume(reader)
        assert result.filesystem == "ntfs"
        assert result.issues == []
        assert result.detail.dirty is False
        assert result.detail.mft_runs == [(4, 16)]
        assert result.detail.mft_record_size == 1024

    def test_boot_signature_damage(self, tmp_path):
        path = tmp_path / "ntfs.img"
        F.build_ntfs(path, 16 << 20)
        F.corrupt_ntfs_boot_signature(path)
        with DiskReader(str(path)) as reader:
            result = analyze_volume(reader)
        assert "NTFS_BOOT_SIGNATURE_MISSING" in codes(result)

    def test_runlist_decoding(self):
        assert decode_mft_runlist(bytes([0x11, 0x10, 0x04, 0x00])) == [(4, 16)]
        # sparse run then real run
        assert decode_mft_runlist(bytes([0x01, 0x05, 0x11, 0x02, 0x03, 0x00])) == [(3, 2)]


class TestExt:
    def test_clean_volume(self, tmp_path):
        path = tmp_path / "ext.img"
        F.build_ext(path, 32 << 20)
        with DiskReader(str(path)) as reader:
            result = analyze_volume(reader)
        assert result.filesystem in ("ext2", "ext3", "ext4")
        assert result.issues == []
        assert result.detail.volume_name == "EXTVOL"

    def test_unclean_and_errors(self, tmp_path):
        path = tmp_path / "ext.img"
        F.build_ext(path, 16 << 20)
        F.set_ext_unclean(path, errors=3)
        with DiskReader(str(path)) as reader:
            result = analyze_volume(reader)
        assert "EXT_NOT_CLEAN" in codes(result)
        assert "EXT_ERRORS_FLAGGED" in codes(result)


class TestApfs:
    def test_fletcher_checksum_is_used(self, tmp_path):
        path = tmp_path / "apfs.img"
        create_sparse_image(path, 4 << 20)
        block = bytearray(4096)
        block[32:36] = b"NXSB"
        block[36:40] = struct.pack("<I", 4096)
        block[40:48] = struct.pack("<Q", 64)
        block[0:8] = b"\x00" * 8
        block[0:8] = struct.pack("<Q", fletcher64(bytes(block)))
        with open(path, "r+b") as fh:
            fh.write(block)
        with DiskReader(str(path)) as reader:
            result = ApfsAnalyzer(reader).analyze()
        assert result.checksum_valid is True
        assert result.block_size == 4096


class TestPartitionTables:
    def test_mbr_parse(self, tmp_path):
        path = tmp_path / "mbr.img"
        create_sparse_image(path, 16 << 20)
        F.write_mbr(path, [(0x06, 2048, 20480), (0x0C, 22528, 10000)])
        with DiskReader(str(path)) as reader:
            result = parse_mbr(reader.read(0, 512), reader.size())
        assert result.scheme == "mbr"
        assert len(result.entries) == 2
        assert result.entries[0].type_label == "FAT16"
        assert result.entries[1].type_label == "FAT32 (LBA)"
        assert result.entries[0].bootable is True

    def test_mbr_overlap_detected(self, tmp_path):
        path = tmp_path / "mbr.img"
        create_sparse_image(path, 16 << 20)
        F.write_mbr(path, [(0x06, 2048, 8192), (0x0C, 4096, 8192)])
        with DiskReader(str(path)) as reader:
            result = parse_mbr(reader.read(0, 512), reader.size())
        assert "MBR_OVERLAP" in {i.code for i in result.issues}

    def test_mbr_beyond_disk_detected(self, tmp_path):
        path = tmp_path / "mbr.img"
        create_sparse_image(path, 1 << 20)
        F.write_mbr(path, [(0x06, 2048, 2_000_000)])
        with DiskReader(str(path)) as reader:
            result = parse_mbr(reader.read(0, 512), reader.size())
        assert "MBR_PARTITION_BEYOND_DISK" in {i.code for i in result.issues}

    def test_missing_signature(self, tmp_path):
        path = tmp_path / "mbr.img"
        create_sparse_image(path, 1 << 20)
        F.write_mbr(path, [(0x06, 2048, 100)])
        F.corrupt_mbr_signature(path)
        with DiskReader(str(path)) as reader:
            result = parse_mbr(reader.read(0, 512), reader.size())
        assert result.scheme == "none"
        assert "MBR_SIGNATURE_MISSING" in {i.code for i in result.issues}

    def test_gpt_full_roundtrip(self, tmp_path):
        path = tmp_path / "gpt.img"
        size = 64 << 20
        create_sparse_image(path, size)
        F.write_gpt(path, size, [(F.GPT_BASIC_DATA, 2048, 40959, "DATA"), (F.GPT_EFI, 40960, 43007, "EFI")])
        with DiskReader(str(path)) as reader:
            header = parse_gpt_header(reader.read(512, 512))
            entries, crc_ok = parse_gpt_entries(reader.read(1024, 128 * 128), header)
            backup = parse_gpt_header(reader.read(size - 512, 512))
            backup_entries, backup_crc = parse_gpt_entries(
                reader.read((size // 512 - 33) * 512, 128 * 128), backup
            )
            result = analyze_gpt(
                header, entries, crc_ok, backup_header=backup,
                backup_entries=backup_entries, backup_crc_ok=backup_crc, disk_size=size,
            )
        assert header["crc_valid"] and crc_ok and backup_crc
        assert result.issues == []
        assert [e.name for e in result.entries] == ["DATA", "EFI"]
        assert result.entries[0].size == (40959 - 2048 + 1) * 512

    def test_gpt_missing_backup_detected(self, tmp_path):
        path = tmp_path / "gpt.img"
        size = 32 << 20
        create_sparse_image(path, size)
        F.write_gpt(path, size, [(F.GPT_BASIC_DATA, 2048, 40959, "DATA")])
        F.destroy_gpt_backup(path, size)
        with DiskReader(str(path)) as reader:
            header = parse_gpt_header(reader.read(512, 512))
            entries, crc_ok = parse_gpt_entries(reader.read(1024, 128 * 128), header)
            result = analyze_gpt(header, entries, crc_ok, backup_header=None, disk_size=size)
        assert "GPT_BACKUP_MISSING" in {i.code for i in result.issues}

    def test_bad_primary_still_recoverable_from_backup(self, tmp_path):
        path = tmp_path / "gpt.img"
        size = 32 << 20
        create_sparse_image(path, size)
        F.write_gpt(path, size, [(F.GPT_BASIC_DATA, 2048, 40959, "DATA")])
        F.destroy_gpt_primary_header(path)
        with DiskReader(str(path)) as reader:
            assert parse_gpt_header(reader.read(512, 512)) is None
            backup = parse_gpt_header(reader.read(size - 512, 512))
            entries, crc = parse_gpt_entries(reader.read((size // 512 - 33) * 512, 128 * 128), backup)
        assert backup is not None and crc and len(entries) == 1

    def test_guid_byte_order(self):
        import uuid

        raw = bytes.fromhex("28" "73" "2a" "c1" "1f" "f8" "d2" "11" "ba4b" "00a0c93ec93b")
        assert parse_guid(raw) == "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
        assert uuid.UUID(parse_guid(raw)).version is not None
