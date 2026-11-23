"""Static checks for the packaging that ships to users.

PyInstaller and Inno Setup only run on Windows/macOS, so these tests catch the
mistakes that would otherwise surface as a broken installer hours later: a
missing data file, an unimportable hidden import, a malformed Inno script.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPECS = [
    pytest.param(ROOT / "installers/USBDoctorAI.spec", id="windows"),
    pytest.param(ROOT / "installers/macos/USBDoctorAI.spec", id="macos"),
]
ISS = ROOT / "installers/windows/usbdoctor.iss"


def spec_source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class TestPyInstallerSpecs:
    @pytest.mark.parametrize("spec", SPECS)
    def test_spec_is_valid_python(self, spec):
        ast.parse(spec_source(spec))

    @pytest.mark.parametrize("spec", SPECS)
    def test_bundles_the_web_frontend(self, spec):
        source = spec_source(spec)
        for name in ("index.html", "app.css", "app.js", "assets"):
            assert name in source, f"{spec.name} does not bundle {name}"

    @pytest.mark.parametrize("spec", SPECS)
    def test_hidden_imports_actually_exist(self, spec):
        """The GUI imports its pages lazily; a typo here is only caught at run time."""
        source = spec_source(spec)
        block = re.search(r"hiddenimports\s*=\s*\[(.*?)\]", source, re.S)
        assert block, f"{spec.name} has no hiddenimports list"
        modules = re.findall(r'"([\w.]+)"', block.group(1))
        assert modules, "hiddenimports is empty"
        import importlib

        for module in modules:
            # GUI modules need PySide6; skip rather than fail when it is absent,
            # because the module name is what is under test, not the dependency.
            try:
                importlib.import_module(module)
            except ModuleNotFoundError as exc:
                if exc.name == "PySide6":
                    pytest.skip("PySide6 not installed")
                pytest.fail(f"{spec.name} lists a hidden import that does not exist: {module}")

    @pytest.mark.parametrize("spec", SPECS)
    def test_icon_reference_is_resolvable(self, spec):
        source = spec_source(spec)
        icon = "app.ico" if spec.parent.name == "installers" else "app.icns"
        assert icon in source
        assert (ROOT / "usbdoctor/frontend/web/assets" / icon).exists()


class TestInnoSetupScript:
    def test_has_every_required_section(self):
        text = ISS.read_text(encoding="utf-8")
        sections = set(re.findall(r"^\[([A-Za-z]+)\]$", text, re.M))
        assert {"Setup", "Files", "Icons", "Run", "Code"} <= sections

    def test_appid_is_a_single_braced_guid(self):
        """Inno requires AppId={{GUID} — the braces are escaped, the GUID is not."""
        text = ISS.read_text(encoding="utf-8")
        line = re.search(r"^AppId=(.*)$", text, re.M).group(1)
        assert line.startswith("{{"), line
        assert line.endswith("}"), line
        assert not line.startswith("{{{{"), "AppId is over-escaped"
        guid = line[2:-1]
        assert re.fullmatch(
            r"[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}", guid
        ), guid

    def test_referenced_sources_exist(self):
        text = ISS.read_text(encoding="utf-8")
        for raw in re.findall(r'Source: "\{#ProjectRoot \+ "\\([^"]+)"\}"', text):
            base = raw.replace("\\\\", "\\").split("*")[0].rstrip("\\") or "."
            assert (ROOT / base).exists(), f"installer references a missing path: {raw}"

    def test_bundles_docs_license_and_readme(self):
        text = ISS.read_text(encoding="utf-8")
        for expected in ("docs", "README.md", "LICENSE"):
            assert expected in text

    def test_verifies_the_installation_after_copying(self):
        text = ISS.read_text(encoding="utf-8")
        assert "selfcheck" in text, "the installer does not verify what it installed"
        assert "CurStepChanged" in text, "no completeness guard for a silently empty install"


class TestBuildScripts:
    def test_release_builder_refuses_other_platforms(self):
        source = (ROOT / "tools/build_release.py").read_text(encoding="utf-8")
        assert "does not cross-compile" in source or "not a supported build host" in source

    def test_release_builder_verifies_the_frozen_product(self):
        source = (ROOT / "tools/build_release.py").read_text(encoding="utf-8")
        assert "selfcheck" in source

    def test_macos_script_is_valid_shell(self):
        import shutil
        import subprocess

        if not shutil.which("bash"):
            pytest.skip("bash not available")
        result = subprocess.run(
            ["bash", "-n", str(ROOT / "installers/macos/build.sh")], capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr

    def test_workflows_are_valid_yaml(self):
        yaml = pytest.importorskip("yaml", reason="PyYAML not installed")
        for workflow in (ROOT / ".github/workflows").glob("*.yml"):
            data = yaml.safe_load(workflow.read_text())
            assert "jobs" in data, workflow

    def test_release_workflow_builds_both_platforms(self):
        yaml = pytest.importorskip("yaml", reason="PyYAML not installed")
        data = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
        jobs = data["jobs"]
        assert any("windows" in str(spec.get("runs-on", "")) for spec in jobs.values())
        macos = jobs["macos"]
        assert "matrix" in macos["strategy"], "macOS must build both architectures"


class TestTestSuitePortability:
    """Guards against tests that only pass on the machine they were written on.

    Two bugs already reached CI this way: a subprocess test that ran ``echo`` and
    ``sleep`` (neither is a program on Windows) and async tests that waited a
    fixed number of 20 ms ticks (a hidden ~6 s performance budget that a loaded
    CI runner can miss).
    """

    TEST_FILES = sorted((ROOT / "tests").rglob("test_*.py"))

    def test_no_shell_utilities_are_executed(self):
        offenders = []
        for path in self.TEST_FILES:
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if "run_process([" not in line:
                    continue
                for tool in ("echo", "sleep", "cat", "ls", "rm", "true"):
                    if f'"{tool}"' in line:
                        offenders.append(f"{path.name}:{number} runs POSIX tool {tool!r}")
        assert not offenders, (
            "use sys.executable instead of shell utilities (they do not exist on Windows):\n  "
            + "\n  ".join(offenders)
        )

    def test_no_fixed_iteration_waits(self):
        """Waiting must be deadline-based, via tests.support.wait_for."""
        import re

        offenders = []
        for path in self.TEST_FILES:
            text = path.read_text(encoding="utf-8")
            for match in re.finditer(r"for _ in range\((\d+)\)", text):
                window = text[match.start() : match.start() + 400]
                if "sleep" in window:
                    line = text[: match.start()].count("\n") + 1
                    offenders.append(f"{path.name}:{line}")
        assert not offenders, (
            "fixed-iteration sleeps encode a machine-speed assumption; "
            "use tests.support.wait_for instead: " + ", ".join(offenders)
        )

    def test_support_helper_exists_and_is_used(self):
        assert (ROOT / "tests/support.py").exists()
        users = [p.name for p in self.TEST_FILES if "tests.support import" in p.read_text(encoding="utf-8")]
        assert users, "no test uses the shared deadline-based wait helper"


class TestReleaseBuilderOutput:
    """The builder is driven by CI, so its output is a contract."""

    def _run(self, *args):
        import subprocess
        import sys

        return subprocess.run(
            [sys.executable, str(ROOT / "tools/build_release.py"), *args],
            capture_output=True,
            text=True,
            cwd=ROOT,
        )

    def test_json_mode_emits_only_json_on_stdout(self):
        """`--json > report.json` must produce a parseable file, failure included."""
        import json
        import platform

        result = self._run("--json")
        assert result.stdout.strip(), "nothing on stdout in --json mode"
        document = json.loads(result.stdout)  # raises if progress logs leaked in
        assert document["host"] == platform.system()
        assert document["status"] in ("ok", "failed")

    def test_unsupported_host_fails_with_a_clear_reason(self):
        import json
        import platform

        if platform.system() in ("Windows", "Darwin"):
            pytest.skip("this host can actually build")
        result = self._run("--json")
        assert result.returncode == 1
        assert "cross-compile" in json.loads(result.stdout)["error"]
        assert "release.yml" in result.stderr

    def test_progress_goes_to_stderr_in_json_mode(self):
        result = self._run("--json")
        assert "==>" not in result.stdout
