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
        import os
        import shutil
        import subprocess

        if os.name == "nt":
            pytest.skip("bash -n on Git Bash cannot read a Windows path; the macOS job checks this")

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

        result = self._run("--json", "--dry-run")
        assert result.stdout.strip(), "nothing on stdout in --json mode"
        document = json.loads(result.stdout)  # raises if progress logs leaked in
        assert document["host"] == platform.system()
        assert document["status"] in ("ok", "failed")

    def test_unsupported_host_fails_with_a_clear_reason(self):
        import json
        import platform

        if platform.system() in ("Windows", "Darwin"):
            pytest.skip("this host can actually build")
        result = self._run("--json", "--dry-run")
        assert result.returncode == 1
        assert "supported build host" in json.loads(result.stdout)["error"]
        assert "release.yml" in result.stderr

    def test_progress_goes_to_stderr_in_json_mode(self):
        result = self._run("--json", "--dry-run")
        assert "==>" not in result.stdout


class TestFailureReporting:
    """This script is how a red CI build explains itself."""

    SCRIPT = ROOT / ".github/scripts/report_failures.py"

    def _run(self, *args):
        import subprocess
        import sys

        return subprocess.run(
            [sys.executable, str(self.SCRIPT), *args], capture_output=True, text=True
        )

    @staticmethod
    def _junit(tmp_path, message="assert 404 == 200", with_file=True):
        attributes = ' file="tests/integration/test_api.py"' if with_file else ""
        xml = f'''<?xml version="1.0"?>
<testsuites><testsuite name="pytest" tests="2" failures="1">
  <testcase classname="tests.integration.test_api.TestApi" name="test_status"{attributes}>
    <failure message="{message}">traceback</failure>
  </testcase>
  <testcase classname="tests.unit.test_ok" name="test_passes"/>
</testsuite></testsuites>'''
        path = tmp_path / "junit.xml"
        path.write_text(xml)
        return path

    def test_a_failure_becomes_a_named_annotation(self, tmp_path):
        result = self._run(str(self._junit(tmp_path)))
        assert result.returncode == 0
        assert "::error file=tests/integration/test_api.py::" in result.stdout
        assert "TestApi.test_status" in result.stdout
        assert "assert 404 == 200" in result.stdout

    def test_passing_tests_are_not_annotated(self, tmp_path):
        result = self._run(str(self._junit(tmp_path)))
        assert "test_passes" not in result.stdout

    def test_a_multiline_message_stays_on_one_line(self, tmp_path):
        """A raw newline would truncate the workflow command mid-message."""
        result = self._run(str(self._junit(tmp_path, message="line one&#10;line two")))
        body = [line for line in result.stdout.splitlines() if line.startswith("::error")]
        assert len(body) == 1, result.stdout
        assert "line one" in body[0] and "line two" in body[0]

    def test_percent_signs_are_escaped(self, tmp_path):
        """GitHub decodes %XX in workflow commands; a bare % breaks the message."""
        result = self._run(str(self._junit(tmp_path, message="usage was 90%")))
        assert "%25" in result.stdout

    def test_missing_report_is_itself_an_error(self, tmp_path):
        result = self._run(str(tmp_path / "absent.xml"))
        assert result.returncode == 1
        assert "::error::" in result.stdout

    def test_missing_file_attribute_is_tolerated(self, tmp_path):
        result = self._run(str(self._junit(tmp_path, with_file=False)))
        assert result.returncode == 0
        assert "::error::FAILED" in result.stdout


class TestCollectionErrorReporting:
    """A collection error has an empty `message`; its text holds the reason."""

    SCRIPT = ROOT / ".github/scripts/report_failures.py"

    def test_the_traceback_is_reported_when_message_is_empty(self, tmp_path):
        import subprocess
        import sys

        xml = '''<?xml version="1.0"?>
<testsuites><testsuite name="pytest" errors="1" tests="1">
  <testcase classname="" name="tests/integration/test_desktop.py">
    <error message="">ImportError: libGL.so.1: cannot open shared object file</error>
  </testcase>
</testsuite></testsuites>'''
        report = tmp_path / "junit.xml"
        report.write_text(xml)
        result = subprocess.run(
            [sys.executable, str(self.SCRIPT), str(report)], capture_output=True, text=True
        )
        assert result.returncode == 0
        assert "libGL.so.1" in result.stdout, result.stdout
        assert "collection of tests/integration/test_desktop.py" in result.stdout


class TestRunnerLabels:
    """A retired runner label is invisible: the job queues and never starts.

    That is exactly how this release was blocked. GitHub accepts a job for
    ``macos-13`` and no runner ever claims it, so the run looks healthy and
    pending instead of failing. ``macos-13`` was retired in December 2025 and
    ``macos-14`` on 2 November 2026; the live Intel label is ``macos-15-intel``.
    """

    RETIRED = {"macos-12", "macos-13", "macos-14", "macos-13-large", "macos-14-large"}

    def _workflows(self):
        yaml = pytest.importorskip("yaml", reason="PyYAML not installed")
        return {
            path.name: yaml.safe_load(path.read_text())
            for path in (ROOT / ".github/workflows").glob("*.yml")
        }

    def test_no_retired_macos_runner_labels(self):
        offenders = []
        for name, workflow in self._workflows().items():
            for job_name, job in workflow.get("jobs", {}).items():
                labels = [job.get("runs-on")]
                for leg in (job.get("strategy", {}).get("matrix", {}).get("include") or []):
                    labels.append(leg.get("runner"))
                for label in labels:
                    if isinstance(label, str) and label in self.RETIRED:
                        offenders.append(f"{name}:{job_name} uses retired {label}")
        assert not offenders, "\n".join(offenders)

    def test_the_intel_macos_leg_uses_an_intel_label(self):
        """macos-latest/macos-15 are arm64; requesting x64 on them builds the wrong arch."""
        workflow = self._workflows()["release.yml"]
        legs = workflow["jobs"]["macos"]["strategy"]["matrix"]["include"]
        intel = [leg for leg in legs if leg["arch"] == "x64"]
        assert len(intel) == 1, legs
        assert "intel" in intel[0]["runner"], (
            f"{intel[0]['runner']} is an arm64 image; the x64 build needs an -intel label"
        )

    def test_build_jobs_have_a_timeout(self):
        """Without one, a stalled queue stays pending forever instead of going red."""
        workflow = self._workflows()["release.yml"]
        for job_name in ("windows", "macos"):
            assert workflow["jobs"][job_name].get("timeout-minutes"), (
                f"{job_name} has no timeout; a queued job can hang indefinitely"
            )


class TestSpecRootResolution:
    """A spec must find the repo root, not count parents.

    The macOS spec used ``SPECPATH.parent.parent.parent``, one level too many, so
    the build failed on the machine with ``script '.../launcher.py' not found``.
    """

    SPECS = [ROOT / "installers/USBDoctorAI.spec", ROOT / "installers/macos/USBDoctorAI.spec"]

    @pytest.mark.parametrize("spec", SPECS, ids=["windows", "macos"])
    def test_root_is_found_by_searching_for_the_entry_point(self, spec):
        source = spec.read_text(encoding="utf-8")
        assert 'while not (ROOT / "launcher.py").exists()' in source, (
            f"{spec} counts parents instead of finding the repository root"
        )

    @pytest.mark.parametrize("spec", SPECS, ids=["windows", "macos"])
    def test_the_resolution_lands_on_the_repository_root(self, spec):
        """Simulate PyInstaller: SPECPATH is the spec's own directory."""
        root = spec.parent.resolve()
        while not (root / "launcher.py").exists() and root.parent != root:
            root = root.parent
        assert root == ROOT, f"{spec} resolves to {root}, expected {ROOT}"


class TestBuildDeadlines:
    """Every child process in the build must have a deadline.

    A packaged GUI that opens a dialog never exits, and an unbounded
    subprocess.run waits with it — which is how a one-hour "build" happens with
    no output at all.
    """

    SOURCE = (ROOT / "tools/build_release.py").read_text(encoding="utf-8")

    def test_no_unbounded_subprocess_calls(self):
        import re

        for match in re.finditer(r"subprocess\.run\((.*?)\)", self.SOURCE, re.S):
            assert "timeout=" in match.group(1), f"subprocess.run without a timeout:\n{match.group(1)}"

    def test_timeout_errors_are_reported_as_build_failures(self):
        assert "TimeoutExpired" in self.SOURCE
        assert "did not finish within" in self.SOURCE or "did not exit within" in self.SOURCE

    def test_a_hung_frozen_app_explains_itself(self):
        assert "QT_QPA_PLATFORM=offscreen" in self.SOURCE


class TestFreezeBudget:
    """The freeze needs a bigger budget than an ordinary command."""

    SOURCE = (ROOT / "tools/build_release.py").read_text(encoding="utf-8")

    def test_freeze_timeout_is_generous(self):
        import re

        match = re.search(r"FREEZE_TIMEOUT\s*=\s*([0-9.]+)", self.SOURCE)
        assert match, "FREEZE_TIMEOUT is not defined"
        assert float(match.group(1)) >= 1800

    def test_every_pyinstaller_call_uses_it(self):
        import re

        for match in re.finditer(r"run\(\s*(?:\[|probe_args)(.*?)\)", self.SOURCE, re.S):
            call = match.group(0)
            if "PyInstaller" in call or "probe_args" in call:
                assert "FREEZE_TIMEOUT" in call, f"PyInstaller call without the budget:\n{call[:160]}"


class TestBuildRecursion:
    """A build must never start a build.

    The build runs the suite, the suite exercises the builder, and on a host that
    can build that is an infinite recursion — which hung Windows and macOS for
    ninety minutes while Linux, where the builder refuses instantly, stayed green.
    """

    SCRIPT = ROOT / "tools/build_release.py"

    def _run(self, *args, env=None):
        import os
        import subprocess
        import sys

        return subprocess.run(
            [sys.executable, str(self.SCRIPT), *args],
            capture_output=True, text=True, cwd=ROOT,
            env=dict(os.environ, **(env or {})),
        )

    def test_a_nested_build_is_refused(self):
        import json

        result = self._run("--json", env={"USBDOCTOR_BUILD_IN_PROGRESS": "1"})
        assert result.returncode == 1
        assert "nested build" in json.loads(result.stdout)["error"]
        assert "refusing" in result.stderr.lower()

    def test_dry_run_never_builds_but_still_reports(self):
        import json

        document = json.loads(self._run("--json", "--dry-run").stdout)
        assert document["dry_run"] is True
        assert document["artifacts"] == []

    def test_no_test_starts_a_real_build(self):
        for path in (ROOT / "tests").rglob("test_*.py"):
            text = path.read_text(encoding="utf-8")
            if "build_release.py" in text:
                assert "--dry-run" in text, f"{path.name} invokes the builder without --dry-run"


class TestPhaseAnnotations:
    """A lost log must not lose the progress information."""

    SOURCE = (ROOT / "tools/build_release.py").read_text(encoding="utf-8")

    def test_log_publishes_an_annotation_under_github_actions(self):
        assert 'GITHUB_ACTIONS' in self.SOURCE
        assert '::warning title=build phase::' in self.SOURCE

    def test_the_actions_value_is_matched_case_insensitively(self):
        """GITHUB_ACTIONS is "true", not "1" — comparing to "1" silently disabled this."""
        import re

        match = re.search(r"os\.environ\.get\(\"GITHUB_ACTIONS\"[^)]*\)([^\n]*)", self.SOURCE)
        assert match, "the GITHUB_ACTIONS check is missing"
        assert "lower()" in match.group(1), "the check must accept 'true' as well as '1'"

    def test_annotations_are_flushed(self):
        import re

        match = re.search(r'print\(f"::warning title=build phase::\{message\}"(.*?)\)', self.SOURCE)
        assert match and "flush=True" in match.group(1)
