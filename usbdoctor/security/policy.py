"""Device protection policy: what may be written, and under which conditions."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Iterable, Optional

from usbdoctor.core.errors import UnsafeOperationError
from usbdoctor.core.model import DeviceRef
from usbdoctor.core.platform import detect_system


@dataclass
class PolicySettings:
    #: Writing to fixed (non-removable) internal disks is off by default.
    allow_internal_disks: bool = False
    #: Allow operations while the target volume is mounted (only imaging/logical
    #: repairs that explicitly support it consult this flag).
    allow_mounted_write: bool = False
    #: Extra device paths the user explicitly whitelisted for modification.
    trusted_paths: list[str] = field(default_factory=list)
    #: Paths that must never be written to, no matter what.
    protected_paths: list[str] = field(default_factory=list)


class SafetyPolicy:
    def __init__(self, settings: Optional[PolicySettings] = None) -> None:
        self.settings = settings or PolicySettings()
        self._system_disks = _discover_system_disks()

    # ------------------------------------------------------------------ facts
    @property
    def system_disks(self) -> set[str]:
        return set(self._system_disks)

    def is_system_disk(self, device: DeviceRef) -> bool:
        if device.is_system_disk:
            return True
        return os.path.basename(device.path) in {os.path.basename(p) for p in self._system_disks}

    def refresh(self) -> None:
        self._system_disks = _discover_system_disks()

    # ----------------------------------------------------------------- checks
    def check_writable(self, device: DeviceRef, operation: str) -> None:
        """Raise :class:`UnsafeOperationError` unless writing is permitted."""
        if device.kind == "image":
            # Images are plain files; still forbid writing into the OS tree.
            real = os.path.realpath(device.path)
            if real.startswith(self._critical_prefixes()):
                raise UnsafeOperationError(
                    f"Refusing to modify image inside system directories: {device.path}"
                )
            return

        if device.path in self.settings.protected_paths:
            raise UnsafeOperationError(
                f"Device {device.path} is on the protected list.",
                hint="Remove it in Settings → Safety if this is really intended.",
            )

        if self.is_system_disk(device):
            raise UnsafeOperationError(
                f"{operation} would modify the disk that contains your operating system.",
                hint="USB Doctor AI never modifies the system disk. Use a different machine/VM.",
                device=device.path,
            )

        if device.kind != "disk" and device.kind != "partition":
            raise UnsafeOperationError(f"Unsupported device kind: {device.kind}")

        if device.read_only:
            raise UnsafeOperationError(
                "The operating system reports this device as read-only.",
                hint=(
                    "Check the write-protection diagnosis: the lock may be a physical "
                    "switch, a controller-level lock, or a policy setting."
                ),
                device=device.path,
            )

        if device.bus not in ("usb", "mmc", "image", None):
            if not self.settings.allow_internal_disks:
                raise UnsafeOperationError(
                    f"{operation} targets an internal ({device.bus or 'unknown bus'}) drive.",
                    hint=(
                        "Internal-drive modification is disabled by default. "
                        "Enable 'Allow internal disks' in Settings → Safety."
                    ),
                    device=device.path,
                )

        if device.mountpoints and not self.settings.allow_mounted_write:
            raise UnsafeOperationError(
                f"Volume is currently mounted at {', '.join(device.mountpoints)}.",
                hint="Unmount / eject the volume first, then retry the operation.",
                device=device.path,
            )

    def describe(self) -> dict:
        return {
            "system_disks": sorted(self._system_disks),
            "allow_internal_disks": self.settings.allow_internal_disks,
            "allow_mounted_write": self.settings.allow_mounted_write,
            "protected_paths": list(self.settings.protected_paths),
        }

    @staticmethod
    def _critical_prefixes() -> tuple[str, ...]:
        if detect_system() == "windows":
            return (os.environ.get("SystemRoot", r"C:\Windows").lower(),)
        return ("/dev", "/proc", "/sys", "/boot", "/etc", "/usr", "/bin", "/sbin")


def _discover_system_disks() -> set[str]:
    """Identify the disk(s) backing the running OS, per platform."""
    found: set[str] = set()
    system = detect_system()
    try:
        if system == "windows":
            drive = os.environ.get("SystemDrive", "C:")
            found.add(drive.rstrip("\\"))
            # Physical drive number via IOCTL would need elevation; the drive
            # letter mapping is sufficient because policy also blocks the
            # boot volume by mountpoint.
        elif system == "macos":
            found.add("/dev/disk0")
        else:
            for target in ("/", "/boot", "/boot/efi"):
                try:
                    st = os.stat(target)
                    dev = os.major(st.st_dev)
                    name = _linux_disk_name_for_dev(dev)
                    if name:
                        found.add(f"/dev/{name}")
                except OSError:
                    continue
    except Exception:  # pragma: no cover - defensive; policy must never crash
        pass
    return found


def _linux_disk_name_for_dev(major_dev: int) -> Optional[str]:
    """Map a Linux major device number to its parent whole-disk name."""
    try:
        for entry in os.listdir("/sys/dev/block"):
            if not entry.startswith(f"{major_dev}:"):
                continue
            # /sys/dev/block/MAJ:MIN -> symlink into /sys/devices/...
            link = os.path.realpath(f"/sys/dev/block/{entry}")
            parts = link.split("/")
            # Walk up until we find a directory containing 'partition' absence
            name = os.path.basename(link)
            # If this is a partition (nvme0n1p2, sda1, mmcblk0p1), strip suffix
            parent = os.path.dirname(link)
            if os.path.exists(f"{parent}/partition"):
                name = os.path.basename(parent)
            return name
    except OSError:
        return None
    return None


def classify_bus_to_expected_interface(bus: Optional[str]) -> str:
    return {
        "usb": "USB",
        "mmc": "SD/MMC",
        "sata": "SATA",
        "nvme": "NVMe",
        "scsi": "SCSI/SAS",
        "virtual": "Virtual",
    }.get((bus or "").lower(), "Unknown")
