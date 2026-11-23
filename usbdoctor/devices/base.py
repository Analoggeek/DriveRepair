"""Device enumeration dispatcher.

Each platform provider returns fully-populated :class:`DeviceRef` objects sourced
from native APIs.  A provider *must* raise rather than return invented data; the
dispatcher then surfaces the error to the UI instead of silently showing
"Unknown device".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional, Protocol

from usbdoctor.core.errors import PlatformUnsupportedError, UsbDoctorError
from usbdoctor.core.model import DeviceRef
from usbdoctor.core.platform import detect_system

LOG = logging.getLogger(__name__)


@dataclass
class ProviderResult:
    devices: list[DeviceRef] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    source: str = ""
    requires_admin: bool = False


class DeviceProvider(Protocol):
    name: str

    def list_devices(self, *, include_internal: bool = True) -> ProviderResult: ...


def _provider_for_system(system: str):
    if system == "windows":
        from usbdoctor.devices.windows import WindowsDeviceProvider

        return WindowsDeviceProvider()
    if system == "macos":
        from usbdoctor.devices.macos import MacDeviceProvider

        return MacDeviceProvider()
    if system == "linux":
        from usbdoctor.devices.linux import LinuxDeviceProvider

        return LinuxDeviceProvider()
    raise PlatformUnsupportedError(
        f"Storage enumeration is not implemented for platform '{system}'.",
        hint="USB Doctor AI supports Windows 10/11, macOS (Intel + Apple Silicon) and Linux.",
    )


def enumerate_devices(
    *,
    include_internal: bool = True,
    include_loop: bool = False,
    provider: Optional[DeviceProvider] = None,
) -> ProviderResult:
    """Return every storage device the OS currently reports."""
    system = detect_system()
    try:
        impl = provider or _provider_for_system(system)
        result = impl.list_devices(include_internal=include_internal)
    except UsbDoctorError:
        raise
    except Exception as exc:  # a provider crash must not take the app down
        LOG.exception("device enumeration failed")
        raise UsbDoctorError(
            f"Storage enumeration failed on this system: {exc}",
            hint="Check the log for details, or try running with administrator privileges.",
        ) from exc

    if not include_loop:
        # Loop devices, RAM disks and container-mapped volumes are not storage
        # the user can repair; keep them out of the list but never hide a
        # *physical* disk, even when it is virtualised (the safety policy marks
        # the system disk and refuses writes to it).
        result.devices = [d for d in result.devices if not _is_container_noise(d)]
    result.devices.sort(key=lambda d: (not d.is_usb, d.kind != "disk", d.path))
    return result


def _is_container_noise(device: DeviceRef) -> bool:
    """True for pseudo-devices that must never be offered as repair targets."""
    if device.kind == "image":
        return False
    path = device.path.lower()
    if "/dev/loop" in path or "/dev/ram" in path or "/dev/zram" in path:
        return True
    if "virtualdisk" in path:
        return True
    return False


def get_device(path: str) -> Optional[DeviceRef]:
    """Look up a single device by path among currently connected devices."""
    result = enumerate_devices(include_internal=True)
    for device in result.devices:
        if device.path == path:
            return device
    return None
