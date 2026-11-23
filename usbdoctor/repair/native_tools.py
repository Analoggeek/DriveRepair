"""Delegation to the operating system's own file-system checkers.

Rules enforced here:

* The command line is built from an allow-list per platform and always passed
  as an argv list (never a shell string).
* A read-only *check first* pass is always available; the repairing pass is only
  offered separately and requires its own consent.
* Output is parsed so the report can say precisely which repairs the OS made.
* Exit codes are interpreted per tool (fsck uses bit masks; CHKDSK uses its own).
"""

from __future__ import annotations

import os
import platform
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from usbdoctor.core.errors import ToolUnavailableError
from usbdoctor.core.model import DeviceRef
from usbdoctor.core.platform import detect_system
from usbdoctor.core.util import run_process, which
from usbdoctor.repair.base import RepairResult, RepairStatus, RunContext

# fsck exit codes are a bit mask (see fsck(8)).
FSCK_CODES = {
    0: "no errors",
    1: "file system errors corrected",
    2: "file system errors corrected, system should be rebooted",
    4: "file system errors left uncorrected",
    8: "operational error",
    16: "usage or syntax error",
    32: "check cancelled by user request",
    128: "shared library error",
}


@dataclass
class NativeCheckResult:
    tool: str
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    errors_found: int = 0
    errors_fixed: int = 0
    summary: str = ""
    needs_mount: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "argv": self.argv,
            "returncode": self.returncode,
            "errors_found": self.errors_found,
            "errors_fixed": self.errors_fixed,
            "summary": self.summary,
            "stdout_tail": self.stdout[-4000:],
            "stderr_tail": self.stderr[-2000:],
            "details": self.details,
        }


# ---------------------------------------------------------------------------
# Command construction (pure functions: unit-testable without a device)
# ---------------------------------------------------------------------------


def chkdsk_argv(volume: str, *, fix: bool, surface: bool = False) -> list[str]:
    """Build a read-only or repairing CHKDSK command for a volume letter.

    Read-only mode is ``chkdsk X:`` with no switches.  ``/f`` repairs file-system
    errors; ``/r`` adds a surface scan (implies ``/f``) and needs the volume
    dismounted (``/x``).
    """
    letter = volume.strip().rstrip("\\").rstrip(":").rstrip("\\")
    if not letter:
        raise ValueError("a drive letter is required for CHKDSK")
    args = ["chkdsk", f"{letter}:"]
    if surface:
        args += ["/r", "/x"]
    elif fix:
        args.append("/f")
    return args


def fsck_argv(path: str, filesystem: str, *, fix: bool) -> list[str]:
    """Build the correct fsck invocation for a Linux/macOS target.

    Switches follow each tool's documented behaviour:
      dosfsck -n (read-only) / -a (auto-repair)
      fsck.exfat -n / -p (preen = repair)
      ntfsfix     -n (check) / -d (clear dirty + repair)
      e2fsck      -n (read-only) / -p (preen) / -f -y for a forced full repair
    """
    fs = (filesystem or "").lower()
    if fs.startswith("fat"):
        tool = "fsck.fat" if which("fsck.fat") else "dosfsck"
        return [tool, "-a" if fix else "-n", path]
    if fs == "exfat":
        return ["fsck.exfat", "-p" if fix else "-n", path]
    if fs.startswith("ntfs"):
        return ["ntfsfix", "-d" if fix else "-n", path]
    if fs.startswith("ext"):
        return ["e2fsck", "-p" if fix else "-n", path]
    return ["fsck", "-n", path]


def diskutil_verify_argv(identifier: str) -> list[str]:
    return ["diskutil", "verifyVolume", identifier]


def diskutil_repair_argv(identifier: str) -> list[str]:
    return ["diskutil", "repairVolume", identifier]


def smartctl_selftest_argv(device: str, test: str = "short") -> list[str]:
    return ["smartctl", "-t", test, device]


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def parse_chkdsk_output(stdout: str, stderr: str) -> dict[str, Any]:
    text = stdout + "\n" + stderr
    info: dict[str, Any] = {"raw_errors": [], "index_errors": [], "bad_sectors": [], "stages": []}
    for line in text.splitlines():
        low = line.lower()
        if "bad sector" in low or "unreadable" in low or "read failure" in low:
            info["bad_sectors"].append(line.strip())
        if "correcting errors in the" in low or "index entries" in low:
            info["index_errors"].append(line.strip())
        if "has been repaired" in low or "corrected" in low or "recovered" in low:
            info["raw_errors"].append(line.strip())
        match = re.search(r"stage\s+(\d)\s+of\s+(\d)", low)
        if match:
            info["stages"].append(int(match.group(1)))
    if "windows has scanned the file system and found no problems" in text.lower():
        info["clean"] = True
    elif "windows has made corrections" in text.lower() or "windows has scanned" in text.lower():
        info["clean"] = False
    return info


def parse_fsck_output(stdout: str, stderr: str, returncode: int) -> dict[str, Any]:
    text = stdout + "\n" + stderr
    fixed = len(re.findall(r"(?i)\bfixed\b|\brepaired\b|correcting", text))
    found = len(re.findall(r"(?i)\berror\b|corrupt|invalid|truncat", text))
    return {
        "bits": [FSCK_CODES[b] for b in (1, 2, 4, 8, 16, 32, 128) if returncode & b],
        "messages_of_interest": [
            line.strip() for line in text.splitlines() if re.search(r"(?i)fixed|error|corrupt|invalid", line)
        ][:50],
        "fix_count": fixed,
        "error_count": found,
    }


def parse_diskutil_verify(stdout: str) -> dict[str, Any]:
    info: dict[str, Any] = {"ok": False, "messages": []}
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if "appears to be OK" in stripped or "volume appears to be OK" in stripped:
            info["ok"] = True
        if re.search(r"(?i)error|corrupt|invalid node|inconsistent", stripped):
            info["messages"].append(stripped)
    return info


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def _mounted_volumes(device: DeviceRef) -> list[str]:
    volumes = list(device.mountpoints)
    for part in device.partitions:
        if part.mounted_at and part.mounted_at not in volumes:
            volumes.append(part.mounted_at)
    return volumes


def run_native_check(ctx: RunContext, *, filesystem: str, repair: bool) -> RepairResult:
    """Run the platform's file-system checker in check-only or repair mode."""
    system = detect_system()
    if system == "windows":
        return _run_chkdsk(ctx, filesystem=filesystem, repair=repair)
    if system == "macos":
        return _run_diskutil(ctx, repair=repair)
    return _run_fsck(ctx, filesystem=filesystem, repair=repair)


def _run_chkdsk(ctx: RunContext, *, filesystem: str, repair: bool) -> RepairResult:
    volumes = _mounted_volumes(ctx.device)
    letter = None
    for vol in volumes:
        match = re.match(r"([A-Za-z]):", vol)
        if match:
            letter = match.group(1)
            break
    if letter is None and ctx.device.path.upper().startswith("\\\\.\\PHYSICALDRIVE"):
        return RepairResult(
            status=RepairStatus.NOT_APPLICABLE,
            message="CHKDSK needs a mounted volume letter. Assign a drive letter (or let Windows mount "
            "the volume) and retry.",
        )
    if letter is None:
        return RepairResult.failure("No volume letter is associated with this device.")
    if ctx.device.is_system_disk:
        return RepairResult.failure("Refusing to run CHKDSK against the system volume.")
    argv = chkdsk_argv(letter, fix=repair, surface=ctx.options.get("surface_scan", False))
    ctx.log("Running: " + " ".join(argv))
    try:
        proc = run_process(argv, timeout=ctx.options.get("timeout", 7200))
    except Exception as exc:
        return RepairResult(
            status=RepairStatus.REQUIRED_TOOL_MISSING, message=f"CHKDSK could not be started: {exc}"
        )
    parsed = parse_chkdsk_output(proc.stdout, proc.stderr)
    errors_fixed = len(parsed.get("raw_errors", []))
    returncode = proc.returncode
    # CHKDSK exit codes: 0 clean, 1 repaired, 2 cleanup/flush, 3 could not check.
    verified: Optional[bool] = None
    if returncode == 0:
        verified = True
        status = RepairStatus.SUCCEEDED
        message = "CHKDSK found no file-system problems."
    elif returncode == 1:
        verified = True
        status = RepairStatus.SUCCEEDED
        message = "CHKDSK found and repaired file-system errors."
    elif returncode == 3:
        status = RepairStatus.FAILED
        message = "CHKDSK could not check the volume (it may be locked). Reboot or dismount and retry."
    else:
        status = RepairStatus.PARTIAL if errors_fixed else RepairStatus.FAILED
        message = f"CHKDSK finished with exit code {returncode}."
    return RepairResult(
        status=status,
        message=message,
        verified=verified,
        details=parsed | {"argv": argv, "returncode": returncode},
    )


def _run_fsck(ctx: RunContext, *, filesystem: str, repair: bool) -> RepairResult:
    if ctx.device.mountpoints:
        return RepairResult.failure(
            f"The volume is mounted at {', '.join(ctx.device.mountpoints)}. Unmount it before running fsck."
        )
    target = None
    if ctx.device.kind == "partition":
        target = ctx.device.path
    else:
        for part in ctx.device.partitions:
            if part.path:
                target = part.path
                if part.filesystem and (filesystem or "").lower().startswith(part.filesystem.lower()[:4]):
                    break
    if not target:
        return RepairResult.failure(
            "No partition path was found to check. Run the check on a partition, not a whole disk."
        )
    argv = fsck_argv(target, filesystem, fix=repair)
    if which(argv[0]) is None:
        return RepairResult(
            status=RepairStatus.REQUIRED_TOOL_MISSING,
            message=f"{argv[0]} is not installed. Install the matching fsck package "
            f"(dosfstools / exfatprogs / ntfs-3g / e2fsprogs) and retry.",
        )
    ctx.log("Running: " + " ".join(argv))
    try:
        proc = run_process(argv, timeout=ctx.options.get("timeout", 3600))
    except Exception as exc:
        return RepairResult(status=RepairStatus.REQUIRED_TOOL_MISSING, message=f"fsck failed to start: {exc}")
    parsed = parse_fsck_output(proc.stdout, proc.stderr, proc.returncode)
    rc = proc.returncode
    if rc == 0:
        return RepairResult.success(
            "File-system check completed with no errors.", details=parsed | {"argv": argv}
        )
    if rc & 0x04:
        return RepairResult(
            status=RepairStatus.FAILED,
            message="File-system errors remain uncorrected (fsck exit code 4).",
            verified=False,
            details=parsed | {"argv": argv},
        )
    if rc & (0x02 | 0x01):
        return RepairResult.success(
            "File-system errors were corrected.", details=parsed | {"argv": argv, "reboot": bool(rc & 0x02)}
        )
    return RepairResult(
        status=RepairStatus.FAILED,
        message=f"fsck exited with {rc}: " + ", ".join(parsed["bits"] or ["unknown failure"]),
        verified=False,
        details=parsed | {"argv": argv},
    )


def _run_diskutil(ctx: RunContext, *, repair: bool) -> RepairResult:
    identifier = os.path.basename(ctx.device.path)
    if not re.match(r"disk\d+(s\d+)?$", identifier):
        return RepairResult.failure(f"{identifier} is not a diskutil identifier.")
    if ctx.device.is_system_disk:
        return RepairResult.failure("Refusing to repair the macOS system volume.")
    argv = diskutil_repair_argv(identifier) if repair else diskutil_verify_argv(identifier)
    ctx.log("Running: " + " ".join(argv))
    try:
        proc = run_process(argv, timeout=ctx.options.get("timeout", 7200))
    except Exception as exc:
        return RepairResult(status=RepairStatus.REQUIRED_TOOL_MISSING, message=f"diskutil failed: {exc}")
    parsed = parse_diskutil_verify(proc.stdout)
    if proc.ok and parsed["ok"] and not parsed["messages"]:
        return RepairResult.success("diskutil reports the volume is OK.", details=parsed | {"argv": argv})
    if proc.ok and repair:
        return RepairResult(
            status=RepairStatus.SUCCEEDED if not parsed["messages"] else RepairStatus.PARTIAL,
            message="diskutil repair completed. Review the messages for any remaining issues.",
            details=parsed | {"argv": argv},
        )
    return RepairResult(
        status=RepairStatus.FAILED,
        message="diskutil reported problems: " + "; ".join(parsed["messages"][:3] or ["unknown failure"]),
        verified=False,
        details=parsed | {"argv": argv},
    )


def start_smart_selftest(ctx: RunContext, *, test: str = "short") -> RepairResult:
    exe = which("smartctl")
    if not exe:
        raise ToolUnavailableError(
            "smartmontools is not installed.",
            hint="Install smartmontools to run device self-tests.",
        )
    argv = [exe, "-t", test, ctx.device.path]
    proc = run_process(argv, timeout=120)
    text = proc.stdout + proc.stderr
    if "Self-test has begun" in text or "Please wait" in text or "Testing has begun" in text:
        return RepairResult.success(
            f"The device started its {test} self-test. Re-run the SMART check in a few minutes to read the result.",
            details={"argv": argv, "output": text[-800:]},
        )
    return RepairResult.failure(
        "The device refused to start a self-test (many USB flash drives do not support it).",
        details={"argv": argv, "output": text[-800:]},
    )
