"""SMART analysis namespace."""

from usbdoctor.analysis.smart.reader import (
    ATA_CRITICAL_ATTRS,
    NVME_CRITICAL_WARNING,
    SmartAttribute,
    SmartResult,
    read_smart,
    read_smart_linux_sysfs,
    read_smart_macos,
    read_smart_smartctl,
    read_smart_windows,
    smart_attribute_issues,
)

__all__ = [
    "ATA_CRITICAL_ATTRS",
    "NVME_CRITICAL_WARNING",
    "SmartAttribute",
    "SmartResult",
    "read_smart",
    "read_smart_linux_sysfs",
    "read_smart_macos",
    "read_smart_smartctl",
    "read_smart_windows",
    "smart_attribute_issues",
]
