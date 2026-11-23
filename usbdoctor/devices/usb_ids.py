"""USB vendor/controller knowledge used to *hedge* diagnoses honestly.

The tables below are curated from public USB-IF vendor assignments and from the
controller chips commonly found in counterfeit / mass-produced flash drives.
They are used only to add context ("this VID/PID identifies a Phison controller
bridge") — never to fabricate a manufacturer when the OS reports none.
"""

from __future__ import annotations

from typing import Optional

#: USB-IF vendor IDs commonly seen on storage devices.
USB_VENDORS: dict[int, str] = {
    0x0781: "SanDisk",
    0x0951: "Kingston Technology",
    0x090C: "Silicon Motion / Feiya",
    0x13FE: "Phison Electronics",
    0x048D: "ITE Tech",
    0x058F: "Alcor Micro",
    0x1307: "Transcend",
    0x8564: "Transcend",
    0x0EA0: "Trek / OTi",
    0x14CD: "Super Top (USB bridge)",
    0x05E3: "Genesys Logic (USB bridge)",
    0x152D: "JMicron Technology",
    0x174C: "ASMedia Technology",
    0x2109: "VIA Labs",
    0x0BC2: "Seagate",
    0x1058: "Western Digital",
    0x04E8: "Samsung Electronics",
    0x0930: "Toshiba",
    0x1B1C: "Corsair",
    0x11B0: "ATECH Flash / Verbatim",
    0x0718: "Imation",
    0x125F: "A-DATA Technology",
    0x18A5: "Verbatim",
    0x1F75: "Innostor",
    0x13FD: "Initio",
    0x067B: "Prolific Technology",
    0x1B8F: "TwinMOS / Chipsbank",
    0x0204: "Chipsbank",
    0x0C45: "Sonix",
    0x2537: "Norelsys",
    0x1976: "Chrontel",
    0x1E3D: "Chipsbank (CBM)",
}

#: Bridge/controller families, keyed by VID:PID (uppercase hex strings).
KNOWN_CONTROLLERS: dict[str, dict[str, str]] = {
    "13FE:1A00": {"chip": "Phison PS2251-03", "notes": "Mode-switch controller; MPALL/Phison tools exist but "
                                                        "flashing is model-specific and risky."},
    "13FE:1D00": {"chip": "Phison PS2251-68", "notes": "Common on budget 32-64 GB drives."},
    "13FE:4100": {"chip": "Phison PS2251-67", "notes": "Frequently re-branded by several vendors."},
    "0951:1666": {"chip": "Kingston DataTraveler", "notes": "Kingston-branded SM/Phison bridge."},
    "090C:1000": {"chip": "Silicon Motion SM3267", "notes": "SM32x controllers can report false capacity."},
    "090C:2000": {"chip": "Silicon Motion SM3268", "notes": "Common in low-cost drives."},
    "1F75:0916": {"chip": "Innostor IS917", "notes": "Another mode-switch family."},
    "058F:6387": {"chip": "Alcor Micro AU6989", "notes": "Mode-switch controller; AlcorMP tool family."},
    "1E3D:198A": {"chip": "Chipsbank CBM2099", "notes": "Chipsbank tool family."},
    "0204:6025": {"chip": "Chipsbank CBM2098", "notes": "Chipsbank tool family."},
    "174C:55AA": {"chip": "ASMedia ASM2362/ASM1153 bridge", "notes": "External SATA/NVMe enclosure bridge; "
                                                                    "SMART may pass through with -d sat."},
    "152D:0583": {"chip": "JMicron JMS583 bridge", "notes": "NVMe enclosure bridge; SMART often available with -d sntjmicron."},
    "1058:25EE": {"chip": "Western Digital My Passport", "notes": "WD external drive; WD utilities available "
                                                                  "for firmware/health."},
    "0BC2:231A": {"chip": "Seagate Expansion", "notes": "Seagate external drive; SeaTools available."},
    "04E8:4001": {"chip": "Samsung external SSD", "notes": "Samsung Magician applicable on supported models."},
}

#: VID/PID pairs frequently used by counterfeit-capacity drives. Not proof.
COUNTERFEIT_PRONE_PIDS: set[str] = {
    "13FE:1F00",
    "13FE:1E00",
    "0951:1665",
    "090C:1000",
    "1F75:0917",
}


def vendor_from_vid(vid: Optional[int]) -> Optional[str]:
    if vid is None:
        return None
    return USB_VENDORS.get(vid)


def controller_hint(vid: Optional[int], pid: Optional[int]) -> Optional[dict[str, str]]:
    if vid is None or pid is None:
        return None
    key = f"{vid:04X}:{pid:04X}"
    return KNOWN_CONTROLLERS.get(key)


def counterfeit_prone(vid: Optional[int], pid: Optional[int]) -> bool:
    if vid is None or pid is None:
        return False
    return f"{vid:04X}:{pid:04X}" in COUNTERFEIT_PRONE_PIDS


#: USB speed enums (USB 2.0 / 3.x) as reported by sysfs, IOKit and WMI.
USB_SPEEDS: dict[int, str] = {
    1: "USB 1.1 Low Speed (1.5 Mbps)",
    2: "USB 1.1 Full Speed (12 Mbps)",
    3: "USB 2.0 High Speed (480 Mbps)",
    4: "USB 3.0 SuperSpeed (5 Gbps)",
    5: "USB 3.1 SuperSpeed+ (10 Gbps)",
    6: "USB 3.2 SuperSpeed+ (20 Gbps)",
    0: "Unknown / not reported",
}
