#!/usr/bin/env python3
"""Build the USB Doctor AI desktop package and a fully offline installer.

Run this **on the target operating system**: PyInstaller does not cross-compile,
so a Windows installer can only be produced on Windows and a macOS disk image
only on macOS. Everything below uses the standard library plus the build extras
(PyInstaller, PySide6, Pillow) and makes no network request at build time.

    python tools/build_release.py                 # build + verify + package
    python tools/build_release.py --skip-tests    # iterate faster (not for release)
    python tools/build_release.py --json          # machine-readable summary

Outputs
    Windows  dist/USBDoctorAI/                      portable folder
             dist/USBDoctorAI-Setup-<version>.exe   offline installer (Inno Setup)
             dist/USBDoctorAI-<version>-portable.zip offline, no installation needed
    macOS    dist/USBDoctorAI.app                   application bundle
             dist/USBDoctorAI-<version>.dmg         offline disk image
             dist/USBDoctorAI-<version>-portable.zip offline zip of the .app

Exit code is non-zero if any step fails; a release build never "succeeds with
warnings".
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"


class BuildError(RuntimeError):
    """A release step failed; the artifact must not be published."""


# When --json is requested, stdout must contain the JSON document and nothing
# else, so progress goes to stderr. Otherwise a caller doing
# `build_release.py --json > report.json` would get a file it cannot parse.
_json_mode = False


def _echo(message: str) -> None:
    print(message, file=sys.stderr if _json_mode else sys.stdout)


def log(message: str) -> None:
    _echo(f"\n\033[1;36m==> {message}\033[0m" if sys.stdout.isatty() else f"\n==> {message}")


# Set for every child process the build starts. A nested build refuses to run:
# the build runs the test suite, and the suite exercises this tool, so without a
# marker one build can spawn another, which spawns another, forever.
IN_PROGRESS_ENV = "USBDOCTOR_BUILD_IN_PROGRESS"


def run(
    args: list[str],
    *,
    check: bool = True,
    capture: bool = False,
    timeout: float = 900,
    env: dict | None = None,
) -> subprocess.CompletedProcess:
    printable = " ".join(args)
    _echo(f"    $ {printable}")
    try:
        result = subprocess.run(
            args,
            cwd=ROOT,
            text=True,
            capture_output=capture,
            check=False,
            timeout=timeout,
            env={**os.environ, **(env or {}), IN_PROGRESS_ENV: "1"},
        )
    except subprocess.TimeoutExpired as exc:
        # Without this, a wedged child made the whole build hang silently until
        # the CI job's own timeout killed the runner.
        raise BuildError(
            f"command did not finish within {timeout:.0f}s: {printable}"
        ) from exc
    if check and result.returncode != 0:
        detail = (result.stdout or "") + (result.stderr or "")
        raise BuildError(f"command failed ({result.returncode}): {printable}\n{detail[-4000:]}")
    return result


def version() -> str:
    sys.path.insert(0, str(ROOT))
    from usbdoctor.version import __version__

    return __version__


# ---------------------------------------------------------------------------
# verification
# ---------------------------------------------------------------------------


def _run_frozen(argv: list[str], *, env: dict, label: str, timeout: float = 300):
    """Run the frozen product with a hard deadline.

    A packaged GUI application is the one thing in this pipeline that can wedge
    without printing anything: a modal dialog waits forever with no one to
    dismiss it, and a captured subprocess call then waits with it. A deadline
    turns that into a clear failure that says what to look at.
    """
    try:
        return subprocess.run(argv, env=env, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise BuildError(
            f"{label} did not exit within {timeout:.0f}s while running `{' '.join(argv[1:])}`. "
            "A packaged GUI that opens a dialog or waits for an event loop never exits on a "
            "headless machine; check that QT_QPA_PLATFORM=offscreen reaches it and that the "
            "self-check path does not create a QApplication that is expected to run."
        ) from exc


def verify_install(executable: Path, *, label: str, windowed: bool) -> dict:
    """Run the packaged self-check inside the frozen product.

    ``windowed`` builds have no stdout on Windows, so for those only the exit
    code is trustworthy.
    """
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"  # build machines have no display
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)

    if windowed:
        print(f"    checking {label} (exit code only)")
        result = _run_frozen([str(executable), "selfcheck"], env=env, label=label)
        if result.returncode != 0:
            raise BuildError(
                f"{label} failed its self-check (exit {result.returncode}).\n"
                f"{result.stdout[-2000:]}{result.stderr[-2000:]}"
            )
        return {"ok": True, "checks": []}

    print(f"    checking {label}")
    result = _run_frozen([str(executable), "selfcheck", "--json"], env=env, label=label)
    payload: dict | None = None
    try:
        payload = json.loads(result.stdout[result.stdout.index("{"):])
    except (ValueError, json.JSONDecodeError):
        pass
    if result.returncode != 0 or not payload or not payload.get("ok"):
        raise BuildError(
            f"{label} failed its self-check (exit {result.returncode}).\n"
            f"{result.stdout[-3000:]}{result.stderr[-3000:]}"
        )
    for check in payload["checks"]:
        flag = "PASS" if check["passed"] else "FAIL"
        print(f"      {flag}  {check['name']:22} {check['detail']}")
    return payload


# ---------------------------------------------------------------------------
# packaging helpers
# ---------------------------------------------------------------------------


def zip_directory(source: Path, target: Path) -> Path:
    """Deterministic-ish zip of a directory (portable, fully offline artifact)."""
    if target.exists():
        target.unlink()
    count = 0
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(source.parent))
                count += 1
    print(f"    {target.name}: {count} files, {target.stat().st_size / 1024 / 1024:.1f} MiB")
    return target


def human_size(path: Path) -> str:
    total = sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
    return f"{total / 1024 / 1024:.1f} MiB ({sum(1 for p in path.rglob('*') if p.is_file())} files)"


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------


def find_inno_setup() -> Path | None:
    candidates = [
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Inno Setup 6" / "ISCC.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Inno Setup 6" / "ISCC.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    found = shutil.which("iscc") or shutil.which("ISCC")
    return Path(found) if found else None


def build_windows(args) -> dict:
    python = sys.executable
    out: dict = {"platform": "windows", "artifacts": []}

    log("Running the test suite")
    if not args.skip_tests:
        run([python, "-m", "pytest", "tests", "-q"], env={IN_PROGRESS_ENV: "1"})

    log("Generating icons")
    run([python, "tools/make_icons.py"], check=False)  # Qt may be missing a display

    log("Freezing the application")
    for stale in ("build", "dist"):
        if (ROOT / stale).exists():
            shutil.rmtree(ROOT / stale)
    run([python, "-m", "PyInstaller", "installers/USBDoctorAI.spec", "--noconfirm", "--clean"])

    bundle = DIST / "USBDoctorAI"
    exe = bundle / "USBDoctorAI.exe"
    if not exe.exists():
        raise BuildError(f"PyInstaller did not produce {exe}")
    out["portable_dir"] = str(bundle)
    out["portable_size"] = human_size(bundle)

    # A console-enabled twin, because the shipped build is silent by design.
    log("Building the console probe (for automated verification)")
    probe_dir = DIST / "probe"
    probe_args = [
        python, "-m", "PyInstaller", "launcher.py",
        "--name", "USBDoctorAIConsole", "--console", "--onefile", "--noconfirm", "--clean",
        "--distpath", str(probe_dir), "--workpath", str(ROOT / "build-probe"),
        "--paths", ".", "--exclude-module", "tkinter",
    ]
    for source, dest in (
        ("usbdoctor/frontend/web/index.html", "usbdoctor/frontend/web"),
        ("usbdoctor/frontend/web/app.css", "usbdoctor/frontend/web"),
        ("usbdoctor/frontend/web/app.js", "usbdoctor/frontend/web"),
        ("usbdoctor/frontend/web/assets", "usbdoctor/frontend/web/assets"),
    ):
        probe_args += ["--add-data", f"{source};{dest}"]
    run(probe_args)
    probe = probe_dir / "USBDoctorAIConsole.exe"
    if not probe.exists():
        raise BuildError("the console probe was not produced")

    log("Verifying the frozen product")
    out["probe_checks"] = verify_install(probe, label="frozen console build", windowed=False)["checks"]
    verify_install(exe, label="frozen shipped build", windowed=True)
    shutil.rmtree(probe_dir, ignore_errors=True)

    log("Building the offline installer")
    iscc = find_inno_setup()
    if iscc is None:
        raise BuildError(
            "Inno Setup 6 was not found. Install it (https://jrsoftware.org/isdl.php) "
            "or pass --portable-only to produce just the zip."
        )
    run([str(iscc), str(ROOT / "installers/windows/usbdoctor.iss")])
    installer = DIST / f"USBDoctorAI-Setup-{version()}.exe"
    if not installer.exists():
        candidates = sorted(DIST.glob("*Setup*.exe"))
        if not candidates:
            raise BuildError("Inno Setup reported success but produced no installer")
        installer = candidates[-1]
    out["installer"] = str(installer)
    out["installer_size"] = f"{installer.stat().st_size / 1024 / 1024:.1f} MiB"
    out["artifacts"].append(str(installer))

    log("Building the portable archive")
    archive = DIST / f"USBDoctorAI-{version()}-x64-portable.zip"
    zip_directory(bundle, archive)
    out["artifacts"].append(str(archive))

    return out


# ---------------------------------------------------------------------------
# macOS
# ---------------------------------------------------------------------------


def build_macos(args) -> dict:
    python = sys.executable
    arch = platform.machine()  # arm64 or x86_64
    arch_tag = {"arm64": "arm64", "x86_64": "x64"}.get(arch, arch)
    out: dict = {"platform": "macos", "artifacts": [], "architecture": arch}

    log("Running the test suite")
    if not args.skip_tests:
        run([python, "-m", "pytest", "tests", "-q"], env={IN_PROGRESS_ENV: "1"})

    log("Generating icons")
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    try:
        subprocess.run(
            [python, "tools/make_icons.py"], cwd=ROOT, env=env, check=False, timeout=180
        )
    except subprocess.TimeoutExpired:
        raise BuildError("icon generation did not finish within 180s") from None

    log("Freezing the application bundle")
    for stale in ("build", "dist"):
        if (ROOT / stale).exists():
            shutil.rmtree(ROOT / stale)
    run([python, "-m", "PyInstaller", "installers/macos/USBDoctorAI.spec", "--noconfirm", "--clean"])

    app = DIST / "USBDoctorAI.app"
    binary = app / "Contents" / "MacOS" / "USBDoctorAI"
    if not binary.exists():
        raise BuildError(f"PyInstaller did not produce {binary}")

    archs = run(["lipo", "-archs", str(binary)], capture=True, check=False).stdout.strip()
    out["bundle_archs"] = archs
    print(f"    bundle architectures: {archs}")

    log("Verifying the frozen product")
    out["probe_checks"] = verify_install(binary, label="frozen .app", windowed=False)["checks"]

    log("Building the disk image")
    dmg = DIST / f"USBDoctorAI-{version()}-{arch_tag}.dmg"
    if dmg.exists():
        dmg.unlink()
    if shutil.which("create-dmg"):
        run([
            "create-dmg", "--volname", "USB Doctor AI",
            "--volicon", "usbdoctor/frontend/web/assets/app.icns",
            "--window-pos", "200", "120", "--window-size", "640", "400",
            "--icon-size", "96", "--icon", "USBDoctorAI.app", "160", "180",
            "--app-drop-link", "470", "180", "--hide-extension", "USBDoctorAI.app",
            str(dmg), str(app),
        ], check=False)
    if not dmg.exists():
        run(["hdiutil", "create", "-volname", "USB Doctor AI", "-srcfolder", str(app),
             "-ov", "-format", "UDZO", str(dmg)])
    if not dmg.exists():
        raise BuildError("the disk image was not produced")
    out["dmg"] = str(dmg)
    out["dmg_size"] = f"{dmg.stat().st_size / 1024 / 1024:.1f} MiB"
    out["artifacts"].append(str(dmg))

    log("Building the portable archive")
    archive = DIST / f"USBDoctorAI-{version()}-{arch_tag}-portable.zip"
    zip_directory(app, archive)
    out["artifacts"].append(str(archive))

    return out


# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--skip-tests", action="store_true", help="do not run the test suite first")
    parser.add_argument("--portable-only", action="store_true",
                        help="skip the OS installer and produce only the portable zip")
    parser.add_argument("--json", action="store_true", help="print a machine-readable summary")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="check the host and report what would be built, without building anything",
    )
    args = parser.parse_args(argv)

    global _json_mode
    _json_mode = bool(args.json)

    started = time.time()
    system = platform.system()

    # Refuse to nest. The build runs the test suite; the suite exercises this
    # tool; without this, `build_release.py` on a build-capable host calls
    # itself, then itself again, until the runner dies or the job times out.
    if os.environ.get(IN_PROGRESS_ENV) == "1" and not args.dry_run:
        print(
            f"\n\033[1;31mBUILD FAILED\033[0m\nrefusing to start a build inside a build "
            f"({IN_PROGRESS_ENV}=1). Use --dry-run from tests.",
            file=sys.stderr,
        )
        if _json_mode:
            print(json.dumps({"status": "failed", "host": system, "artifacts": [],
                              "error": "nested build refused"}, indent=2))
        return 1
    # A base report exists before any build step runs, so a failure can still be
    # reported as valid JSON instead of a NameError on an unbound variable.
    report: dict = {"status": "failed", "host": system, "artifacts": []}
    if args.dry_run:
        supported = system in ("Windows", "Darwin")
        report.update(
            status="ok" if supported else "failed",
            version=version(),
            dry_run=True,
            platform=system.lower(),
            portable_only=bool(args.portable_only),
            error=None if supported else f"{system} is not a supported build host",
        )
        if not supported:
            # Same guidance as a real build, so `--dry-run` is a safe way to ask
            # "can this machine build the installers?" from anywhere.
            print(
                f"\n\033[1;31mBUILD FAILED\033[0m\n{system} is not a supported build host. "
                "PyInstaller does not cross-compile: run this on Windows or macOS, or use the "
                "release workflow on GitHub Actions (.github/workflows/release.yml).",
                file=sys.stderr,
            )
        if _json_mode:
            print(json.dumps(report, indent=2))
        else:
            print(f"host    : {system} ({'supported' if supported else 'cannot build'})")
            print(f"version : {report['version']}")
            print("dry run : nothing was built")
        return 0 if supported else 1
    try:
        if system == "Windows":
            if args.portable_only:
                log("Building the portable package only")
                report = _portable_only_windows(args)
            else:
                report = build_windows(args)
        elif system == "Darwin":
            report = build_macos(args)
        else:
            raise BuildError(
                f"{system} is not a supported build host. PyInstaller does not "
                "cross-compile: run this on Windows or macOS, or use the release "
                "workflow on GitHub Actions (.github/workflows/release.yml)."
            )
    except BuildError as exc:
        message = str(exc)
        print(f"\n\033[1;31mBUILD FAILED\033[0m\n{message}", file=sys.stderr)
        if _json_mode:
            # Keep the machine-readable contract even on failure: a report file
            # that is always valid JSON can be read by the caller without
            # knowing whether the build worked.
            report = {
                "status": "failed",
                "host": system,
                "version": version(),
                "error": message.splitlines()[0],
                "artifacts": [],
            }
            print(json.dumps(report, indent=2))
        return 1

    report["version"] = version()
    report["duration_s"] = round(time.time() - started, 1)
    report["status"] = "ok"

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        log("Done")
        print(f"    version : {report['version']}")
        for artifact in report.get("artifacts", []):
            print(f"    artifact: {artifact}")
        print("    Every artifact is fully offline: no download is required to install or run.")

    return 0


def _portable_only_windows(args) -> dict:
    """Windows portable zip without Inno Setup (useful on minimal machines)."""
    python = sys.executable
    if not args.skip_tests:
        log("Running the test suite")
        run([python, "-m", "pytest", "tests", "-q"], env={IN_PROGRESS_ENV: "1"})
    log("Freezing the application")
    run([python, "-m", "PyInstaller", "installers/USBDoctorAI.spec", "--noconfirm", "--clean"])
    bundle = DIST / "USBDoctorAI"
    if not (bundle / "USBDoctorAI.exe").exists():
        raise BuildError("PyInstaller did not produce the portable folder")
    log("Verifying the frozen product")
    verify_install(bundle / "USBDoctorAI.exe", label="frozen build", windowed=True)
    archive = DIST / f"USBDoctorAI-{version()}-x64-portable.zip"
    log("Building the portable archive")
    zip_directory(bundle, archive)
    return {"platform": "windows", "artifacts": [str(archive)]}


if __name__ == "__main__":
    raise SystemExit(main())
