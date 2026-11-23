"""Repairs on a PARTITIONED disk — how SSD/HDD volumes are actually laid out.

USB sticks are often partitionless, but a hard disk or SSD has a partition
table and the file system starts at a partition offset (1 MiB on Windows). Every
repair used to act on LBA 0 regardless, which meant:

* the repair silently did nothing (it patched the MBR area), and
* in the worst case it wrote a FAT boot sector over the partition table of a
  perfectly healthy disk.

These tests pin the correct behaviour: the repair must target the volume, and
the partition table must be byte-identical afterwards.
"""

from __future__ import annotations

import struct

import pytest

from tests.fixtures import images as F

from usbdoctor.analysis.health import Health
from usbdoctor.service import DoctorService
from usbdoctor.service.engine import ScanOptions

PART_LBA = 2048          # the conventional 1 MiB alignment
SIZE = 32 * 1024 * 1024
PART_OFFSET = PART_LBA * 512
PART_BYTES = SIZE - PART_OFFSET
TYPE_BYTE = {"fat32": 0x0C, "exfat": 0x07, "ntfs": 0x07, "ext2": 0x83}
BUILDER = {
    "fat32": F.build_fat32,
    "exfat": F.build_exfat,
    "ntfs": F.build_ntfs,
    "ext2": F.build_ext,
}


def build_partitioned(path, fs: str):
    """A disk with one partition at LBA 2048, volume written at that offset."""
    F._mk(path, SIZE)
    scratch = path.with_suffix(".part")
    BUILDER[fs](scratch, PART_BYTES)
    blob = scratch.read_bytes()
    scratch.unlink()
    with path.open("r+b") as handle:
        handle.seek(PART_OFFSET)
        handle.write(blob)
    F.write_mbr(path, [(TYPE_BYTE[fs], PART_LBA, PART_BYTES // 512)])


def damage_boot(path, mutate):
    with path.open("r+b") as handle:
        handle.seek(PART_OFFSET)
        boot = bytearray(handle.read(512))
        mutate(boot)
        handle.seek(PART_OFFSET)
        handle.write(bytes(boot))


def table_intact(path) -> bool:
    raw = path.read_bytes()
    entry_start = struct.unpack("<I", raw[0x1BE + 8 : 0x1BE + 12])[0]
    return (
        raw[510:512] == b"\x55\xaa"
        and entry_start == PART_LBA
        and raw[0x1BE + 4] == TYPE_BYTE["fat32"] or entry_start == PART_LBA
    )


@pytest.fixture()
def service(tmp_path):
    return DoctorService(state_dir=tmp_path / "state")


def scan(service, path):
    return service.scan(str(path), ScanOptions(mode="smart", surface_time_budget_s=2))


def repair_all(service, path):
    result = scan(service, path)
    ops = [plan.operation_id for plan in result.plans]
    if not ops:
        return result, None
    approval = service.approve(str(path), operation_ids=ops, mode="smart")
    return result, service.execute(str(path), consent_token=approval["consent_token"])


class TestHealthyPartitionedDisks:
    @pytest.mark.parametrize("fs", ["fat32", "exfat", "ntfs", "ext2"])
    def test_healthy_volume_at_a_partition_offset_is_good(self, service, tmp_path, fs):
        path = tmp_path / f"{fs}.img"
        build_partitioned(path, fs)
        result = scan(service, path)
        assert result.health.verdict == Health.GOOD, [
            (f.code, f.severity) for f in result.health.findings
        ]
        assert result.plans == []


class TestRepairTargetsTheVolume:
    def test_fat32_lost_boot_signature(self, service, tmp_path):
        path = tmp_path / "fat.img"
        build_partitioned(path, "fat32")
        damage_boot(path, lambda b: b.__setitem__(slice(510, 512), b"\x00\x00"))

        before, report = repair_all(service, path)
        assert before.health.verdict == Health.CRITICAL
        assert report is not None
        step = report["steps"][0]
        assert step["plan"]["operation_id"] == "fat-boot"
        assert step["result"]["status"] == "succeeded"
        assert step["result"]["verified"] is True
        assert scan(service, path).health.verdict == Health.GOOD

    def test_fat32_wiped_primary_restored_from_the_backup_at_sector_6(self, service, tmp_path):
        path = tmp_path / "fat.img"
        build_partitioned(path, "fat32")
        damage_boot(path, lambda b: b.__setitem__(slice(0, 512), bytes(512)))

        before, report = repair_all(service, path)
        assert report is not None, "a wiped primary plus an intact backup must be repairable"
        assert report["steps"][0]["plan"]["operation_id"] == "fat-boot"
        assert report["steps"][0]["result"]["verified"] is True
        assert scan(service, path).health.verdict == Health.GOOD

    def test_exfat_wiped_boot_region(self, service, tmp_path):
        path = tmp_path / "exfat.img"
        build_partitioned(path, "exfat")
        damage_boot(path, lambda b: b.__setitem__(slice(0, 512), bytes(512)))

        _, report = repair_all(service, path)
        assert report is not None
        assert report["steps"][0]["plan"]["operation_id"] == "exfat-boot"
        assert report["steps"][0]["result"]["verified"] is True
        assert scan(service, path).health.verdict == Health.GOOD

    def test_exfat_boot_checksum(self, service, tmp_path):
        path = tmp_path / "exfat.img"
        build_partitioned(path, "exfat")
        damage_boot(path, lambda b: b.__setitem__(slice(11, 12), b"\xff"))

        _, report = repair_all(service, path)
        assert report is not None
        assert report["steps"][0]["result"]["verified"] is True
        assert scan(service, path).health.verdict == Health.GOOD

    def test_ntfs_lost_boot_signature(self, service, tmp_path):
        path = tmp_path / "ntfs.img"
        build_partitioned(path, "ntfs")
        damage_boot(path, lambda b: b.__setitem__(slice(510, 512), b"\x00\x00"))

        _, report = repair_all(service, path)
        assert report is not None
        assert report["steps"][0]["plan"]["operation_id"] == "ntfs-boot"
        assert report["steps"][0]["result"]["verified"] is True
        assert scan(service, path).health.verdict == Health.GOOD


class TestPartitionTableIsNeverDamaged:
    """The failure this file exists for: a 'repair' that eats the MBR."""

    @pytest.mark.parametrize("fs", ["fat32", "exfat", "ntfs"])
    def test_partition_table_survives_every_repair(self, service, tmp_path, fs):
        path = tmp_path / f"{fs}.img"
        build_partitioned(path, fs)
        before_table = path.read_bytes()[:512]

        damage_boot(path, lambda b: b.__setitem__(slice(510, 512), b"\x00\x00"))
        repair_all(service, path)

        after_table = path.read_bytes()[:512]
        assert after_table == before_table, (
            "the partition table changed during a file-system repair"
        )

    def test_mbr_repair_still_works_when_it_is_the_actual_problem(self, service, tmp_path):
        path = tmp_path / "fat.img"
        build_partitioned(path, "fat32")
        # Genuine MBR damage: remove the signature only, keep the entries.
        raw = bytearray(path.read_bytes())
        raw[510:512] = b"\x00\x00"
        path.write_bytes(bytes(raw))

        before = scan(service, path)
        assert any(p.operation_id == "mbr-signature" for p in before.plans)
        approval = service.approve(str(path), operation_ids=["mbr-signature"], mode="smart")
        report = service.execute(str(path), consent_token=approval["consent_token"])
        assert report["steps"][0]["result"]["verified"] is True

        raw_after = path.read_bytes()
        assert raw_after[510:512] == b"\x55\xaa"
        assert struct.unpack("<I", raw_after[0x1BE + 8 : 0x1BE + 12])[0] == PART_LBA
