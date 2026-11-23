"""Windows storage enumeration via Storage Management APIs.

Primary source: ``MSFT_PhysicalDisk`` / ``MSFT_Disk`` / ``MSFT_Partition`` from
the ``Root/Microsoft/Windows/Storage`` WMI namespace (the same data Disk
Management shows).  ``Win32_DiskDrive`` is used only to fill gaps, and
PowerShell ``Get-StorageReliabilityCounter`` supplies SMART-equivalent counters
on NVMe/SSD media.

Every call is wrapped: on a locked-down machine WMI can be unavailable and the
provider then reports why instead of inventing devices.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Optional

from usbdoctor.core.errors import UsbDoctorError
from usbdoctor.core.model import DeviceRef, Partition
from usbdoctor.core.platform import detect_system, find_tool, is_admin
from usbdoctor.core.util import human_size, run_process, which
from usbdoctor.devices.base import ProviderResult
from usbdoctor.devices.usb_ids import USB_SPEEDS, vendor_from_vid

BUS_TYPE = {
    0: None,
    1: "scsi",
    2: "atapi",
    3: "ata",
    4: "ieee1394",
    5: "ssa",
    6: "fibre",
    7: "usb",
    8: "raid",
    9: "iscsi",
    10: "sas",
    11: "sata",
    12: "sd",
    13: "mmc",
    14: "virtual",
    15: "filebacked",
    16: "spaces",
    17: "nvme",
    18: "scm",
    19: "ufs",
}

MEDIA_TYPE = {0: "Unspecified", 3: "HDD", 4: "SSD", 5: "SCM"}


class WindowsDeviceProvider:
    name = "windows-storage-wmi"

    def list_devices(self, *, include_internal: bool = True) -> ProviderResult:
        result = ProviderResult(source=self.name, requires_admin=False)
        if not detect_system() == "windows":
            raise UsbDoctorError("The Windows provider can only run on Windows.")
        disks = _powershell_json(
            "Get-Disk | Select-Object Number,FriendlyName,SerialNumber,Size,BusType,MediaType,"
            "PartitionStyle,IsBoot,IsSystem,IsReadOnly,IsOffline,HealthStatus,OperationalStatus,"
            "FirmwareVersion,UniqueId | ConvertTo-Json -Depth 4",
            warnings=result.warnings,
        )
        if disks is None:
            raise UsbDoctorError(
                "The Windows Storage API did not respond.",
                hint="Run USB Doctor AI as Administrator, and make sure the Storage Management "
                "service is running.",
            )
        if isinstance(disks, dict):
            disks = [disks]
        wmi_usb = self._usb_controller_map(result.warnings)
        for disk in disks or []:
            device = self._from_disk(disk, wmi_usb, result.warnings)
            if device:
                result.devices.append(device)
        for device in result.devices:
            self._attach_partitions(device, result.warnings)
        if not include_internal:
            result.devices = [d for d in result.devices if d.is_usb]
        return result

    # ------------------------------------------------------------- converters
    def _from_disk(
        self, disk: dict[str, Any], usb_map: dict[int, dict[str, Any]], warnings: list[str]
    ) -> Optional[DeviceRef]:
        try:
            number = int(disk.get("Number"))
        except (TypeError, ValueError):
            return None
        bus = BUS_TYPE.get(int(disk.get("BusType") or 0))
        model = _clean(disk.get("FriendlyName"))
        device = DeviceRef(
            path=f"\\\\.\\PhysicalDrive{number}",
            kind="disk",
            platform="windows",
            size_bytes=_as_int(disk.get("Size")),
            sector_size=512,
            bus=bus,
            removable=bus in ("usb", "sd", "mmc"),
            read_only=bool(disk.get("IsReadOnly")),
            model=model,
            serial=_clean(disk.get("SerialNumber")),
            firmware=_clean(disk.get("FirmwareVersion")),
            is_system_disk=bool(disk.get("IsBoot")) or bool(disk.get("IsSystem")) or number == 0,
            health_hint=_clean(disk.get("HealthStatus")),
        )
        device.native["msft_disk"] = {
            "number": number,
            "partition_style": disk.get("PartitionStyle"),
            "media_type": MEDIA_TYPE.get(int(disk.get("MediaType") or 0), "Unknown"),
            "operational_status": disk.get("OperationalStatus"),
            "health_status": disk.get("HealthStatus"),
            "unique_id": disk.get("UniqueId"),
            "offline": disk.get("IsOffline"),
        }
        if device.size_bytes:
            device.native["capacity"] = human_size(device.size_bytes)
        usb = usb_map.get(number)
        if usb:
            device.vid = usb.get("vid")
            device.pid = usb.get("pid")
            device.usb_speed = usb.get("speed")
            if not device.model:
                device.model = usb.get("product")
            if not device.serial:
                device.serial = usb.get("serial")
            device.native["usb"] = usb
        self._fill_from_win32_diskdrive(device, number, warnings)
        self._fill_from_storage_reliability(device, number)
        return device

    def _fill_from_win32_diskdrive(self, device: DeviceRef, number: int, warnings: list[str]) -> None:
        script = (
            f"Get-CimInstance Win32_DiskDrive | Where-Object {{$_.Index -eq {number}}} | "
            "Select-Object Index,Model,SerialNumber,FirmwareRevision,InterfaceType,MediaType,"
            "BytesPerSector,Partitions,PNPDeviceID,Status | ConvertTo-Json -Depth 3"
        )
        data = _powershell_json(script, warnings=warnings)
        if isinstance(data, list):
            data = data[0] if data else None
        if not isinstance(data, dict):
            return
        if not device.model:
            device.model = _clean(data.get("Model"))
        if not device.serial:
            device.serial = _clean(data.get("SerialNumber"))
        if not device.firmware:
            device.firmware = _clean(data.get("FirmwareRevision"))
        if data.get("BytesPerSector"):
            device.sector_size = int(data["BytesPerSector"])
        media = _clean(data.get("MediaType"))
        if media:
            device.native.setdefault("msft_disk", {})["win32_media_type"] = media
        pnp = _clean(data.get("PNPDeviceID"))
        if pnp:
            device.native.setdefault("msft_disk", {})["pnp_device_id"] = pnp
            vidpid = re.search(r"VID_([0-9A-F]{4})&PID_([0-9A-F]{4})", pnp, re.I)
            if vidpid:
                device.vid = device.vid or vidpid.group(1).upper()
                device.pid = device.pid or vidpid.group(2).upper()
                vendor = vendor_from_vid(int(vidpid.group(1), 16))
                if vendor:
                    device.vendor = device.vendor or vendor
        if _clean(data.get("Status")) not in (None, "OK"):
            device.limitations.append(f"Windows reports drive status: {data.get('Status')}")
        bus = _clean(data.get("InterfaceType"))
        if bus and device.bus is None:
            device.bus = {
                "USB": "usb",
                "SCSI": "scsi",
                "IDE": "ata",
                "SATA": "sata",
                "NVMe": "nvme",
                "SD": "mmc",
            }.get(bus.upper(), bus.lower())

    def _fill_from_storage_reliability(self, device: DeviceRef, number: int) -> None:
        info = storage_reliability(device.path)
        if not info:
            return
        if info.get("temperature") is not None:
            try:
                device.temperature_c = float(info["temperature"])
            except (TypeError, ValueError):
                pass

    def _attach_partitions(self, device: DeviceRef, warnings: list[str]) -> None:
        number = device.path.rsplit("PhysicalDrive", 1)[-1]
        script = (
            f"Get-Partition -DiskNumber {number} -ErrorAction SilentlyContinue | "
            "Select-Object PartitionNumber,DriveLetter,Offset,Size,Type,GptType,IsReadOnly | "
            "ConvertTo-Json -Depth 3"
        )
        parts = _powershell_json(script, warnings=warnings)
        if isinstance(parts, dict):
            parts = [parts]
        for part in parts or []:
            number_p = part.get("PartitionNumber")
            if number_p is None:
                continue
            letter = part.get("DriveLetter")
            mounted = f"{letter}:\\" if isinstance(letter, str) and letter else None
            fs = None
            label = None
            if mounted:
                vol = _powershell_json(
                    f"Get-Volume -DriveLetter {letter} "
                    "| Select-Object FileSystem,FileSystemLabel,HealthStatus | ConvertTo-Json",
                    warnings=warnings,
                )
                if isinstance(vol, dict):
                    fs = _clean(vol.get("FileSystem"))
                    label = _clean(vol.get("FileSystemLabel"))
            device.partitions.append(
                Partition(
                    index=int(number_p),
                    path=f"\\\\?\\Volume{{disk{number}-part{number_p}}}",
                    offset_bytes=_as_int(part.get("Offset")) or 0,
                    size_bytes=_as_int(part.get("Size")) or 0,
                    scheme=(device.native.get("msft_disk", {}) or {}).get("partition_style"),
                    type_id=_clean(part.get("GptType")) or _clean(part.get("Type")),
                    type_label=_clean(part.get("Type")),
                    filesystem=fs,
                    label=label,
                    mounted_at=mounted,
                    read_only=bool(part.get("IsReadOnly")) if part.get("IsReadOnly") is not None else None,
                )
            )

    # ------------------------------------------------------------------- USB
    def _usb_controller_map(self, warnings: list[str]) -> dict[int, dict[str, Any]]:
        """Correlate physical drive numbers with USB VID/PID via Win32_DiskDrive."""
        script = (
            "Get-CimInstance Win32_DiskDrive | Where-Object {$_.InterfaceType -eq 'USB'} | "
            "Select-Object Index,Model,SerialNumber,PNPDeviceID | ConvertTo-Json -Depth 3"
        )
        data = _powershell_json(script, warnings=warnings)
        if isinstance(data, dict):
            data = [data]
        mapping: dict[int, dict[str, Any]] = {}
        for item in data or []:
            try:
                index = int(item.get("Index"))
            except (TypeError, ValueError):
                continue
            pnp = _clean(item.get("PNPDeviceID")) or ""
            vidpid = re.search(r"VID_([0-9A-F]{4})&PID_([0-9A-F]{4})", pnp, re.I)
            entry: dict[str, Any] = {
                "product": _clean(item.get("Model")),
                "serial": _clean(item.get("SerialNumber")),
                "pnp": pnp,
            }
            if vidpid:
                entry["vid"] = vidpid.group(1).upper()
                entry["pid"] = vidpid.group(2).upper()
                vendor = vendor_from_vid(int(vidpid.group(1), 16))
                if vendor:
                    entry["manufacturer"] = vendor
            mapping[index] = entry
        return mapping


# ---------------------------------------------------------------------------
# PowerShell helpers
# ---------------------------------------------------------------------------


def _powershell() -> Optional[str]:
    return which("powershell") or which("pwsh")


def _powershell_json(script: str, *, warnings: list[str], timeout: float = 45.0) -> Any:
    exe = _powershell()
    if not exe:
        warnings.append("PowerShell was not found; Windows Storage queries are unavailable.")
        return None
    argv = [
        exe,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        "$ProgressPreference='SilentlyContinue'; " + script,
    ]
    try:
        proc = run_process(argv, timeout=timeout)
    except Exception as exc:
        warnings.append(f"PowerShell query failed: {exc}")
        return None
    if not proc.ok or not proc.stdout.strip():
        if proc.stderr.strip():
            warnings.append(f"PowerShell: {proc.stderr.strip().splitlines()[0][:200]}")
        return None
    text = proc.stdout.strip()
    start = text.find("[") if "[" in text[:3] else text.find("{")
    if start < 0:
        return None
    try:
        return json.loads(text[start:])
    except json.JSONDecodeError:
        warnings.append("PowerShell returned non-JSON output.")
        return None


def storage_reliability(device_path: str) -> Optional[dict[str, Any]]:
    """Return Get-StorageReliabilityCounter data for a physical drive, if any."""
    match = re.search(r"PhysicalDrive(\d+)", device_path, re.I)
    if not match:
        return None
    number = match.group(1)
    script = (
        f"$d = Get-PhysicalDisk | Where-Object DeviceId -eq '{number}'; "
        "if ($d) {$c = $d | Get-StorageReliabilityCounter -ErrorAction SilentlyContinue; "
        "if ($c) {[pscustomobject]@{"
        "temperature=$c.Temperature; wear=$c.Wear; power_on_hours=$c.PowerOnHours; "
        "read_errors_total=$c.ReadErrorsTotal; write_errors_total=$c.WriteErrorsTotal; "
        "read_errors_uncorrected=$c.ReadErrorsUncorrected; "
        "write_errors_uncorrected=$c.WriteErrorsUncorrected; "
        "load_unload_count=$c.LoadUnloadCycleCount; "
        "start_stop_cycle=$c.StartStopCycleCount; "
        "model=$d.FriendlyName; firmware=$d.FirmwareVersion; serial=$d.SerialNumber"
        "} | ConvertTo-Json}}"
    )
    data = _powershell_json(script, warnings=[], timeout=30)
    if not isinstance(data, dict):
        return None
    out = {k: v for k, v in data.items() if v is not None}
    return out or None


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clean(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def elevate_hint() -> str:
    return (
        "Administrator privileges are granted."
        if is_admin()
        else "Run USB Doctor AI as Administrator to allow repair operations on raw disks."
    )
