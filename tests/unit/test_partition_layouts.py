"""Partition-table interpretation.

USB flash drives are usually formatted *partitionless*: the file system's boot
sector sits at LBA 0 and there is no partition table at all. Treating that as a
destroyed table used to make every healthy stick report CRITICAL and offer a
"restore the boot signature" repair that was both pointless and risky.
"""

from __future__ import annotations

import pytest

from usbdoctor.analysis.health import Health
from usbdoctor.analysis.partitions.tables import parse_mbr


def boot_sector(oem: bytes, signature: bytes = b"\x55\xaa", **fields: bytes) -> bytes:
    sector = bytearray(512)
    sector[0:3] = b"\xeb\x58\x90"
    sector[3 : 3 + len(oem)] = oem
    for offset, value in fields.items():
        start = int(offset[1:], 16)
        sector[start : start + len(value)] = value
    sector[510:512] = signature
    return bytes(sector)


class TestParseMbr:
    def test_fat32_superfloppy_is_mbr_with_no_partitions(self):
        sector = boot_sector(b"MSWIN4.1", **{"x52": b"FAT32   "})
        result = parse_mbr(sector, 8 * 1024 * 1024)
        assert result.scheme == "mbr"
        codes = [i.code for i in result.issues]
        assert codes == ["MBR_NO_PARTITIONS"]
        assert result.entries == []

    def test_ntfs_superfloppy(self):
        result = parse_mbr(boot_sector(b"NTFS    "), 8 * 1024 * 1024)
        assert result.scheme == "mbr"
        assert [i.code for i in result.issues] == ["MBR_NO_PARTITIONS"]

    def test_exfat_superfloppy(self):
        result = parse_mbr(boot_sector(b"EXFAT   "), 8 * 1024 * 1024)
        assert result.scheme == "mbr"
        assert [i.code for i in result.issues] == ["MBR_NO_PARTITIONS"]

    def test_missing_signature_is_damage_not_a_layout(self):
        sector = boot_sector(b"NTFS    ", signature=b"\x00\x00")
        result = parse_mbr(sector, 8 * 1024 * 1024)
        assert result.scheme == "none"
        codes = [i.code for i in result.issues]
        assert "MBR_SIGNATURE_MISSING" in codes
        assert "NO_PARTITION_TABLE" not in codes
        # The volume is still recognisable, and the message says so.
        assert result.details.get("volume_at_lba0") is True
        issue = next(i for i in result.issues if i.code == "MBR_SIGNATURE_MISSING")
        assert issue.repairable is True
        assert "file-system boot record" in issue.message

    def test_blank_disk_is_reported_as_missing_signature(self):
        result = parse_mbr(bytes(512), 8 * 1024 * 1024)
        assert result.scheme == "none"
        assert [i.code for i in result.issues] == ["MBR_SIGNATURE_MISSING"]
        assert result.details.get("volume_at_lba0") is None

    def test_truncated_sector(self):
        result = parse_mbr(b"\x00" * 100, 1024)
        assert [i.code for i in result.issues] == ["MBR_TRUNCATED"]

    def test_gpt_protective_mbr_is_not_flagged(self):
        sector = bytearray(boot_sector(b"MSWIN4.1", **{"x52": b"FAT32   "}))
        sector[0x1BE + 4] = 0xEE
        sector[0x1BE + 8 : 0x1BE + 12] = (1).to_bytes(4, "little")
        sector[0x1BE + 12 : 0x1BE + 16] = (1000).to_bytes(4, "little")
        result = parse_mbr(bytes(sector), 8 * 1024 * 1024)
        assert result.scheme == "gpt"
        assert result.entries


class TestSuperfloppyScanVerdict:
    """Healthy partitionless volumes must scan as GOOD, not CRITICAL."""

    @pytest.mark.parametrize(
        "builder,label",
        [("build_fat32", "fat32"), ("build_exfat", "exfat"), ("build_ntfs", "ntfs"), ("build_ext", "ext2")],
    )
    def test_healthy_partitionless_volume_scans_good(self, tmp_path, builder, label):
        from usbdoctor.service import DoctorService
        from usbdoctor.service.engine import ScanOptions
        from tests.fixtures import images as F

        path = tmp_path / f"{label}.img"
        getattr(F, builder)(path, 12 * 1024 * 1024)
        service = DoctorService(state_dir=tmp_path / "state")
        result = service.scan(str(path), ScanOptions(mode="smart", surface_time_budget_s=1))

        assert result.health.verdict == Health.GOOD, [f.code for f in result.health.findings]
        codes = [f.code for f in result.health.findings]
        assert "NO_PARTITION_TABLE" in codes
        assert "MBR_SIGNATURE_MISSING" not in codes
        assert result.plans == [] or all(p.operation_id != "mbr-signature" for p in result.plans)

    def test_damaged_boot_signature_is_still_critical(self, tmp_path):
        from usbdoctor.service import DoctorService
        from usbdoctor.service.engine import ScanOptions
        from tests.fixtures import images as F

        path = tmp_path / "bad.img"
        F.build_fat32(path, 12 * 1024 * 1024)
        F.corrupt_fat32_boot_signature(path)
        service = DoctorService(state_dir=tmp_path / "state")
        result = service.scan(str(path), ScanOptions(mode="smart", surface_time_budget_s=1))

        assert result.health.verdict in (Health.WARNING, Health.CRITICAL)
        # And a repair is offered for the actual damage.
        assert any(p.operation_id == "fat-boot" for p in result.plans)


class TestVerdictText:
    def test_str_is_the_name_not_the_number(self):
        # Before this was fixed, f"{verdict}" produced "4" in CLI output and
        # notifications, because Health is an IntEnum.
        assert str(Health.CRITICAL) == "CRITICAL"
        assert f"{Health.WARNING}" == "WARNING"
        assert str(Health.UNKNOWN) == "UNKNOWN"

    def test_ordering_and_int_behaviour_are_preserved(self):
        assert Health.CRITICAL > Health.WARNING
        assert Health.GOOD == 2
        assert isinstance(Health.CRITICAL, int)
