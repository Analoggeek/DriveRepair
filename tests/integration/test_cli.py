"""CLI behaviour: safety gates, exit codes, and JSON output."""

from __future__ import annotations

import json
import os

import pytest

from tests.fixtures import images as F

from usbdoctor import cli


def run(capsys, *argv, state_dir=None):
    """Invoke the CLI exactly like main() does, returning (code, stdout, stderr)."""
    args = ["--no-color", "--json"] + list(argv)
    if state_dir:
        args = ["--state-dir", str(state_dir)] + args
    code = cli.main(args)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.fixture()
def damaged_image(tmp_path):
    path = tmp_path / "damaged.img"
    geo = F.build_fat32(path, 8 * 1024 * 1024)
    F.add_fat32_file(path, "DATA.TXT", b"recover me", geo)
    data = bytearray(path.read_bytes())
    data[0:512] = b"\x00" * 512
    path.write_bytes(data)
    return path


class TestCliSafety:
    def test_repair_without_approve_refuses_and_exits_2(self, capsys, tmp_path, damaged_image):
        code, out, _ = run(capsys, "repair", str(damaged_image), "--mode", "smart", state_dir=tmp_path / "s")
        assert code == cli.EXIT_UNSAFE
        assert "Refusing to write without --approve" in out

    def test_repair_dry_run_needs_no_approval_and_writes_nothing(self, capsys, tmp_path, damaged_image):
        before = damaged_image.read_bytes()
        code, out, _ = run(capsys, "repair", str(damaged_image), "--mode", "smart", "--dry-run",
                           state_dir=tmp_path / "s")
        assert code == cli.EXIT_OK
        payload = json.loads(out)
        assert payload["steps"][0]["result"]["status"] == "dry_run"
        assert damaged_image.read_bytes() == before

    def test_approved_repair_succeeds_and_verifies(self, capsys, tmp_path, damaged_image):
        code, out, _ = run(capsys, "repair", str(damaged_image), "--mode", "smart", "--approve", "--yes",
                           state_dir=tmp_path / "s")
        assert code == cli.EXIT_OK
        payload = json.loads(out)
        assert payload["succeeded"] == 1
        assert payload["steps"][0]["result"]["verified"] is True

    def test_capacity_without_approve_exits_2(self, capsys, tmp_path, damaged_image):
        code, out, _ = run(capsys, "capacity", str(damaged_image), state_dir=tmp_path / "s")
        assert code == cli.EXIT_UNSAFE


class TestCliOutput:
    def test_scan_json_is_complete_and_honest(self, capsys, tmp_path, damaged_image):
        code, out, _ = run(capsys, "scan", str(damaged_image), "--mode", "smart",
                           "--time-budget", "2", "--no-smart", state_dir=tmp_path / "s")
        assert code == cli.EXIT_OK
        payload = json.loads(out)
        assert payload["health"]["verdict"] in ("WARNING", "CRITICAL")
        assert "FAT_BOOT_SECTOR_DESTROYED" in payload["evidence_codes"]
        assert payload["plans"][0]["operation_id"] == "fat-boot"
        # An image has no hardware behind it — SMART must be absent, not invented.
        assert payload["smart"] is None
        assert any("SMART cannot be read" in note for note in payload["limitations"])

    def test_plan_lists_risk_and_consent(self, capsys, tmp_path, damaged_image):
        code, out, _ = run(capsys, "plan", str(damaged_image), state_dir=tmp_path / "s")
        assert code == cli.EXIT_OK
        payload = json.loads(out)
        assert payload["plans"][0]["risk"] == "low"
        assert payload["plans"][0]["requires_consent"] is True

    def test_status_reports_real_tool_detection(self, capsys, tmp_path):
        code, out, _ = run(capsys, "status", state_dir=tmp_path / "s")
        assert code == cli.EXIT_OK
        payload = json.loads(out)
        assert payload["app"] == "USB Doctor AI"
        assert all(isinstance(v, bool) for v in payload["tools"].values())

    def test_devices_lists_policy(self, capsys, tmp_path):
        code, out, _ = run(capsys, "devices", state_dir=tmp_path / "s")
        assert code == cli.EXIT_OK
        payload = json.loads(out)
        assert "devices" in payload and "policy" in payload

    def test_missing_device_is_a_clean_error_not_a_traceback(self, capsys, tmp_path):
        code, out, _ = run(capsys, "scan", "/dev/definitely-not-here", state_dir=tmp_path / "s")
        assert code == cli.EXIT_ERROR
        assert "error" in json.loads(out)
