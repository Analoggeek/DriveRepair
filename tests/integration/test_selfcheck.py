"""The `selfcheck` verb the installers depend on.

This is the only thing standing between a broken package and a user, so it must
be honest: every check reports a real result, and a missing dependency makes it
fail rather than pass silently.
"""

from __future__ import annotations

import json

import pytest

from usbdoctor import cli


class TestSelfCheck:
    def test_reports_each_area(self, capsys):
        code = cli.main(["selfcheck"])
        out = capsys.readouterr().out
        for name in ("packaged data", "engine", "cli verbs"):
            assert name in out
        # The GUI checks depend on PySide6 being installed; they are either
        # reported as PASS or, without the dependency, as FAIL — never omitted.
        assert "gui modules" in out
        assert "desktop window" in out
        assert "self-check:" in out
        assert code in (0, 1)

    def test_json_mode_is_parseable_and_matches_the_exit_code(self, capsys):
        code = cli.main(["selfcheck", "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is (code == 0)
        assert {c["name"] for c in payload["checks"]} == {
            "packaged data", "gui modules", "desktop window", "engine", "cli verbs"
        }
        for check in payload["checks"]:
            assert isinstance(check["passed"], bool)
            assert isinstance(check["detail"], str)

    def test_engine_check_runs_in_a_scratch_directory(self, capsys, monkeypatch, tmp_path):
        """It must not touch the user's real state directory."""
        monkeypatch.setenv("USBDOCTOR_STATE_DIR", str(tmp_path / "should-not-be-used"))
        cli.main(["selfcheck", "--json"])
        payload = json.loads(capsys.readouterr().out)
        engine = next(c for c in payload["checks"] if c["name"] == "engine")
        assert engine["passed"] is True
        assert not (tmp_path / "should-not-be-used").exists()

    def test_a_missing_asset_is_reported_as_a_failure(self, capsys, monkeypatch, tmp_path):
        import usbdoctor.cli as module

        class FakeFile:
            def __init__(self, path):
                self._path = path

            def resolve(self):
                return self

            @property
            def parent(self):
                return self._path

        # Point the packaged-data check at an empty directory.
        monkeypatch.setattr(module, "__file__", str(tmp_path / "cli.py"))
        code = cli.main(["selfcheck", "--json"])
        payload = json.loads(capsys.readouterr().out)
        data = next(c for c in payload["checks"] if c["name"] == "packaged data")
        assert data["passed"] is False
        assert "missing packaged data" in data["detail"]
        assert code == 1

    def test_launcher_routes_selfcheck_to_the_cli(self):
        import launcher

        assert "selfcheck" in (
            "devices", "status", "scan", "plan", "repair", "image", "recover",
            "capacity", "report", "research", "history", "serve", "selfcheck",
        )
        # The routing list in launcher.py must contain every verb the CLI defines.
        parser = cli.build_parser()
        from argparse import _SubParsersAction

        actions = [a for a in parser._actions if isinstance(a, _SubParsersAction)]
        verbs = set(actions[0].choices)
        source = open(launcher.__file__, encoding="utf-8").read()
        for verb in verbs:
            assert f'"{verb}"' in source, f"launcher.py does not route the {verb} verb"
