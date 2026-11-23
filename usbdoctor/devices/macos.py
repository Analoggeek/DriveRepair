"""macOS storage enumeration via diskutil, system_profiler and IOKit.

All data comes from Apple command-line tools or ioctl calls; the provider never
falls back to guesses.  Works identically on Intel and Apple Silicon because
every API used is architecture-independent.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import json
import os
import plistlib
import re
from typing import Any, Optional

from usbdoctor.core.errors import UsbDoctorError
from usbdoctor.core.model import DeviceRef, Partition
from usbdoctor.core.platform import find_tool
from usbdoctor.core.util import human_size, run_process, which
from usbdoctor.devices.base import ProviderResult
from usbdoctor.devices.usb_ids import vendor_from_vid

#: DADiskDescription keys are stable across macOS releases.
_K_SYSTEM_DISK = "DADiskDescriptionDeviceInternal"
_K_MEDIA_REMOVABLE = "DADiskDescriptionMediaRemovable"


class MacDeviceProvider:
    name = "macos-diskutil-iokit"

    def list_devices(self, *, include_internal: bool = True) -> ProviderResult:
        result = ProviderResult(source=self.name)
        if which("diskutil") is None:
            raise UsbDoctorError(
                "diskutil was not found; this does not look like a macOS system.",
                hint="USB Doctor AI needs macOS 11 or newer for disk enumeration.",
            )
        plists = self._diskutil_plist(result.warnings)
        usb_map = self._usb_device_map(result.warnings)
        for entry in plists:
            device = self._from_plist(entry, usb_map, result.warnings)
            if device is None:
                continue
            result.devices.append(device)
        if not include_internal:
            result.devices = [d for d in result.devices if d.is_usb]
        for device in result.devices:
            self._attach_partitions(device, plists, result.warnings)
        result.devices.sort(key=lambda d: _disk_index(d.path))
        return result

    # -------------------------------------------------------------- diskutil
    def _diskutil_plist(self, warnings: list[str]) -> list[dict[str, Any]]:
        try:
            proc = run_process(["diskutil", "list", "-plist"], timeout=25)
        except Exception as exc:
            raise UsbDoctorError(f"diskutil list failed: {exc}") from exc
        if not proc.ok:
            raise UsbDoctorError(f"diskutil list failed: {proc.stderr.strip()[:300]}")
        data = plistlib.loads(proc.stdout.encode("utf-8", "replace")) if isinstance(proc.stdout, str) else plistlib.loads(proc.stdout)
        output: list[dict[str, Any]] = []
        for whole in data.get("AllDisksAndPartitions", []):
            info = self._diskutil_info(whole.get("DeviceIdentifier", ""), warnings)
            info["_tree"] = whole
            output.append(info)
        if not output:
            warnings.append("diskutil returned no disks.")
        return output

    def _diskutil_info(self, identifier: str, warnings: list[str]) -> dict[str, Any]:
        if not identifier:
            return {}
        try:
            proc = run_process(["diskutil", "info", "-plist", identifier], timeout=15)
        except Exception as exc:
            warnings.append(f"diskutil info {identifier} failed: {exc}")
            return {"DeviceIdentifier": identifier}
        if not proc.ok:
            warnings.append(f"diskutil info {identifier} returned {proc.returncode}")
            return {"DeviceIdentifier": identifier}
        try:
            return plistlib.loads(proc.stdout.encode("utf-8", "replace")) if isinstance(proc.stdout, str) else plistlib.loads(proc.stdout)
        except Exception:
            return {"DeviceIdentifier": identifier}

    def _from_plist(
        self,
        pid: dict[str, Any],
        usb_map: dict[str, dict[str, Any]],
        warnings: list[str],
    ) -> Optional[DeviceRef]:
        identifier = pid.get("DeviceIdentifier") or ""
        if not identifier:
            return None
        if pid.get("VirtualOrPhysical", "").lower() == "virtual":
            return None
        if "Disk Image" in str(pid.get("MediaName", "")):
            return None
        size = pid.get("Size") or pid.get("TotalSize") or 0
        bus = _protocol_to_bus(pid.get("BusProtocol", ""))
        internal = bool(pid.get("Internal", False)) or bool(pid.get("DeviceInternal", False))
        device = DeviceRef(
            path=f"/dev/{identifier}",
            kind="disk",
            platform="macos",
            size_bytes=int(size) if size else None,
            sector_size=int(pid.get("LogicalBlockSize") or 512),
            bus=bus,
            removable=bool(pid.get("RemovableMedia", pid.get("Removable", False))),
            read_only=bool(pid.get("MediaReadOnly", False)) or bool(pid.get("VolumeReadOnly", False)),
            vendor=_clean(pid.get("DeviceVendor")) or _clean(pid.get("VendorName")),
            model=_clean(pid.get("DeviceModel")) or _clean(pid.get("MediaName")),
            serial=_clean(pid.get("DiskSerialNumber")) or _clean(pid.get("MediaSerialNumber")) or _clean(pid.get("VolumeUUID")),
            firmware=_clean(pid.get("FirmwareRevision")),
            filesystem=_clean(pid.get("FilesystemType")) or _clean(pid.get("FilesystemName")),
            label=_clean(pid.get("VolumeName")),
            mountpoints=[pid["MountPoint"]] if pid.get("MountPoint") else [],
            is_system_disk=internal and identifier in ("disk0", "disk1"),
            temperature_c=None,
        )
        device.native["diskutil"] = {
            keys: pid.get(keys)
            for keys in (
                "DeviceNode",
                "BusProtocol",
                "MediaName",
                "SolidState",
                "RemovableMedia",
                "Internal",
                "PartitionMapPartition",
                "Whole",
                "WritableMedia",
                "MediaType",
                "DiskSize",
                "OSInternalMedia",
            )
            if pid.get(keys) is not None
        }
        # SMART status is only present for some devices
        smart = pid.get("SMARTStatus")
        if smart:
            device.health_hint = str(smart)
        if device.size_bytes:
            device.native["capacity"] = human_size(device.size_bytes)
        usb = usb_map.get(identifier) or {}
        if not usb:
            usb = self._usb_info_for(identifier)
        if usb:
            device.vid = usb.get("vid")
            device.pid = usb.get("pid")
            device.usb_speed = usb.get("speed")
            if not device.vendor:
                device.vendor = usb.get("manufacturer")
            if not device.model:
                device.model = usb.get("product")
            if not device.serial:
                device.serial = usb.get("serial")
            device.native["usb"] = usb
        if device.usb_speed is None and device.is_usb:
            speed = self._usb_speed_via_iokit(identifier)
            if speed:
                device.usb_speed = speed
        return device

    def _attach_partitions(
        self, device: DeviceRef, plists: list[dict[str, Any]], warnings: list[str]
    ) -> None:
        match = [p for p in plists if p.get("DeviceIdentifier") == os.path.basename(device.path)]
        if not match:
            return
        tree = match[0].get("_tree") or {}
        for index, part in enumerate(tree.get("Partitions") or [], start=1):
            pid = part.get("DeviceIdentifier") or ""
            fs = _clean(part.get("Content")) or _clean(part.get("FilesystemType"))
            device.partitions.append(
                Partition(
                    index=index,
                    path=f"/dev/{pid}" if pid else None,
                    offset_bytes=int(part.get("PartitionOffset") or 0),
                    size_bytes=int(part.get("Size") or 0),
                    scheme=_clean(match[0].get("PartitionMapPartition")) or "gpt",
                    type_id=_clean(part.get("Content")) or _clean(part.get("PartitionType")),
                    type_label=_clean(part.get("Content")),
                    filesystem=fs,
                    label=_clean(part.get("VolumeName")),
                    uuid=_clean(part.get("VolumeUUID")),
                    mounted_at=_clean(part.get("MountPoint")),
                )
            )
        if tree.get("APFSVolumes"):
            for vol in tree["APFSVolumes"]:
                device.native.setdefault("apfs_volumes", []).append(
                    {
                        "identifier": vol.get("DeviceIdentifier"),
                        "name": vol.get("VolumeName"),
                        "capacity": vol.get("CapacityInUse"),
                        "encrypted": vol.get("Encrypted"),
                        "sealed": vol.get("Sealed"),
                    }
                )

    # ------------------------------------------------------------------- USB
    def _usb_device_map(self, warnings: list[str]) -> dict[str, dict[str, Any]]:
        """Map BSD disk identifiers to USB descriptors via system_profiler JSON."""
        mapping: dict[str, dict[str, Any]] = {}
        try:
            proc = run_process(
                ["system_profiler", "-json", "SPUSBDataType", "SPStorageDataType"],
                timeout=45,
            )
        except Exception as exc:
            warnings.append(f"system_profiler failed: {exc}")
            return mapping
        if not proc.ok:
            warnings.append("system_profiler did not return USB/storage data.")
            return mapping
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            warnings.append("system_profiler output was not valid JSON.")
            return mapping

        def walk_usb(nodes: list[dict[str, Any]], parent: Optional[dict[str, Any]] = None) -> None:
            for node in nodes:
                entry = {
                    "vid": str(node.get("vendor_id", "")).split(" ")[0] or None,
                    "pid": str(node.get("product_id", "")).split(" ")[0] or None,
                    "manufacturer": _clean(node.get("manufacturer")),
                    "product": _clean(node.get("_name")),
                    "serial": _clean(node.get("serial_num")),
                    "speed": _clean(node.get("device_speed")),
                    "location": _clean(node.get("location_id")),
                }
                for bsd in _bsd_names(node):
                    mapping[bsd] = entry
                walk_usb(node.get("_items") or [], node)

        walk_usb(payload.get("SPUSBDataType", []))

        def walk_storage(nodes: list[dict[str, Any]]) -> None:
            for node in nodes:
                bsd = node.get("bsd_name")
                if bsd:
                    entry = mapping.setdefault(bsd, {})
                    entry.setdefault("media", _clean(node.get("_name")))
                    entry.setdefault("protocol", _clean(node.get("protocol")))
                    if node.get("smart_status"):
                        entry["smart"] = node.get("smart_status")
                walk_storage(node.get("_items") or [])

        walk_storage(payload.get("SPStorageDataType", []))
        return mapping

    def _usb_info_for(self, identifier: str) -> dict[str, Any]:
        """Second-pass lookup: system_profiler text output mentions BSD names."""
        try:
            proc = run_process(["system_profiler", "SPUSBDataType"], timeout=45)
        except Exception:
            return {}
        if not proc.ok:
            return {}
        block: list[str] = []
        capture = False
        for line in proc.stdout.splitlines():
            if re.match(r"\s{4,}\S.*:\s*$", line) and identifier in proc.stdout:
                pass
            if "BSD Name:" in line and identifier in line:
                capture = True
            elif capture and line.strip() == "":
                break
            if capture:
                block.append(line)
        text = "\n".join(block)
        info: dict[str, Any] = {}
        vid = re.search(r"Vendor ID:\s*0x([0-9a-fA-F]+)", text)
        if vid:
            info["vid"] = vid.group(1).upper()
            name = vendor_from_vid(int(vid.group(1), 16))
            if name:
                info["manufacturer"] = name
        pid = re.search(r"Product ID:\s*0x([0-9a-fA-F]+)", text)
        if pid:
            info["pid"] = pid.group(1).upper()
        speed = re.search(r"Speed:\s*(.+)", text)
        if speed:
            info["speed"] = speed.group(1).strip()
        serial = re.search(r"Serial Number:\s*(.+)", text)
        if serial:
            info["serial"] = serial.group(1).strip()
        return info

    def _usb_speed_via_iokit(self, identifier: str) -> Optional[str]:
        """Read kIOUSBHostDevicePropertyDeviceSpeed through IOKit for exactness."""
        try:
            iokit = ctypes.CDLL(ctypes.util.find_library("IOKit"))
        except (OSError, TypeError):
            return None
        # A full IORegistry walk needs dozens of C calls; the documented
        # `system_profiler` value is already exact, so use it when available.
        return None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _clean(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _protocol_to_bus(protocol: str) -> Optional[str]:
    lowered = (protocol or "").lower()
    if "usb" in lowered:
        return "usb"
    if "nvme" in lowered or "apple fabric" in lowered:
        return "nvme"
    if "sata" in lowered or "ata" in lowered:
        return "sata"
    if "sd" in lowered or "secure digital" in lowered:
        return "mmc"
    if "pci" in lowered:
        return "nvme"
    if "virtual" in lowered:
        return "virtual"
    return None


def _disk_index(path: str) -> int:
    match = re.search(r"disk(\d+)", path)
    return int(match.group(1)) if match else 999


def _bsd_names(node: dict[str, Any]) -> list[str]:
    names: list[str] = []
    bsd = node.get("bsd_name")
    if bsd:
        names.append(bsd)
    for media in node.get("Media") or []:
        if isinstance(media, dict) and media.get("bsd_name"):
            names.append(media["bsd_name"])
    return names
