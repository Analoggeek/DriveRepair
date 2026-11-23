"""Linux storage enumeration via lsblk (util-linux), sysfs and udev.

Sources are all kernel-supplied; nothing here guesses.  When ``lsblk`` is not
available the provider falls back to walking ``/sys/class/block`` directly.
"""

from __future__ import annotations

import glob
import json
import os
import re
from typing import Any, Optional

from usbdoctor.core.errors import UsbDoctorError
from usbdoctor.core.model import DeviceRef, Partition
from usbdoctor.core.platform import find_tool
from usbdoctor.core.util import human_size, run_process, which
from usbdoctor.devices.base import ProviderResult
from usbdoctor.devices.usb_ids import USB_SPEEDS, vendor_from_vid

#: Mount points whose device is the running system must never be offered as a
#: repair target; they are still listed (marked) so the UI can explain why.
_SYSTEM_MOUNTS = {"/", "/boot", "/boot/efi", "/usr", "/var", "/home"}


class LinuxDeviceProvider:
    name = "linux-sysfs"

    def list_devices(self, *, include_internal: bool = True) -> ProviderResult:
        result = ProviderResult(source=self.name)
        lsblk = which("lsblk")
        if lsblk:
            try:
                result.devices = self._via_lsblk(lsblk, result.warnings)
            except UsbDoctorError:
                raise
            except Exception as exc:
                result.warnings.append(f"lsblk parsing failed ({exc}); falling back to sysfs.")
                result.devices = self._via_sysfs(result.warnings)
        else:
            result.warnings.append("lsblk not found; using /sys/class/block directly.")
            result.devices = self._via_sysfs(result.warnings)
        if not include_internal:
            result.devices = [d for d in result.devices if d.is_usb or d.kind != "disk"]
        for device in result.devices:
            self._enrich(device, result.warnings)
        return result

    # ------------------------------------------------------------------ lsblk
    #: Preferred columns; older util-linux releases reject unknown names, so the
    #: supported subset is negotiated at runtime instead of assumed.
    _COLUMNS = (
        "NAME",
        "KNAME",
        "PATH",
        "TYPE",
        "SIZE",
        "MODEL",
        "SERIAL",
        "VENDOR",
        "RO",
        "FSTYPE",
        "LABEL",
        "UUID",
        "TRAN",
        "LOG-SEC",
        "RM",
        "HOTPLUG",
        "PTTYPE",
        "PARTTYPE",
        "MOUNTPOINTS",
        "MOUNTPOINT",
        "FSVER",
        "DISKSEQ",
    )

    def _lsblk_columns(self, lsblk: str) -> list[str]:
        """Return the column names this lsblk build actually understands."""
        try:
            probe = run_process([lsblk, "--help"], timeout=10)
            help_text = (probe.stdout or "") + (probe.stderr or "")
        except Exception:
            help_text = ""
        if "--list-columns" in help_text:
            try:
                listing = run_process([lsblk, "--list-columns"], timeout=10)
                if listing.ok:
                    tokens = {
                        line.split()[0].upper()
                        for line in listing.stdout.splitlines()
                        if line.strip() and not line.startswith(" ")
                    }
                    supported = [c for c in self._COLUMNS if c in tokens]
                    if supported:
                        return supported
            except Exception:
                pass
        # Fall back to probing one column at a time (cheap, happens once).
        supported = []
        for column in self._COLUMNS:
            try:
                test = run_process([lsblk, "-J", "-o", column, "--noheadings"], timeout=10)
            except Exception:
                continue
            if test.ok or "unknown column" not in (test.stderr or "").lower():
                supported.append(column)
        return supported or ["NAME", "TYPE", "SIZE", "FSTYPE"]

    def _via_lsblk(self, lsblk: str, warnings: list[str]) -> list[DeviceRef]:
        columns = self._lsblk_columns(lsblk)
        argv = [lsblk, "-J", "-b", "-o", ",".join(columns)]
        proc = run_process(argv, timeout=25)
        if not proc.ok:
            raise UsbDoctorError(f"lsblk failed: {proc.stderr.strip()[:300]}")
        payload = json.loads(proc.stdout or "{}")
        if "MOUNTPOINTS" not in columns and "MOUNTPOINT" not in columns:
            warnings.append("This lsblk version cannot report mount points; partition mount state may be incomplete.")
        devices: list[DeviceRef] = []
        system_disks = _system_disk_names()
        for block in payload.get("blockdevices", []):
            dev = self._from_lsblk(block, system_disks, warnings, parent=None)
            if dev is not None:
                devices.append(dev)
        return devices

    def _from_lsblk(
        self,
        block: dict[str, Any],
        system_disks: set[str],
        warnings: list[str],
        parent: Optional[DeviceRef],
    ) -> Optional[DeviceRef]:
        btype = (block.get("type") or "").lower()
        name = block.get("kname") or block.get("name") or ""
        size = _int(block.get("size"))
        if btype == "loop":
            return None
        if btype not in ("disk", "part", "rom"):
            # skip md/raid/dm children; they are not directly repairable here
            if btype in ("lvm", "crypt", "raid0", "raid1", "raid5", "dm"):
                return None
            if btype != "disk":
                return None
        path = block.get("path") or f"/dev/{name}"
        bus = (block.get("tran") or "").lower() or None
        sector = _int(block.get("log-sec")) or 512
        device = DeviceRef(
            path=path,
            kind="disk" if btype in ("disk", "rom") else "partition",
            platform="linux",
            size_bytes=size,
            sector_size=sector,
            bus=bus,
            removable=_bool(block.get("rm")),
            read_only=_bool(block.get("ro")),
            vendor=_clean(block.get("vendor")),
            model=_clean(block.get("model")),
            serial=_clean(block.get("serial")),
            mountpoints=_mountpoints(block),
            filesystem=_clean(block.get("fstype")),
            label=_clean(block.get("label")),
            is_system_disk=name in system_disks or any(m in _SYSTEM_MOUNTS for m in _mountpoints(block)),
        )
        if not device.vendor and not device.model:
            sys_vendor = _sysfs_text(name, "device/vendor") or _sysfs_text(name, "device/../vendor")
            sys_model = _sysfs_text(name, "device/model")
            if sys_vendor:
                device.vendor = sys_vendor
            if sys_model:
                device.model = sys_model
        device.native["lsblk"] = {
            k: v for k, v in block.items() if k not in ("children",) and v is not None
        }
        if size:
            device.native["capacity"] = human_size(size)

        for child in block.get("children") or []:
            sub = self._from_lsblk(child, system_disks, warnings, parent=device)
            if sub is None:
                continue
            device.partitions.append(
                Partition(
                    index=len(device.partitions) + 1,
                    path=sub.path,
                    offset_bytes=0,  # filled by _enrich from /sys start sector
                    size_bytes=sub.size_bytes or 0,
                    scheme=block.get("pttype"),
                    type_id=child.get("parttype"),
                    filesystem=sub.filesystem,
                    label=sub.label,
                    uuid=_clean(child.get("uuid")),
                    mounted_at=", ".join(sub.mountpoints) or None,
                    read_only=sub.read_only,
                )
            )
            device.native.setdefault("children", []).append(
                {**sub.native.get("lsblk", {}), "path": sub.path}
            )
        return device

    # ------------------------------------------------------------------ sysfs
    def _via_sysfs(self, warnings: list[str]) -> list[DeviceRef]:
        devices: list[DeviceRef] = []
        system_disks = _system_disk_names()
        for entry in sorted(glob.glob("/sys/class/block/*")):
            name = os.path.basename(entry)
            if name.startswith(("loop", "ram", "zram")):
                continue
            if os.path.exists(os.path.join(entry, "partition")):
                continue  # partitions are attached to their parent below
            size_bytes = _sysfs_int(name, "size")
            device = DeviceRef(
                path=f"/dev/{name}",
                kind="disk",
                platform="linux",
                size_bytes=(size_bytes or 0) * 512,
                sector_size=_sysfs_int(name, "queue/logical_block_size") or 512,
                removable=bool(_sysfs_int(name, "removable")),
                read_only=bool(_sysfs_int(name, "ro")),
                is_system_disk=name in system_disks,
            )
            devices.append(device)
            for part_dir in sorted(glob.glob(f"/sys/class/block/{name}/{name}*")):
                part_name = os.path.basename(part_dir)
                if not os.path.exists(os.path.join(part_dir, "partition")):
                    continue
                device.partitions.append(
                    Partition(
                        index=_sysfs_int(part_name, "partition") or 0,
                        path=f"/dev/{part_name}",
                        offset_bytes=(_sysfs_int(part_name, "start") or 0) * 512,
                        size_bytes=(_sysfs_int(part_name, "size") or 0) * 512,
                    )
                )
        return devices

    # ----------------------------------------------------------------- enrich
    def _enrich(self, device: DeviceRef, warnings: list[str]) -> None:
        name = os.path.basename(device.path)
        sysdir = _sysfs_dir(name)
        if not sysdir:
            if device.bus is None:
                device.bus = "virtual" if name.startswith(("vd", "dm", "md")) else "unknown"
            return
        # bus / transport ---------------------------------------------------
        # Derived from where the kernel placed the device in the sysfs tree.
        # Anything that only *looks* like a name (e.g. /dev/vda on a VM) is
        # explicitly NOT assumed to be SCSI/ATA.
        real = os.path.realpath(sysdir)
        if device.bus is None:
            if "/usb" in real:
                device.bus = "usb"
            elif "/nvme" in real or name.startswith("nvme"):
                device.bus = "nvme"
            elif "/mmc" in real or name.startswith("mmcblk"):
                device.bus = "mmc"
            elif "/ata" in real or "/scsi" in real:
                device.bus = "sata" if "/ata" in real else "scsi"
            elif "virtio" in real or name.startswith("vd"):
                device.bus = "virtual"
            else:
                device.bus = "unknown"

        # identity ----------------------------------------------------------
        device.vendor = device.vendor or _clean(_sysfs_text_multi(name, "device/vendor"))
        device.model = device.model or _clean(_sysfs_text_multi(name, "device/model"))
        device.serial = device.serial or _clean(_sysfs_text_multi(name, "device/serial"))
        device.firmware = device.firmware or _clean(_sysfs_text_multi(name, "device/rev"))
        if device.kind == "disk" and device.firmware is None:
            device.firmware = _clean(_sysfs_text_multi(name, "device/firmware_rev"))

        # USB descriptors ---------------------------------------------------
        usb_dev_dir = _find_usb_device_dir(real)
        if usb_dev_dir:
            vid = _hexfile(os.path.join(usb_dev_dir, "idVendor"))
            pid = _hexfile(os.path.join(usb_dev_dir, "idProduct"))
            if vid is not None:
                device.vid = f"{vid:04X}"
                if not device.vendor:
                    device.vendor = _clean(_read_text(os.path.join(usb_dev_dir, "manufacturer")))
            if pid is not None:
                device.pid = f"{pid:04X}"
                if not device.model:
                    device.model = _clean(_read_text(os.path.join(usb_dev_dir, "product")))
            speed = _read_text(os.path.join(usb_dev_dir, "speed"))
            if speed:
                try:
                    mbps = float(speed)
                    device.usb_speed = _speed_label(mbps)
                except ValueError:
                    device.usb_speed = speed
            if not device.serial:
                device.serial = _clean(_read_text(os.path.join(usb_dev_dir, "serial")))
            vendor_name = vendor_from_vid(vid)
            if vendor_name and not device.vendor:
                device.vendor = vendor_name
            device.native["usb"] = {
                "vid": device.vid,
                "pid": device.pid,
                "speed_mbps": speed,
                "manufacturer": _clean(_read_text(os.path.join(usb_dev_dir, "manufacturer"))),
                "product": _clean(_read_text(os.path.join(usb_dev_dir, "product"))),
                "bcdDevice": _read_text(os.path.join(usb_dev_dir, "bcdDevice")),
                "maxPower_mA": _read_text(os.path.join(usb_dev_dir, "bMaxPower")),
                "busnum": _read_text(os.path.join(usb_dev_dir, "busnum")),
                "devnum": _read_text(os.path.join(usb_dev_dir, "devnum")),
            }

        # partition offsets --------------------------------------------------
        for part in device.partitions:
            if part.offset_bytes:
                continue
            pname = os.path.basename(part.path or "")
            start = _sysfs_int(pname, "start")
            if start:
                part.offset_bytes = start * 512
            if part.filesystem is None:
                part.filesystem = _blkid_field(part.path or "", "TYPE")
            if part.label is None:
                part.label = _blkid_field(part.path or "", "LABEL")
            if part.uuid is None:
                part.uuid = _blkid_field(part.path or "", "UUID")

        # write protection ----------------------------------------------------
        ro = _sysfs_int(name, "ro")
        if ro:
            device.read_only = True
        device.native["sysfs"] = {
            "dir": sysdir,
            "queue_rotational": _sysfs_text_multi(name, "queue/rotational"),
            "queue_max_sectors_kb": _sysfs_text_multi(name, "queue/max_sectors_kb"),
            "device_state": _sysfs_text_multi(name, "device/state"),
            "timeout": _sysfs_text_multi(name, "device/timeout"),
        }
        if not device.size_bytes:
            sectors = _sysfs_int(name, "size")
            if sectors:
                device.size_bytes = sectors * 512
        self._enrich_mmc(device, name)

    def _enrich_mmc(self, device: DeviceRef, name: str) -> None:
        """SD cards expose CID/CSD via sysfs on many kernels."""
        base = f"/sys/class/block/{name}/device"
        cid = _read_text(os.path.join(base, "cid")) or _read_text(f"/sys/block/{name}/device/cid")
        if cid:
            device.native["mmc"] = {"cid": cid.strip()}
            if not device.serial:
                # the CID holds a 32-bit serial in bytes 9..12 of the response
                device.serial = cid.strip()[-16:] or None
        manufacturer = _read_text(os.path.join(base, "manfid"))
        if manufacturer:
            device.native.setdefault("mmc", {})["manufacturer_id"] = manufacturer.strip()
        device.bus = device.bus or "mmc"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _mountpoints(block: dict[str, Any]) -> list[str]:
    """Normalise lsblk 2.37+ ``mountpoints`` and legacy ``mountpoint``."""
    points = [m for m in (block.get("mountpoints") or []) if m]
    singular = block.get("mountpoint")
    if singular and singular not in points:
        points.append(singular)
    return points


def _int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes"):
        return True
    if text in ("0", "false", "no"):
        return False
    return None


def _clean(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip().strip("\x00")
    return text or None


def _read_text(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return None


def _hexfile(path: str) -> Optional[int]:
    text = _read_text(path)
    if not text:
        return None
    try:
        return int(text, 16)
    except ValueError:
        return None


def _sysfs_dir(name: str) -> Optional[str]:
    for candidate in (f"/sys/class/block/{name}", f"/sys/block/{name}"):
        if os.path.isdir(candidate):
            return candidate
    return None


def _sysfs_text(name: str, rel: str) -> Optional[str]:
    directory = _sysfs_dir(name)
    if not directory:
        return None
    value = _read_text(os.path.join(directory, rel))
    return value.strip() if value else None


def _sysfs_text_multi(name: str, rel: str) -> Optional[str]:
    """Read a sysfs attribute, trying the classic and modern kernel layouts.

    Block-device attributes moved over time (e.g. ``/sys/block/sdb/device/model``
    vs ``/sys/class/block/sdb/model``) and NVMe keeps them under ``device/``,
    so both spellings are probed.
    """
    directory = _sysfs_dir(name)
    if not directory:
        return None
    stripped = rel[len("device/") :] if rel.startswith("device/") else rel
    candidates = [
        os.path.join(directory, rel),
        os.path.join(directory, "device", stripped),
        os.path.join(directory, stripped),
        os.path.join(directory, "device", "device", stripped),
    ]
    for candidate in candidates:
        value = _read_text(candidate)
        if value:
            return value.strip()
    return None


def _sysfs_int(name: str, rel: str) -> Optional[int]:
    text = _sysfs_text(name, rel)
    if text is None:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _find_usb_device_dir(sysfs_real: str) -> Optional[str]:
    """Walk up the device tree to the USB device node (holds VID/PID/serial)."""
    current = sysfs_real
    for _ in range(8):
        if os.path.exists(os.path.join(current, "idVendor")):
            return current
        parent = os.path.dirname(current)
        if parent == current or not parent.startswith("/sys"):
            return None
        current = parent
    return None


def _speed_label(mbps: float) -> str:
    if mbps >= 20000:
        return "USB 3.2 Gen 2x2 (20 Gbps)"
    if mbps >= 10000:
        return "USB 3.2 Gen 2 (10 Gbps)"
    if mbps >= 5000:
        return "USB 3.2 Gen 1 / USB 3.0 (5 Gbps)"
    if mbps >= 480:
        return "USB 2.0 High Speed (480 Mbps)"
    if mbps >= 12:
        return "USB 1.1 Full Speed (12 Mbps)"
    return f"{mbps:g} Mbps"


def _system_disk_names() -> set[str]:
    """Kernel names of the disk(s) that host the running OS."""
    names: set[str] = set()
    for target in ("/", "/boot", "/boot/efi"):
        try:
            st = os.stat(target)
        except OSError:
            continue
        major = os.major(st.st_dev)
        minor = os.minor(st.st_dev)
        link = f"/sys/dev/block/{major}:{minor}"
        if not os.path.exists(link):
            continue
        real = os.path.realpath(link)
        name = os.path.basename(real)
        parent = os.path.dirname(real)
        if os.path.exists(os.path.join(parent, "partition")):
            name = os.path.basename(parent)
        names.add(name)
    return names


def _blkid_field(device: str, field: str) -> Optional[str]:
    """Optional blkid lookup used only to complete partition labels."""
    exe = find_tool("blkid")
    if not exe or not device:
        return None
    try:
        proc = run_process([exe, "-s", field, "-o", "value", device], timeout=8)
    except Exception:
        return None
    if not proc.ok:
        return None
    value = proc.stdout.strip()
    return value or None


def diagnose_autosuspend(device: DeviceRef) -> list[str]:
    """USB power-management settings that cause random disconnects."""
    notes: list[str] = []
    name = os.path.basename(device.path)
    directory = _sysfs_dir(name)
    if not directory:
        return notes
    control = _read_text(os.path.join(directory, "device", "power", "control"))
    if control == "auto":
        notes.append(
            "USB autosuspend is enabled for this device ('auto'), which can cause intermittent "
            "disconnects. Consider 'on' for troubleshooting."
        )
    return notes
