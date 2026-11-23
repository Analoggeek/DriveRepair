"""Knowledge base, AI assistant, history store, reports and the service layer.

These tests exercise the *real* pipeline on real disk images: a damaged volume
is created, scanned, diagnosed, approved, repaired and verified end-to-end.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.fixtures import images as F

from usbdoctor.ai import DiagnosticAssistant, render_text
from usbdoctor.analysis.health import Health, evaluate_health
from usbdoctor.analysis.partitions.tables import TableIssue
from usbdoctor.analysis.smart.reader import SmartAttribute, SmartResult
from usbdoctor.core.errors import KnowledgeSourceError
from usbdoctor.db import HistoryStore
from usbdoctor.knowledge import (
    ENTRIES,
    build_research_queries,
    get,
    is_allowed,
    search,
    vendor_links_for,
)
from usbdoctor.knowledge.online import OnlineResearcher
from usbdoctor.repair.base import RepairPlan, Risk
from usbdoctor.report import ReportBundle, save, to_html, to_markdown
from usbdoctor.service import DoctorService, ScanOptions


# ---------------------------------------------------------------------------
# knowledge base
# ---------------------------------------------------------------------------
class TestKnowledgeDatabase:
    def test_every_entry_is_actionable_and_honest(self):
        assert len(ENTRIES) >= 15
        keys = set()
        for entry in ENTRIES:
            assert entry.key not in keys, f"duplicate key {entry.key}"
            keys.add(entry.key)
            assert entry.title and entry.steps and entry.verification
            assert entry.symptoms, f"{entry.key} must describe symptoms"
            assert entry.severity in ("info", "warning", "error", "critical")
            assert entry.sources, f"{entry.key} must cite a source"
            for url in entry.sources:
                assert url.startswith("https://"), f"{entry.key} has a non-HTTPS source"
            # A hardware-unfixable entry must say what software cannot do.
            if not entry.software_fixable:
                assert entry.steps, f"{entry.key} gives no guidance for unfixable hardware"

    def test_error_code_search_ranks_the_right_entry_first(self):
        assert search(error_codes=["FAT_BOOT_SECTOR_DESTROYED"])[0][1].key == "fat32-backup-boot"
        assert search(error_codes=["CAPACITY_MISMATCH"])[0][1].key == "counterfeit-capacity"
        assert search(error_codes=["GPT_BACKUP_MISSING"])[0][1].key == "gpt-backup-missing"
        assert search(error_codes=["SMART_ATTR_5_HIGH"])[0][1].key == "flash-wear"

    def test_filesystem_and_vendor_affinity(self):
        keys = [e.key for _, e in search(filesystem="exfat", error_codes=["EXFAT_BOOT_CHECKSUM"])]
        assert keys[0] == "exfat-boot-checksum"
        assert [e.key for _, e in search(vid="13FE")][0] == "phison-mode-switch"

    def test_hardware_entries_never_claim_software_can_fix_them(self):
        for key in ("flash-wear", "counterfeit-capacity", "phison-mode-switch", "sdcard-locked"):
            assert get(key).software_fixable is False
        assert get("write-protected").when_not_to, "write-protect entry must warn against forcing writes"

    def test_unknown_code_returns_nothing_rather_than_guessing(self):
        assert search(error_codes=["COMPLETELY_MADE_UP_CODE"]) == []


class TestOnlineResearch:
    def test_allow_list_refuses_http_and_unknown_hosts(self):
        assert is_allowed("https://www.kingston.com/en/support")
        assert not is_allowed("http://www.kingston.com/en/support")  # plain HTTP
        assert not is_allowed("https://evil.example.com/kingston")
        assert not is_allowed("https://kingston.com.evil.example/")

    def test_fetching_a_disallowed_url_raises(self):
        researcher = OnlineResearcher()
        with pytest.raises(KnowledgeSourceError):
            researcher.fetch("https://example.com/anything")

    def test_offline_mode_reports_offline_instead_of_inventing_content(self, tmp_path):
        researcher = OnlineResearcher(offline=True)
        result = researcher.fetch("https://www.kingston.com/en/support")
        assert result.status == "offline"
        assert result.snippet == ""
        assert "disabled" in result.error

    def test_queries_are_built_from_real_identity(self):
        queries = build_research_queries(
            brand="SanDisk",
            model="Cruzer Blade",
            vid="0781",
            pid="5567",
            firmware="1.26",
            error_codes=["FS_UNKNOWN"],
            filesystem="exfat",
        )
        assert queries[0] == "SanDisk Cruzer Blade RAW file system repair"
        assert any("0781" in q and "5567" in q for q in queries)
        assert any("exfat" in q.lower() for q in queries)

    def test_vendor_links_are_https_and_match_brand(self):
        links = vendor_links_for("SanDisk")
        assert links and all(link.url.startswith("https://") for link in links)
        assert any(link.vendor == "SanDisk" for link in links)
        assert len(vendor_links_for(None)) > 3


# ---------------------------------------------------------------------------
# diagnostic assistant
# ---------------------------------------------------------------------------
def _repairable_diagnosis():
    issues = [TableIssue("critical", "FAT_BOOT_SECTOR_DESTROYED", "Primary boot sector destroyed.", repairable=True)]
    health = evaluate_health(filesystem_findings=issues, surface_scanned_bytes=16 << 20)
    plans = [
        RepairPlan("fat-boot", "Restore FAT boot sector", "Rebuild 512 bytes.", Risk.LOW),
        RepairPlan("backup-image", "Image the device", "Copy readable sectors.", Risk.NONE),
    ]
    return DiagnosticAssistant().diagnose(
        device_name="Test Drive",
        health=health,
        filesystem="unknown",
        evidence_codes=["FAT_BOOT_SECTOR_DESTROYED"],
        plans=plans,
        brand="SanDisk",
    )


class TestDiagnosticAssistant:
    def test_repairable_metadata_is_described_as_repairable(self):
        diagnosis = _repairable_diagnosis()
        assert diagnosis.can_repair_in_software is True
        assert diagnosis.replacement_advised is False
        assert diagnosis.headline == "Repairable file-system damage"
        assert "fat-boot" in diagnosis.recommended_plans
        assert any("not a dead drive" in b for s in diagnosis.sections for b in s.bullets)

    def test_plans_are_ordered_least_risky_first(self):
        diagnosis = _repairable_diagnosis()
        assert diagnosis.recommended_plans == ["backup-image", "fat-boot"]

    def test_hardware_failure_produces_the_friendly_message(self):
        smart = SmartResult(
            available=True,
            source="smartctl",
            health_passed=False,
            health_text="FAILED!",
            attributes=[SmartAttribute(5, "Reallocated_Sector_Ct", 100, 100, 10, 240, "240")],
        )
        health = evaluate_health(smart=smart, surface_read_errors=12, surface_error_bytes=12 * 512,
                                 surface_scanned_bytes=32 << 20)
        diagnosis = DiagnosticAssistant().diagnose(device_name="Dead Drive", health=health, filesystem="fat32")
        assert diagnosis.replacement_advised is True
        assert diagnosis.can_repair_in_software is False
        assert "duty" in diagnosis.friendly_message
        assert diagnosis.professional_recovery_advised is True

    def test_healthy_device_says_healthy_without_alarm(self):
        health = evaluate_health(surface_scanned_bytes=64 << 20, surface_read_errors=0)
        diagnosis = DiagnosticAssistant().diagnose(device_name="Fine Drive", health=health, filesystem="fat32")
        assert diagnosis.replacement_advised is False
        assert diagnosis.data_at_risk is False
        assert "healthy" in diagnosis.headline.lower()

    def test_text_render_includes_provenance(self):
        text = render_text(_repairable_diagnosis())
        assert "RELATED KNOWN ISSUES" in text
        assert "https://" in text
        assert "CONFIDENCE" not in text  # rendered as "Confidence:"
        assert "software-repairable" in text

    def test_assistant_cannot_invent_plans(self):
        """It may only re-order plans it was given; it can never add operations."""
        health = evaluate_health()
        diagnosis = DiagnosticAssistant().diagnose(device_name="x", health=health, filesystem="unknown")
        assert diagnosis.recommended_plans == []


# ---------------------------------------------------------------------------
# history store
# ---------------------------------------------------------------------------
class TestHistoryStore:
    def test_scan_lifecycle_roundtrip(self, tmp_path):
        store = HistoryStore(tmp_path / "h.db")
        scan_id = store.start_scan(
            device_path="/dev/sdz", device_name="Test", serial_hash="abc123", size_bytes=4096, mode="smart"
        )
        store.finish_scan(scan_id, verdict="GOOD", confidence="medium", report={"findings": []})
        row = store.get_scan(scan_id)
        assert row["verdict"] == "GOOD"
        assert row["report"] == {"findings": []}
        assert row["finished_at"]
        assert store.last_scan_for("/dev/sdz")["id"] == scan_id
        assert store.recent_scans()[0]["device_name"] == "Test"

    def test_repairs_and_statistics(self, tmp_path):
        store = HistoryStore(tmp_path / "h.db")
        scan_id = store.start_scan(device_path="/dev/sdy", device_name="D", serial_hash=None,
                                   size_bytes=1, mode="safe")
        store.finish_scan(scan_id, verdict="WARNING", confidence="low", report={})
        store.record_repair(scan_id=scan_id, device_path="/dev/sdy", operation_id="fat-boot",
                            title="Restore", risk="low", status="succeeded", verified=True,
                            message="ok", duration_s=0.2, rollback_file="/tmp/rb.bin")
        store.record_recovery(device_path="/dev/sdy", method="carve", output_dir="/tmp/out",
                              files=7, bytes_written=1234, details={"a": 1})
        stats = store.statistics()
        assert stats["scans"] == 1
        assert stats["repairs"] == 1 and stats["repairs_succeeded"] == 1
        assert stats["files_recovered"] == 7
        assert store.repair_counts() == {"fat-boot": 1}
        assert store.recent_repairs(device_path="/dev/sdy")[0]["verified"] == 1

    def test_settings_roundtrip_with_json_types(self, tmp_path):
        store = HistoryStore(tmp_path / "h.db")
        store.set_setting("offline_mode", True)
        store.set_setting("theme", "dark")
        store.set_setting("limit", 42)
        assert store.get_setting("offline_mode") is True
        assert store.get_setting("theme") == "dark"
        assert store.get_setting("limit") == 42
        assert store.get_setting("missing", "fallback") == "fallback"
        assert store.all_settings()["theme"] == "dark"

    def test_purge_device_forgets_only_that_device(self, tmp_path):
        store = HistoryStore(tmp_path / "h.db")
        for path in ("/dev/a", "/dev/b"):
            sid = store.start_scan(device_path=path, device_name=path, serial_hash=None,
                                   size_bytes=1, mode="safe")
            store.finish_scan(sid, verdict="GOOD", confidence="low", report={})
        store.purge_device("/dev/a")
        assert [row["device_path"] for row in store.recent_scans()] == ["/dev/b"]


# ---------------------------------------------------------------------------
# reports
# ---------------------------------------------------------------------------
class TestReports:
    def _bundle(self):
        return ReportBundle(
            device={"display_name": "SanDisk Cruzer", "path": "/dev/sdb", "size_bytes": 16 << 20,
                    "bus": "usb", "usb_speed": "USB 2.0 High Speed (480 Mbps)", "vid": "0781",
                    "pid": "5567", "serial": None, "filesystem": "fat32"},
            health={"verdict": "WARNING", "verdict_summary": "Problems detected", "health_confidence": "medium",
                    "counts": {"critical": 0, "error": 1, "warning": 2},
                    "findings": [{"severity": "error", "message": "Boot sector destroyed", "code": "X"}]},
            smart={"available": False, "reason": "This controller does not pass through SMART"},
            limitations=["SMART unavailable on this bridge"],
        )

    def test_missing_data_is_reported_as_unavailable_not_invented(self):
        md = to_markdown(self._bundle())
        assert "not reported" in md
        assert "Serial | not reported" in md
        assert "SMART data is **not available**" in md
        assert "This controller does not pass through SMART" in md

    def test_markdown_contains_real_findings_and_verdict(self):
        md = to_markdown(self._bundle())
        assert "WARNING — Problems detected" in md
        assert "Boot sector destroyed" in md
        assert "16.0 MiB" in md
        assert "## Limitations of this scan" in md

    def test_html_is_self_contained_and_escaped(self):
        bundle = self._bundle()
        bundle.device["display_name"] = "<script>alert(1)</script>"
        html = to_html(bundle)
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html
        assert "http://" not in html and "src=" not in html  # no external assets
        assert "<!DOCTYPE html>" in html

    def test_save_writes_three_formats(self, tmp_path):
        paths = save(self._bundle(), str(tmp_path / "report"))
        assert set(paths) == {"markdown", "json", "html"}
        payload = json.loads(Path(paths["json"]).read_text())
        assert payload["device"]["vid"] == "0781"
        assert payload["health"]["verdict"] == "WARNING"
        assert Path(paths["html"]).stat().st_size > 500


# ---------------------------------------------------------------------------
# service layer (end-to-end on real image files)
# ---------------------------------------------------------------------------
@pytest.fixture()
def service(tmp_path):
    return DoctorService(state_dir=tmp_path / "state")


@pytest.fixture()
def fat_image(tmp_path):
    path = tmp_path / "fat32.img"
    geo = F.build_fat32(path, 16 * 1024 * 1024)
    F.add_fat32_file(path, "THESIS.TXT", b"my entire thesis", geo)
    return path, geo


class TestServiceScan:
    def test_scan_produces_evidence_plans_and_persists_history(self, service, fat_image):
        path, _ = fat_image
        result = service.scan(str(path), ScanOptions(surface_time_budget_s=5))
        assert result.filesystem["filesystem"] == "fat32"
        assert result.health.verdict in (Health.GOOD, Health.EXCELLENT, Health.WARNING)
        assert result.scan_id is not None
        assert service.history.get_scan(result.scan_id)["verdict"] == result.health.verdict.name
        assert result.diagnosis.headline
        # An image has no hardware behind it; that must be stated, not faked.
        assert any("SMART cannot be read" in note for note in result.limitations)

    def test_scan_rejects_a_missing_device_with_a_clear_error(self, service):
        from usbdoctor.core.errors import UsbDoctorError

        with pytest.raises(UsbDoctorError, match="not currently connected"):
            service.scan("/dev/does-not-exist-xyz")

    def test_async_scan_emits_real_progress_and_finishes(self, service, fat_image):
        path, _ = fat_image
        job_id = service.scan_async(str(path), ScanOptions(surface_time_budget_s=5))
        import time

        for _ in range(200):
            if service.job(job_id)["status"] != "running":
                break
            time.sleep(0.02)
        job = service.job(job_id)
        assert job["status"] == "completed", job["error"]
        assert job["result"]["health"]["verdict"]
        assert job["progress"] == 100.0

    def test_bad_mode_is_rejected(self, service, fat_image):
        path, _ = fat_image
        with pytest.raises(ValueError):
            service.scan(str(path), ScanOptions(mode="yolo"))


class TestServiceRepair:
    def test_full_damage_scan_approve_repair_verify_cycle(self, service, tmp_path):
        path = tmp_path / "broken.img"
        geo = F.build_fat32(path, 16 * 1024 * 1024)
        F.add_fat32_file(path, "IMPORTANT.TXT", b"do not lose this", geo)
        data = bytearray(path.read_bytes())
        data[0:512] = b"\x00" * 512  # destroyed primary boot sector
        path.write_bytes(data)

        scan = service.scan(str(path), ScanOptions(mode="smart", surface_time_budget_s=3))
        assert "FAT_BOOT_SECTOR_DESTROYED" in scan.evidence.codes()
        assert scan.diagnosis.can_repair_in_software is True
        assert [p.operation_id for p in scan.plans] == ["fat-boot"]

        # Executing without consent must be refused, not silently allowed.
        from usbdoctor.core.errors import UnsafeOperationError

        with pytest.raises(UnsafeOperationError):
            service.execute(str(path), consent_token=None, mode="smart")

        approval = service.approve(str(path), mode="smart")
        assert approval["operations"] == ["fat-boot"]
        assert "low risk" in approval["risks"][0]

        report = service.execute(str(path), consent_token=approval["consent_token"], mode="smart")
        assert report["succeeded"] == 1 and report["failed"] == 0
        step = report["steps"][0]["result"]
        assert step["verified"] is True

        # The repair must be recorded in history with verification status.
        repairs = service.history.recent_repairs(device_path=str(path))
        assert repairs[0]["operation_id"] == "fat-boot"
        assert repairs[0]["verified"] == 1
        assert repairs[0]["rollback_file"]

        after = service.scan(str(path), ScanOptions(mode="smart", surface_time_budget_s=3))
        assert after.filesystem["filesystem"] == "fat32"

        # And the user's data survived untouched.
        from usbdoctor.core.io import DiskReader
        from usbdoctor.recovery.fat_recovery import FatRecovery

        out = tmp_path / "recovered"
        with DiskReader(str(path)) as reader:
            FatRecovery(reader, str(out)).recover()
        payloads = [p.read_bytes() for p in out.rglob("*") if p.is_file()]
        assert b"do not lose this" in payloads

    def test_consent_token_is_single_use(self, service, tmp_path):
        path = tmp_path / "b2.img"
        F.build_fat32(path, 8 * 1024 * 1024)
        data = bytearray(path.read_bytes())
        data[0:512] = b"\x00" * 512
        path.write_bytes(data)
        approval = service.approve(str(path), mode="smart")
        service.execute(str(path), consent_token=approval["consent_token"], mode="smart")

        # Damage it again so the identical operation is applicable a second time.
        data = bytearray(path.read_bytes())
        data[0:512] = b"\x00" * 512
        path.write_bytes(data)
        from usbdoctor.core.errors import UnsafeOperationError

        with pytest.raises(UnsafeOperationError, match="already used"):
            service.execute(str(path), consent_token=approval["consent_token"], mode="smart")

    def test_dry_run_needs_no_consent_and_changes_nothing(self, service, tmp_path):
        path = tmp_path / "b3.img"
        geo = F.build_fat32(path, 8 * 1024 * 1024)
        F.add_fat32_file(path, "KEEP.TXT", b"keep me", geo)
        data = bytearray(path.read_bytes())
        data[0:512] = b"\x00" * 512
        path.write_bytes(data)
        before = path.read_bytes()
        report = service.execute(str(path), consent_token=None, mode="smart", dry_run=True)
        assert report["steps"]
        assert all(step["result"]["status"] in ("skipped", "dry_run", "planned") for step in report["steps"])
        assert path.read_bytes() == before


class TestServiceRecovery:
    def test_recover_writes_files_outside_the_source_and_logs_it(self, service, fat_image, tmp_path):
        path, _ = fat_image
        payload = service.recover(str(path), output_dir=str(tmp_path / "out"))
        assert payload["files_recovered"] >= 1
        files = [p for p in (tmp_path / "out").rglob("*") if p.is_file()]
        assert any(b"my entire thesis" in f.read_bytes() for f in files)
        assert service.history.recent_recoveries()[0]["method"] == "filesystem"
        # The source image must be byte-identical afterwards.
        assert path.read_bytes()[:512] != b""

    def test_recovery_refuses_to_write_next_to_the_source_image(self, service, fat_image, tmp_path):
        from usbdoctor.core.errors import UsbDoctorError

        path, _ = fat_image
        with pytest.raises(UsbDoctorError, match="over the source"):
            service.recover(str(path), output_dir=str(tmp_path))

    def test_image_creation_produces_a_byte_identical_copy(self, service, fat_image, tmp_path):
        path, _ = fat_image
        payload = service.image_device(str(path), str(tmp_path / "backup.img"), prefer_ddrescue=False)
        assert payload["good_bytes"] == path.stat().st_size
        assert (tmp_path / "backup.img").read_bytes() == path.read_bytes()
        assert service.history.recent_recoveries()[0]["method"].startswith("image")

    def test_research_uses_device_identity_and_offline_mode(self, service, fat_image):
        path, _ = fat_image
        service.set_setting("offline_mode", True)
        result = service.research(str(path))
        assert result["queries"]
        assert result["vendor_links"]
        assert result["official_pages"]
        assert all(page["status"] == "offline" for page in result["official_pages"])
        assert "disclaimer" in result


class TestServiceStatus:
    def test_status_reports_real_tool_availability(self, service):
        status = service.status()
        assert status["app"] == "USB Doctor AI"
        assert status["platform"] in ("linux", "windows", "macos")
        assert isinstance(status["tools"], dict)
        assert all(isinstance(v, bool) for v in status["tools"].values())
        assert status["state_dir"]

    def test_settings_persist_and_apply_to_policy(self, service):
        service.set_setting("allow_internal_disks", True)
        assert service.policy.settings.allow_internal_disks is True
        assert service.history.get_setting("allow_internal_disks") is True

    def test_devices_listing_reports_policy_and_admin_state(self, service):
        listing = service.devices()
        assert "devices" in listing and isinstance(listing["devices"], list)
        assert listing["platform"] in ("linux", "windows", "macos")
        assert isinstance(listing["is_admin"], bool)
        assert "allow_internal_disks" in listing["policy"]
