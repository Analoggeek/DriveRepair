"""SMART (Self-Monitoring, Analysis and Reporting Technology) support.

Never invents values.  Sources, in priority order:

1. ``smartctl`` (smartmontools) with ``-j`` JSON output — richest data, and the
   only source of NAND/bridge health for many USB enclosures.
2. Linux ``/sys/class/block/<dev>/device`` — exposes NVMe/SCSI attributes.
3. macOS ``diskutil info`` / ``system_profiler SPNVMeDataType``.
4. Windows ``Get-StorageReliabilityCounter`` via PowerShell.

If none is available the result is explicitly ``available=False`` with the
reason, and the health engine reports UNKNOWN instead of guessing.
"""

from __future__ import annotations

import json
import os
import re
import struct
from dataclasses import dataclass, field
from typing import Any, Optional

from usbdoctor.core.platform import detect_system
from usbdoctor.core.util import run_process, tool_available, which

# ---------------------------------------------------------------------------
# ATA attribute semantics used by the health rule set
# ---------------------------------------------------------------------------

#: Attributes where a *raw* value above the threshold indicates trouble.
ATA_CRITICAL_ATTRS: dict[int, dict[str, Any]] = {
    5: {"name": "Reallocated_Sector_Ct", "warn": 1, "fail": 10, "meaning": "bad sectors remapped"},
    9: {"name": "Power_On_Hours", "warn": None, "fail": None, "meaning": "total powered-on hours"},
    187: {"name": "Reported_Uncorrect", "warn": 1, "fail": 5, "meaning": "uncorrectable read errors"},
    188: {"name": "Command_Timeout", "warn": 1, "fail": 20, "meaning": "commands timed out"},
    196: {"name": "Reallocation_Event_Count", "warn": 1, "fail": 10, "meaning": "remap events"},
    197: {"name": "Current_Pending_Sector", "warn": 1, "fail": 5, "meaning": "sectors waiting for remap"},
    198: {"name": "Offline_Uncorrectable", "warn": 1, "fail": 5, "meaning": "uncorrectable offline scan errors"},
    199: {"name": "UDMA_CRC_Error_Count", "warn": 1, "fail": 50, "meaning": "cable/connection errors"},
    231: {"name": "SSD_Life_Left", "warn": None, "fail": None, "meaning": "SSD wear indicator"},
    233: {"name": "Media_Wearout_Indicator", "warn": None, "fail": None, "meaning": "NAND wear indicator"},
}

NVME_CRITICAL_WARNING = {
    0x01: "Available spare capacity below threshold",
    0x02: "Temperature above threshold",
    0x04: "NVM subsystem reliability degraded",
    0x08: "Media placed in read-only mode",
    0x10: "Volatile memory backup device failed",
    0x20: "Persistent memory region read-only",
    0x40: "Persistent memory region reliability degraded",
}


@dataclass
class SmartAttribute:
    id: int
    name: str
    value: int
    worst: int
    threshold: int
    raw: int
    raw_text: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "value": self.value,
            "worst": self.worst,
            "threshold": self.threshold,
            "raw": self.raw,
            "raw_text": self.raw_text,
        }


@dataclass
class SmartResult:
    available: bool = False
    source: str = ""
    reason: str = ""
    protocol: str = ""  # ata | nvme | scsi | usb-bridge | none
    device_model: str = ""
    firmware: str = ""
    serial: str = ""
    health_passed: Optional[bool] = None
    health_text: str = ""
    temperature_c: Optional[float] = None
    power_on_hours: Optional[int] = None
    power_cycle_count: Optional[int] = None
    attributes: list[SmartAttribute] = field(default_factory=list)
    nvme_percent_used: Optional[int] = None
    nvme_available_spare: Optional[int] = None
    nvme_critical_warning: Optional[int] = None
    nvme_data_units_written: Optional[int] = None
    error_log_entries: int = 0
    self_test_status: str = ""
    smart_capable: Optional[bool] = None
    warnings: list[str] = field(default_factory=list)
    raw_json: Optional[dict[str, Any]] = None

    # --------------------------------------------------------------- helpers
    def attribute(self, attr_id: int) -> Optional[SmartAttribute]:
        for attr in self.attributes:
            if attr.id == attr_id:
                return attr
        return None

    @property
    def reallocated_sectors(self) -> Optional[int]:
        attr = self.attribute(5)
        return attr.raw if attr else None

    @property
    def pending_sectors(self) -> Optional[int]:
        attr = self.attribute(197)
        return attr.raw if attr else None

    @property
    def uncorrectable_sectors(self) -> Optional[int]:
        attr = self.attribute(198) or self.attribute(187)
        return attr.raw if attr else None

    @property
    def crc_errors(self) -> Optional[int]:
        attr = self.attribute(199)
        return attr.raw if attr else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "source": self.source,
            "reason": self.reason,
            "protocol": self.protocol,
            "device_model": self.device_model,
            "firmware": self.firmware,
            "serial": self.serial,
            "health_passed": self.health_passed,
            "health_text": self.health_text,
            "temperature_c": self.temperature_c,
            "power_on_hours": self.power_on_hours,
            "power_cycle_count": self.power_cycle_count,
            "attributes": [a.to_dict() for a in self.attributes],
            "nvme_percent_used": self.nvme_percent_used,
            "nvme_available_spare": self.nvme_available_spare,
            "nvme_critical_warning": self.nvme_critical_warning,
            "nvme_data_units_written": self.nvme_data_units_written,
            "error_log_entries": self.error_log_entries,
            "self_test_status": self.self_test_status,
            "smart_capable": self.smart_capable,
            "warnings": self.warnings,
        }


# ---------------------------------------------------------------------------
# smartctl (shared by every platform; smartmontools ships builds for all)
# ---------------------------------------------------------------------------


def _smartctl_argv(device_path: str, extra: list[str]) -> Optional[list[str]]:
    exe = which("smartctl") or which("smartctl.exe")
    if not exe:
        return None
    system = detect_system()
    argv = [exe]
    if system == "windows":
        # smartctl wants the physical drive number or a device path.
        m = re.search(r"PhysicalDrive(\d+)", device_path, re.I)
        if m:
            argv += ["-d", "auto", m.group(1)]
        else:
            argv.append(device_path)
    elif system == "macos":
        if device_path.startswith("/dev/disk"):
            argv.append(device_path)
        elif device_path.startswith("/dev/rdisk"):
            argv.append(device_path)
        else:
            argv.append(device_path)
    else:
        argv.append(device_path)
    argv += extra
    return argv


def read_smart_smartctl(device_path: str, *, deep_usb: bool = True) -> SmartResult:
    """Query smartctl for a device and normalise the JSON into SmartResult."""
    result = SmartResult(source="smartctl", protocol="unknown")
    exe = which("smartctl")
    if not exe:
        result.reason = (
            "smartmontools is not installed. Install it to read SMART data "
            "(Windows: official smartmontools build; macOS: brew install smartmontools; "
            "Linux: apt install smartmontools)."
        )
        return result

    extra = ["-a", "-j", "--nocheck=standby"]
    argv = _smartctl_argv(device_path, extra)
    if argv is None:  # pragma: no cover - guarded above
        return result
    try:
        proc = run_process(argv, timeout=30)
    except Exception as exc:  # ToolExecutionError or OS error
        result.reason = f"smartctl could not be executed: {exc}"
        return result
    payload: Optional[dict[str, Any]] = None
    text = proc.stdout.strip()
    start = text.find("{")
    if start >= 0:
        try:
            payload = json.loads(text[start:])
        except json.JSONDecodeError:
            payload = None
    if payload is None:
        if proc.stderr:
            result.reason = f"smartctl returned no JSON ({proc.stderr.splitlines()[0][:200]})"
        else:
            result.reason = "smartctl returned no parsable output"
        return result

    result.raw_json = payload
    _apply_smartctl_json(result, payload)

    # USB flash drives commonly need "-d sat" (USB-to-SATA bridge) or the
    # device reports "SMART support is: Unavailable".  Try the documented
    # bridge types before giving up — but never more than a few, and never
    # against a device that is already erroring.
    if deep_usb and not result.available:
        for dtype in ("sat", "usbjmicron", "usbsunplus", "usbcypress"):
            argv2 = _smartctl_argv(device_path, ["-a", "-j", "-d", dtype])
            if argv2 is None:
                break
            try:
                proc2 = run_process(argv2, timeout=25)
            except Exception:
                continue
            s2 = proc2.stdout[proc2.stdout.find("{") :] if "{" in proc2.stdout else ""
            if not s2:
                continue
            try:
                payload2 = json.loads(s2)
            except json.JSONDecodeError:
                continue
            probe = SmartResult()
            _apply_smartctl_json(probe, payload2)
            if probe.available:
                result = probe
                result.raw_json = payload2
                result.warnings.append(f"SMART read through USB bridge type '{dtype}'.")
                return result
    return result


def _apply_smartctl_json(result: SmartResult, payload: dict[str, Any]) -> None:
    result.available = False
    device = payload.get("device", {})
    result.protocol = (device.get("protocol") or "").lower()
    model_name = payload.get("model_name") or payload.get("scsi_model_name") or ""
    result.device_model = model_name.strip()
    result.firmware = (payload.get("firmware_version") or "").strip()
    result.serial = (payload.get("serial_number") or "").strip()

    smart_support = (payload.get("smart_support") or {}).get("available")
    messages = payload.get("smartctl", {}).get("messages") or []
    result.smart_capable = smart_support if smart_support is not None else None

    if payload.get("smart_status") is not None:
        passed = payload["smart_status"].get("passed")
        result.health_passed = bool(passed) if passed is not None else None
        result.health_text = "PASSED" if passed else "FAILED"
    temperature = (payload.get("temperature") or {}).get("current")
    if temperature is not None:
        result.temperature_c = float(temperature)
    poh = (payload.get("power_on_time") or {}).get("hours")
    if poh is not None:
        result.power_on_hours = int(poh)
    cycles = (payload.get("power_cycle_count"))
    if cycles is not None:
        result.power_cycle_count = int(cycles)
    ata_log = payload.get("ata_smart_error_log", {})
    if isinstance(ata_log, dict):
        summary = ata_log.get("summary") or {}
        count = summary.get("count")
        if count is not None:
            result.error_log_entries = int(count)

    # ATA attributes
    for table in payload.get("ata_smart_attributes", {}).get("table", []) or []:
        raw_block = table.get("raw", {})
        raw_value = raw_block.get("value")
        raw_text = raw_block.get("string", "") or ""
        if raw_value is None and raw_text:
            num = re.match(r"\s*(\d+)", raw_text)
            raw_value = int(num.group(1)) if num else 0
        result.attributes.append(
            SmartAttribute(
                id=int(table.get("id", 0)),
                name=str(table.get("name", "")),
                value=int(table.get("value", 0) or 0),
                worst=int(table.get("worst", 0) or 0),
                threshold=int(table.get("thresh", 0) or 0),
                raw=int(raw_value or 0),
                raw_text=raw_text,
            )
        )

    # NVMe
    nvme = payload.get("nvme_smart_health_information_log") or {}
    if nvme:
        result.protocol = result.protocol or "nvme"
        result.nvme_percent_used = nvme.get("percentage_used")
        result.nvme_available_spare = nvme.get("available_spare")
        result.nvme_critical_warning = nvme.get("critical_warning")
        units = nvme.get("data_units_written")
        result.nvme_data_units_written = units
        if result.temperature_c is None and nvme.get("temperature") is not None:
            result.temperature_c = float(nvme["temperature"])
        if result.power_on_hours is None and nvme.get("power_on_hours") is not None:
            result.power_on_hours = int(nvme["power_on_hours"])
        if nvme.get("media_errors") is not None:
            result.attributes.append(
                SmartAttribute(
                    id=187,
                    name="NVMe_Media_Errors",
                    value=100,
                    worst=100,
                    threshold=0,
                    raw=int(nvme["media_errors"]),
                    raw_text=str(nvme["media_errors"]),
                )
            )
        if nvme.get("critical_warning"):
            bits = int(nvme["critical_warning"])
            for bit, text in NVME_CRITICAL_WARNING.items():
                if bits & bit:
                    result.warnings.append(f"NVMe critical warning: {text}")

    # SCSI / USB bridge "self test"
    if payload.get("self_test", {}).get("status"):
        result.self_test_status = str(payload["self_test"]["status"].get("string", ""))

    if not result.available:
        power_mode = (payload.get("power_mode") or "").upper()
        if smart_support is False or (power_mode == "ACTIVE" and not result.attributes and not nvme):
            result.reason = (
                "This device does not expose SMART data. Most USB flash drives "
                "and SD cards sit behind a bridge chip that hides SMART; this is "
                "normal and not a fault."
            )
        elif result.attributes or nvme:
            result.available = True
            result.reason = ""
        else:
            result.reason = "; ".join(messages[:2]) if messages else "SMART data not reported by the device."
        return
    result.available = True


# ---------------------------------------------------------------------------
# Linux sysfs
# ---------------------------------------------------------------------------


def read_smart_linux_sysfs(device_path: str) -> SmartResult:
    result = SmartResult(source="linux-sysfs")
    name = os.path.basename(os.path.realpath(device_path))
    base = f"/sys/class/block/{name}/device"
    if not os.path.isdir(base):
        result.reason = f"No sysfs device directory for {name} (loop/image or passed-through device)."
        return result
    values: dict[str, str] = {}
    for attr in (
        "health",
        "model",
        "serial",
        "firmware_rev",
        "temperature",
        "num_errors",
        "state",
        "percent_used",
        "available_spare",
        "media_errors",
        "power_on_hours",
    ):
        path = os.path.join(base, attr)
        try:
            with open(path, "r", encoding="ascii", errors="replace") as fh:
                values[attr] = fh.read().strip()
        except OSError:
            continue
    if not values:
        result.reason = "sysfs did not expose health attributes for this device."
        return result
    result.device_model = values.get("model", "")
    result.serial = values.get("serial", "")
    result.firmware = values.get("firmware_rev", "")
    if "health" in values:
        result.health_text = values["health"].upper()
        result.health_passed = result.health_text == "OK"
        result.protocol = "nvme"
        result.available = True
    if "temperature" in values:
        try:
            result.temperature_c = int(values["temperature"]) / (1000.0 if len(values["temperature"]) > 3 else 1.0)
        except ValueError:
            pass
    if "percent_used" in values:
        try:
            result.nvme_percent_used = int(values["percent_used"].rstrip("%"))
            result.available = True
        except ValueError:
            pass
    if "media_errors" in values:
        try:
            count = int(values["media_errors"])
            result.attributes.append(
                SmartAttribute(187, "NVMe_Media_Errors", 100, 100, 0, count, str(count))
            )
            result.available = True
        except ValueError:
            pass
    if not result.available:
        result.reason = "sysfs exposed only identity fields; no health counters found."
    return result


# ---------------------------------------------------------------------------
# macOS native sources
# ---------------------------------------------------------------------------


def read_smart_macos(device_path: str) -> SmartResult:
    result = SmartResult(source="macos-diskutil")
    bs = re.sub(r"^/dev/(r?)disk(\d+)s\d+$", r"/dev/disk\2", device_path)
    try:
        proc = run_process(["diskutil", "info", bs], timeout=20)
    except Exception as exc:
        result.reason = f"diskutil info failed: {exc}"
        return result
    if not proc.ok:
        result.reason = "diskutil did not return information for this device."
        return result
    info = {}
    for line in proc.stdout.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            info[key.strip()] = value.strip()
    result.device_model = info.get("Device / Media Name", "")
    result.serial = info.get("Disk / Media Serial Number", info.get("Volume UUID", ""))
    result.firmware = info.get("Firmware Revision", "")
    result.protocol = info.get("Protocol", "").lower()
    smart = info.get("SMART Status", "")
    if smart:
        result.health_text = smart
        if "Verified" in smart:
            result.health_passed = True
        elif "Failing" in smart or "Fail" in smart:
            result.health_passed = False
        result.available = "Not Supported" not in smart
    temp = info.get("Temperature", "")
    if temp:
        try:
            result.temperature_c = float(re.sub(r"[^0-9.]", "", temp))
        except ValueError:
            pass
    if not result.available:
        result.reason = (
            "macOS reports SMART as not supported for this device. USB flash drives "
            "and SD cards normally do not pass SMART through their bridge chip."
        )
    return result


# ---------------------------------------------------------------------------
# Windows native sources
# ---------------------------------------------------------------------------


def read_smart_windows(device_path: str) -> SmartResult:
    result = SmartResult(source="windows-storage")
    try:
        from usbdoctor.devices import windows as win  # local import: cycle-free
    except Exception as exc:  # pragma: no cover
        result.reason = f"Windows provider unavailable: {exc}"
        return result
    info = win.storage_reliability(device_path)
    if info is None:
        result.reason = (
            "Windows did not return reliability counters for this device "
            "(USB flash drives typically do not implement them)."
        )
        return result
    result.device_model = info.get("model", "")
    result.firmware = info.get("firmware", "")
    result.serial = info.get("serial", "")
    temp = info.get("temperature")
    if temp is not None:
        try:
            result.temperature_c = float(temp)
        except (TypeError, ValueError):
            pass
    wear = info.get("wear")
    if wear is not None:
        try:
            result.nvme_percent_used = int(wear)
        except (TypeError, ValueError):
            pass
    poh = info.get("power_on_hours")
    if poh is not None:
        try:
            result.power_on_hours = int(poh)
        except (TypeError, ValueError):
            pass
    read_errors = info.get("read_errors_total")
    if read_errors is not None:
        try:
            result.attributes.append(
                SmartAttribute(187, "Storage_Read_Errors_Total", 100, 100, 0, int(read_errors), str(read_errors))
            )
        except (TypeError, ValueError):
            pass
    write_errors = info.get("write_errors_total")
    if write_errors is not None:
        try:
            result.attributes.append(
                SmartAttribute(198, "Storage_Write_Errors_Total", 100, 100, 0, int(write_errors), str(write_errors))
            )
        except (TypeError, ValueError):
            pass
    result.available = bool(
        result.temperature_c is not None
        or result.nvme_percent_used is not None
        or result.attributes
        or poh is not None
    )
    if not result.available:
        result.reason = "Windows reported identity only; no health counters available on this device."
    return result


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def read_smart(device_path: str, *, bus: Optional[str] = None, deep_usb: bool = True) -> SmartResult:
    """Best SMART data available on this platform, never fabricated."""
    system = detect_system()
    candidates: list[SmartResult] = []
    if tool_available("smartctl"):
        candidates.append(read_smart_smartctl(device_path, deep_usb=deep_usb))
    if system == "linux":
        candidates.append(read_smart_linux_sysfs(device_path))
    elif system == "macos":
        candidates.append(read_smart_macos(device_path))
    elif system == "windows":
        candidates.append(read_smart_windows(device_path))
    usable = [c for c in candidates if c.available]
    if usable:
        # prefer the richest source (most attributes), then smartctl
        usable.sort(key=lambda r: (len(r.attributes), r.source == "smartctl"), reverse=True)
        best = usable[0]
        for extra in usable[1:]:
            if best.temperature_c is None and extra.temperature_c is not None:
                best.temperature_c = extra.temperature_c
            if best.power_on_hours is None and extra.power_on_hours is not None:
                best.power_on_hours = extra.power_on_hours
        if not best.reason:
            best.reason = ""
        return best
    if candidates:
        merged = candidates[0]
        merged.available = False
        merged.reason = " ".join(c.reason for c in candidates if c.reason)[:600]
        return merged
    return SmartResult(
        available=False,
        source="none",
        reason="No SMART provider is available on this system.",
    )


def smart_attribute_issues(smart: SmartResult) -> list[dict[str, Any]]:
    """Translate SMART values into structured findings for the health engine."""
    findings: list[dict[str, Any]] = []
    if not smart.available:
        return findings
    if smart.health_passed is False:
        findings.append(
            {
                "severity": "critical",
                "code": "SMART_HEALTH_FAILED",
                "message": "The drive's own SMART health check reports FAILURE.",
            }
        )
    for attr_id, spec in ATA_CRITICAL_ATTRS.items():
        attr = smart.attribute(attr_id)
        if attr is None or spec["warn"] is None:
            continue
        if attr.raw >= (spec["fail"] or 1 << 62):
            findings.append(
                {
                    "severity": "critical",
                    "code": f"SMART_ATTR_{attr_id}_HIGH",
                    "message": f"{spec['name']} = {attr.raw:,} ({spec['meaning']}).",
                    "value": attr.raw,
                }
            )
        elif attr.raw >= spec["warn"]:
            findings.append(
                {
                    "severity": "warning",
                    "code": f"SMART_ATTR_{attr_id}_ELEVATED",
                    "message": f"{spec['name']} = {attr.raw:,} ({spec['meaning']}).",
                    "value": attr.raw,
                }
            )
    if smart.temperature_c is not None:
        if smart.temperature_c >= 70:
            findings.append(
                {
                    "severity": "critical",
                    "code": "SMART_TEMPERATURE_HIGH",
                    "message": f"Device temperature {smart.temperature_c:.0f}°C is dangerously high.",
                    "value": smart.temperature_c,
                }
            )
        elif smart.temperature_c >= 60:
            findings.append(
                {
                    "severity": "warning",
                    "code": "SMART_TEMPERATURE_WARM",
                    "message": f"Device temperature {smart.temperature_c:.0f}°C is above the comfortable range.",
                    "value": smart.temperature_c,
                }
            )
    if smart.nvme_percent_used is not None:
        if smart.nvme_percent_used >= 90:
            findings.append(
                {
                    "severity": "warning",
                    "code": "SMART_WEAR_HIGH",
                    "message": f"NVMe media wear is at {smart.nvme_percent_used}% of rated endurance.",
                    "value": smart.nvme_percent_used,
                }
            )
    if smart.nvme_available_spare is not None and smart.nvme_available_spare < 10:
        findings.append(
            {
                "severity": "critical",
                "code": "SMART_SPARE_LOW",
                "message": f"NVMe available spare blocks are down to {smart.nvme_available_spare}%.",
                "value": smart.nvme_available_spare,
            }
        )
    if smart.nvme_critical_warning:
        findings.append(
            {
                "severity": "critical",
                "code": "SMART_NVME_CRITICAL",
                "message": "The NVMe controller raised a critical warning flag.",
                "value": smart.nvme_critical_warning,
            }
        )
    if smart.error_log_entries > 20:
        findings.append(
            {
                "severity": "warning",
                "code": "SMART_ERROR_LOG_LARGE",
                "message": f"The device error log contains {smart.error_log_entries} entries.",
                "value": smart.error_log_entries,
            }
        )
    return findings
