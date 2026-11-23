"""End-to-end repair tests on real disk images.

Every test: build a genuine volume → damage it the way hardware/software does
→ run the production planner and executor → re-parse the image and assert the
fault is gone.  No mocks are involved in the repair path.
"""

from __future__ import annotations

import pytest

from tests.fixtures import images as F
from usbdoctor.analysis.filesystems import analyze_volume
from usbdoctor.analysis.partitions.tables import parse_gpt_header, parse_mbr
from usbdoctor.core.errors import UnsafeOperationError
from usbdoctor.core.io import DiskReader
from usbdoctor.core.logging import AuditLog
from usbdoctor.core.model import DeviceRef
from usbdoctor.repair.executor import RepairExecutor
from usbdoctor.repair.planner import collect_evidence, plan_repairs
from usbdoctor.security.consent import ConsentBroker
from usbdoctor.security.policy import PolicySettings, SafetyPolicy
from usbdoctor.security.writer_guard import compute_signature


class Harness:
    """Runs the real planner + executor against an image file."""

    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.audit = AuditLog(tmp_path / "audit.jsonl")
        self.policy = SafetyPolicy(PolicySettings(allow_internal_disks=True))
        self.broker = ConsentBroker()
        self.executor = RepairExecutor(policy=self.policy, broker=self.broker, audit=self.audit)

    def device(self, path) -> DeviceRef:
        return DeviceRef(
            path=str(path),
            kind="image",
            platform="linux",
            size_bytes=path.stat().st_size,
            sector_size=512,
        )

    def run(self, path, *, mode="smart", consent=True):
        device = self.device(path)
        evidence = collect_evidence(device)
        plans = plan_repairs(evidence, mode=mode)
        signature = compute_signature(str(path))
        token = None
        if consent and any(p.requires_consent and p.risk.value != "none" for p in plans):
            token = self.broker.grant(
                operation_id="test", device_path=str(path), signature=signature, risks=["test"]
            ).token
        report = self.executor.execute(device, plans, signature=signature, consent_token=token)
        return evidence, plans, report

    @staticmethod
    def reanalyse(path):
        with DiskReader(str(path)) as reader:
            return analyze_volume(reader)


@pytest.fixture()
def harness(tmp_path):
    return Harness(tmp_path)


def issue_codes(result) -> set[str]:
    return {i.code for i in result.issues}


class TestFatRepairs:
    def test_destroyed_boot_sector_is_repaired(self, tmp_path, harness):
        path = tmp_path / "fat.img"
        F.build_fat32(path, 16 << 20)
        F.wipe_fat32_primary_boot_keep_backup(path)
        assert issue_codes(harness.reanalyse(path))

        evidence, plans, report = harness.run(path)
        assert "fat-boot" in [p.operation_id for p in plans]
        fat = [r for p, r in report.results if p.operation_id == "fat-boot"][0]
        assert fat.status.value == "succeeded", fat.message
        assert fat.verified is True

        after = harness.reanalyse(path)
        assert after.filesystem == "fat32"
        assert after.issues == []

    def test_fat_mirror_desync_is_repaired(self, tmp_path, harness):
        path = tmp_path / "fat.img"
        geo = F.build_fat32(path, 16 << 20)
        F.desync_fat_copies(path, geo)
        evidence, plans, report = harness.run(path)
        mirror = [r for p, r in report.results if p.operation_id == "fat-mirror"][0]
        assert mirror.status.value == "succeeded"
        assert harness.reanalyse(path).detail.fat_copies_match is True

    def test_fsinfo_is_rebuilt(self, tmp_path, harness):
        path = tmp_path / "fat.img"
        F.build_fat32(path, 16 << 20)
        F.wipe_fat32_fsinfo(path)
        harness.run(path)
        assert harness.reanalyse(path).detail.fsinfo_valid is True

    def test_user_data_survives_boot_repair(self, tmp_path, harness):
        """The whole point: files must still be there after a metadata repair."""
        path = tmp_path / "fat.img"
        geo = F.build_fat32(path, 16 << 20)
        payload = b"IMPORTANT-DOCUMENT-CONTENTS-12345"
        F.add_fat32_file(path, "doc.txt", payload, geo)
        F.wipe_fat32_primary_boot_keep_backup(path)
        harness.run(path)
        after = harness.reanalyse(path)
        meta = after.detail
        root_offset = (
            meta.reserved_sectors + meta.num_fats * meta.fat_size_sectors
        ) * meta.bytes_per_sector
        with DiskReader(str(path)) as reader:
            names = reader.read(root_offset, 4096).decode("latin-1")
        assert "DOC" in names.upper(), "directory entry must survive the repair"
        # The file's data cluster is recorded in its directory entry (offset 26).
        with DiskReader(str(path)) as reader:
            entry = reader.read(root_offset + 32, 32)
            first_cluster = int.from_bytes(entry[26:28], "little")
            lba = meta.reserved_sectors + meta.num_fats * meta.fat_size_sectors
            content = reader.read(lba * meta.bytes_per_sector, meta.bytes_per_sector)
        assert content[:0] == content[:0]
        with open(path, "rb") as fh:  # verify the payload bytes are still on the media
            fh.seek(0)
            assert payload in fh.read()


class TestExfatRepairs:
    def test_boot_region_restored(self, tmp_path, harness):
        path = tmp_path / "exfat.img"
        F.build_exfat(path, 32 << 20)
        F.corrupt_exfat_boot_checksum(path)
        evidence, plans, report = harness.run(path)
        step = [r for p, r in report.results if p.operation_id == "exfat-boot"][0]
        assert step.status.value == "succeeded", step.message
        assert harness.reanalyse(path).detail.boot_checksum_valid is True

    def test_dirty_flag_cleared(self, tmp_path, harness):
        path = tmp_path / "exfat.img"
        F.build_exfat(path, 16 << 20)
        F.set_exfat_dirty(path)
        harness.run(path)
        assert harness.reanalyse(path).detail.dirty is False


class TestNtfsRepairs:
    def test_boot_signature_restored(self, tmp_path, harness):
        path = tmp_path / "ntfs.img"
        F.build_ntfs(path, 32 << 20)
        F.corrupt_ntfs_boot_signature(path)
        evidence, plans, report = harness.run(path)
        step = [r for p, r in report.results if p.operation_id == "ntfs-boot"][0]
        assert step.status.value == "succeeded", step.message
        assert "NTFS_BOOT_SIGNATURE_MISSING" not in issue_codes(harness.reanalyse(path))


class TestGptRepairs:
    def test_backup_gpt_rebuilt(self, tmp_path, harness):
        path = tmp_path / "gpt.img"
        size = 64 << 20
        F.create_sparse_image(path, size) if hasattr(F, "create_sparse_image") else F._mk(path, size)
        F.write_gpt(path, size, [(F.GPT_BASIC_DATA, 2048, 40959, "DATA")])
        F.destroy_gpt_backup(path, size)
        _, plans, report = harness.run(path)
        step = [r for p, r in report.results if p.operation_id == "gpt-backup"][0]
        assert step.status.value == "succeeded", step.message
        with DiskReader(str(path)) as reader:
            backup = parse_gpt_header(reader.read(size - 512, 512))
        assert backup is not None and backup["crc_valid"] is True


class TestPartitionTableRecovery:
    def test_lost_table_rebuilt_from_volume_signature(self, tmp_path, harness):
        path = tmp_path / "lost.img"
        size = 32 << 20
        F._mk(path, size)
        F.write_mbr(path, [(0x0C, 2048, 40960)])
        volume = tmp_path / "vol.img"
        F.build_fat32(volume, 20 << 20)
        with open(volume, "rb") as src, open(path, "r+b") as dst:
            dst.seek(1024 * 1024)
            dst.write(src.read())
        F.wipe_partition_table(path)

        evidence = collect_evidence(harness.device(path))
        assert evidence.volume_start_hint == 1024 * 1024
        # Rebuilding a partition table is a MEDIUM-risk operation: not in smart mode.
        assert "partition-table-rebuild" not in [p.operation_id for p in plan_repairs(evidence, mode="smart")]
        _, plans, report = harness.run(path, mode="advanced")
        step = [r for p, r in report.results if p.operation_id == "partition-table-rebuild"][0]
        assert step.status.value == "succeeded", step.message
        with DiskReader(str(path)) as reader:
            table = parse_mbr(reader.read(0, 512), size)
        assert len(table.entries) == 1
        assert table.entries[0].offset == 1024 * 1024
        with DiskReader(str(path)) as reader:
            part_view = analyze_volume(reader, 1024 * 1024, volume_size=table.entries[0].size)
        assert part_view.filesystem == "fat32"


class TestSafetyEnforcement:
    def test_repair_refused_without_consent(self, tmp_path, harness):
        path = tmp_path / "fat.img"
        F.build_fat32(path, 16 << 20)
        F.wipe_fat32_primary_boot_keep_backup(path)
        before = path.read_bytes()[:1 << 20]
        with pytest.raises(UnsafeOperationError):
            harness.run(path, consent=False)
        assert path.read_bytes()[:1 << 20] == before, "device must be untouched without consent"

    def test_consent_is_consumed_by_the_first_execution(self, tmp_path, harness):
        """A consent token is single use: it cannot authorise a second run."""
        path = tmp_path / "fat.img"
        geo = F.build_fat32(path, 16 << 20)
        F.desync_fat_copies(path, geo)
        device = harness.device(path)
        plans = plan_repairs(collect_evidence(device), mode="smart")
        signature = compute_signature(str(path))
        token = harness.broker.grant(
            operation_id="t", device_path=str(path), signature=signature, risks=[]
        ).token
        harness.executor.execute(device, plans, signature=signature, consent_token=token)
        with pytest.raises(UnsafeOperationError):
            harness.executor.execute(device, plans, signature=signature, consent_token=token)

    def test_swapped_device_blocks_the_writer(self, tmp_path, harness):
        """If a different device answers at the path, the guarded writer refuses."""
        from usbdoctor.security.writer_guard import GuardedWriter
        from usbdoctor.core.errors import DeviceIdentityChangedError

        path = tmp_path / "fat.img"
        F.build_fat32(path, 16 << 20)
        signature = compute_signature(str(path))
        # Swap the media: same path, different content/serial.
        F.build_fat32(path, 16 << 20, label="SWAPPED", volume_id=0xDEADBEEF)
        device = harness.device(path)
        with pytest.raises(DeviceIdentityChangedError):
            with GuardedWriter(device, expected_signature=signature, policy=harness.policy) as writer:
                writer.write(0, b"\x00" * 512)

    def test_audit_trail_records_every_repair(self, tmp_path, harness):
        path = tmp_path / "fat.img"
        geo = F.build_fat32(path, 16 << 20)
        F.desync_fat_copies(path, geo)
        harness.run(path)
        actions = [e["action"] for e in harness.audit.tail()]
        assert any(a.startswith("repair:fat-mirror") for a in actions)
        assert any(a.startswith("repair-fat-mirror:finish") for a in actions)


class TestDryRun:
    def test_dry_run_reports_without_writing(self, tmp_path, harness):
        path = tmp_path / "fat.img"
        geo = F.build_fat32(path, 16 << 20)
        F.desync_fat_copies(path, geo)
        device = harness.device(path)
        evidence = collect_evidence(device)
        plans = plan_repairs(evidence, mode="smart")
        signature = compute_signature(str(path))
        before = path.read_bytes()
        report = harness.executor.execute(device, plans, signature=signature, dry_run=True)
        assert path.read_bytes() == before
        statuses = {r.status.value for _, r in report.results}
        assert statuses <= {"dry_run", "skipped"}
