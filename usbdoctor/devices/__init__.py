"""Hardware enumeration and identity providers."""

from usbdoctor.devices.base import DeviceProvider, ProviderResult, enumerate_devices
from usbdoctor.devices.usb_ids import (
    KNOWN_CONTROLLERS,
    vendor_from_vid,
    controller_hint,
)

__all__ = [
    "DeviceProvider",
    "ProviderResult",
    "enumerate_devices",
    "KNOWN_CONTROLLERS",
    "vendor_from_vid",
    "controller_hint",
]
