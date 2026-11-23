"""Device reference model shared by every engine component."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class Partition:
    """One partition / slice / volume as reported by the OS or parsed metadata."""

    index: int
    path: Optional[str]  # e.g. /dev/sdb1, \\\\.\\PhysicalDrive2 (partition views), disk4s1
    offset_bytes: int
    size_bytes: int
    scheme: Optional[str] = None  # mbr | gpt | apm | none
    type_id: Optional[str] = None  # MBR type byte / GPT type GUID
    type_label: Optional[str] = None
    filesystem: Optional[str] = None  # as reported by OS
    label: Optional[str] = None
    uuid: Optional[str] = None
    mounted_at: Optional[str] = None
    read_only: Optional[bool] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "path": self.path,
            "offset_bytes": self.offset_bytes,
            "size_bytes": self.size_bytes,
            "scheme": self.scheme,
            "type_id": self.type_id,
            "type_label": self.type_label,
            "filesystem": self.filesystem,
            "label": self.label,
            "uuid": self.uuid,
            "mounted_at": self.mounted_at,
            "read_only": self.read_only,
        }


@dataclass
class DeviceRef:
    """Everything we know about a block device before scanning it.

    Instances are cheap snapshots; the ``native`` dict keeps the untouched OS
    record (WMI object, IORegistry dict, udev properties, lsblk JSON) so expert
    views and text reports can show the evidence behind each conclusion.
    """

    path: str
    kind: str = "disk"  # disk | partition | image
    platform: str = "linux"
    size_bytes: Optional[int] = None
    sector_size: int = 512
    bus: Optional[str] = None  # usb | sata | nvme | mmc | virtual | scsi | unknown
    removable: Optional[bool] = None
    read_only: Optional[bool] = None
    vendor: Optional[str] = None
    model: Optional[str] = None
    serial: Optional[str] = None
    firmware: Optional[str] = None
    vid: Optional[str] = None
    pid: Optional[str] = None
    usb_speed: Optional[str] = None  # mapped enum, e.g. "USB 3.2 Gen 1 (5 Gbps)"
    mountpoints: list[str] = field(default_factory=list)
    filesystem: Optional[str] = None
    label: Optional[str] = None
    partitions: list[Partition] = field(default_factory=list)
    is_system_disk: bool = False
    temperature_c: Optional[float] = None
    health_hint: Optional[str] = None
    native: dict[str, Any] = field(default_factory=dict)
    limitations: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ utils
    @property
    def display_name(self) -> str:
        brand = (self.vendor or "").strip() or (self.model or "").strip() or "Unknown device"
        model = (self.model or "").strip()
        if model and brand.lower() not in model.lower():
            return f"{brand} {model}".strip()
        return brand

    @property
    def is_usb(self) -> bool:
        return (self.bus or "").lower() == "usb"

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "platform": self.platform,
            "size_bytes": self.size_bytes,
            "sector_size": self.sector_size,
            "bus": self.bus,
            "removable": self.removable,
            "read_only": self.read_only,
            "vendor": self.vendor,
            "model": self.model,
            "serial": self.serial,
            "firmware": self.firmware,
            "vid": self.vid,
            "pid": self.pid,
            "usb_speed": self.usb_speed,
            "mountpoints": self.mountpoints,
            "filesystem": self.filesystem,
            "label": self.label,
            "partitions": [p.to_dict() for p in self.partitions],
            "is_system_disk": self.is_system_disk,
            "temperature_c": self.temperature_c,
            "health_hint": self.health_hint,
            "display_name": self.display_name,
            "limitations": self.limitations,
        }


@dataclass(frozen=True)
class DeviceSignature:
    """Fingerprint used to prove we are still talking to the same device.

    Recalculated immediately before any write; if it differs from the signature
    captured during the scan, the write is refused.
    """

    path: str
    size_bytes: Optional[int]
    head_sha256: Optional[str]
    tail_sha256: Optional[str]
    disk_signature: Optional[str] = None
    serial_hash: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "size_bytes": self.size_bytes,
            "head_sha256": self.head_sha256,
            "tail_sha256": self.tail_sha256,
            "disk_signature": self.disk_signature,
            "serial_hash": self.serial_hash,
        }

    def matches(self, other: "DeviceSignature") -> bool:
        return (
            self.path == other.path
            and self.size_bytes == other.size_bytes
            and self.head_sha256 == other.head_sha256
            and self.tail_sha256 == other.tail_sha256
        )
