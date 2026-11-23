"""Offline repair knowledge base.

Curated from vendor documentation, and structured so that every entry states
*when it applies*, *what to do*, and — critically — *whether software can help at
all*.  Entries are matched against real evidence: error codes, symptoms, volume
file systems, VID/PID families and model names.

This database ships offline so the app is useful with no network.  Online
research (:mod:`usbdoctor.knowledge.online`) augments it with official sources,
and anything it returns is labelled with its source URL for verification.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

SEVERITY_ORDER = {"info": 0, "warning": 1, "error": 2, "critical": 3}


@dataclass
class KnowledgeEntry:
    """One known problem/solution pair."""

    key: str
    title: str
    symptoms: list[str]
    likely_causes: list[str]
    software_fixable: bool
    steps: list[str]
    verification: list[str]
    severity: str = "warning"
    brands: list[str] = field(default_factory=list)
    filesystems: list[str] = field(default_factory=list)
    error_codes: list[str] = field(default_factory=list)
    vids: list[str] = field(default_factory=list)
    official_tools: list[dict[str, str]] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    when_not_to: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "title": self.title,
            "symptoms": self.symptoms,
            "likely_causes": self.likely_causes,
            "software_fixable": self.software_fixable,
            "steps": self.steps,
            "verification": self.verification,
            "severity": self.severity,
            "brands": self.brands,
            "filesystems": self.filesystems,
            "error_codes": self.error_codes,
            "vids": self.vids,
            "official_tools": self.official_tools,
            "sources": self.sources,
            "when_not_to": self.when_not_to,
        }

    def matches(
        self,
        *,
        error_codes: Iterable[str] = (),
        filesystem: Optional[str] = None,
        brand: Optional[str] = None,
        vid: Optional[str] = None,
        symptom_text: str = "",
    ) -> int:
        """Score relevance (0 = no match). Higher is more specific."""
        score = 0
        codes = {c.upper() for c in error_codes}
        matched_codes = codes & {c.upper() for c in self.error_codes}
        if matched_codes:
            # Weight by how *specific* the entry is: an entry that names one
            # exact code is stronger evidence than an umbrella entry that lists
            # a dozen codes "just in case".
            breadth = max(1, len(self.error_codes))
            score += round(40 * len(matched_codes) / breadth) + 10
        if filesystem and filesystem.lower() in [f.lower() for f in self.filesystems]:
            score += 15
        if brand and brand.lower() in [b.lower() for b in self.brands]:
            score += 10
        if vid and vid.upper() in [v.upper() for v in self.vids]:
            score += 10
        if symptom_text:
            haystack = (symptom_text + " " + " ".join(self.symptoms)).lower()
            hits = sum(
                1
                for token in re.findall(r"[a-z]{4,}", symptom_text.lower())
                if token in haystack
            )
            score += min(12, hits)
        return score


ENTRIES: tuple[KnowledgeEntry, ...] = (
    # ------------------------------------------------------------------ RAW
    KnowledgeEntry(
        key="volume-raw",
        title="Drive shows as RAW / unreadable after unsafe removal",
        symptoms=["Windows asks to format the disk", "drive shows 0 bytes or RAW file system",
                  "volume does not mount", "chkdsk says 'cannot open volume for direct access'"],
        likely_causes=[
            "Partition table or boot sector was overwritten during an unsafe unplug",
            "USB bridge returned a partially written metadata block",
            "Flash translation layer lost its mapping after a power interruption",
        ],
        software_fixable=True,
        steps=[
            "Do NOT format: formatting writes new metadata over the recoverable one.",
            "Create a full image of the device first (Data Recovery → Image device).",
            "Run the read-only scan so the tool can identify the real file system from backups.",
            "Apply the matching boot-sector repair (Safe/Smart mode plans it automatically).",
            "If no backup boot sector exists, recover files by carving from the image.",
        ],
        verification=[
            "Re-scan shows the original file system (FAT32/exFAT/NTFS) instead of RAW.",
            "Files are listed with their original names and sizes.",
        ],
        severity="error",
        error_codes=["FS_UNKNOWN", "MBR_SIGNATURE_MISSING", "FAT_BOOT_SECTOR_DESTROYED",
                     "EXFAT_BOOT_REGION_DESTROYED", "NTFS_BOOT_SIGNATURE_MISSING"],
        sources=["https://learn.microsoft.com/windows-server/administration/windows-commands/chkdsk"],
    ),
    KnowledgeEntry(
        key="fat32-backup-boot",
        title="FAT32 boot sector destroyed but backup intact",
        symptoms=["volume is RAW", "chkdsk cannot read the boot sector", "file list is empty"],
        likely_causes=["Primary boot sector overwritten", "bad sector at LBA 0"],
        software_fixable=True,
        steps=[
            "The FAT32 specification stores a complete backup boot sector at sector 6.",
            "Copy sectors 6-11 over sector 0 and re-read the volume.",
            "Run the FAT mirror check afterwards, because both FATs must still agree.",
        ],
        verification=["Boot checksum/signature valid", "Directory entries list original files"],
        severity="error",
        filesystems=["fat32"],
        error_codes=["FAT_BOOT_SECTOR_DESTROYED", "FAT32_BACKUP_REQUIRED"],
        sources=["https://learn.microsoft.com/windows/win32/fileio/fat32-file-system-specification"],
    ),
    KnowledgeEntry(
        key="exfat-boot-checksum",
        title="exFAT boot region checksum mismatch",
        symptoms=["card works on some devices but not others", "mount failures after removing during a copy"],
        likely_causes=[
            "Boot region was partially overwritten (power loss or unsafe eject)",
            "Weaker symptom of failing flash at the start of the card",
        ],
        software_fixable=True,
        steps=[
            "Verify the backup boot region (sectors 12-23).",
            "Recompute the 32-bit boot checksum over sectors 0-10.",
            "If the backup verifies, restore the primary from it.",
        ],
        verification=["Recalculated checksum equals the stored value", "Volume mounts read-only without errors"],
        severity="error",
        filesystems=["exfat"],
        error_codes=["EXFAT_BOOT_CHECKSUM", "EXFAT_BOOT_REGION_DESTROYED"],
        sources=["https://learn.microsoft.com/windows/win32/fileio/exfat-specification"],
    ),
    KnowledgeEntry(
        key="ntfs-dirty",
        title="NTFS volume marked dirty after unsafe removal",
        symptoms=["Windows runs a check every boot", "volume mounts read-only", "chkdsk scheduled"],
        likely_causes=["$MFT IN_USE flag not cleared", "pending journal replay"],
        software_fixable=True,
        steps=[
            "Back up important files first.",
            "Run 'chkdsk <drive>: /f' (Windows) or 'ntfsfix -d' (macOS/Linux with ntfs-3g).",
            "If chkdsk reports bad clusters, the flash itself is failing — plan a replacement.",
        ],
        verification=["chkdsk reports 0 problems on a second run", "Volume mounts read-write"],
        severity="warning",
        filesystems=["ntfs"],
        error_codes=["NTFS_DIRTY"],
        sources=["https://learn.microsoft.com/windows-server/administration/windows-commands/chkdsk"],
    ),
    # -------------------------------------------------------- write protect
    KnowledgeEntry(
        key="write-protected",
        title="Drive is write-protected and refuses all writes",
        symptoms=["cannot copy files to the drive", "'the disk is write-protected'",
                  "read-only in every OS", "diskpart 'attributes disk' shows Read-only: Yes"],
        likely_causes=[
            "Physical write-protect switch on an SD card or USB enclosure",
            "Windows disk policy flag (Set-Disk -IsReadOnly)",
            "Controller locked itself after detecting failed NAND writes (final failure mode)",
        ],
        software_fixable=True,
        steps=[
            "Check for a physical switch on the card/adaptor first.",
            "Windows: diskpart → 'attributes disk clear readonly'.",
            "Linux: 'blockdev --setrw /dev/sdX' only if /sys/block/<dev>/ro allows it.",
            "If the controller enforces the lock, it is a hardware failure: copy data off and replace.",
        ],
        verification=["A small test file writes and reads back", "/sys/block/<dev>/ro reports 0"],
        severity="warning",
        error_codes=["DEVICE_READ_ONLY", "READ_ONLY_WITH_FAILURE"],
        when_not_to=[
            "Never force-enable writes on a drive whose controller locked itself: every write "
            "accelerates data loss and can destroy the remaining recoverable sectors.",
        ],
        sources=[
            "https://learn.microsoft.com/windows-server/administration/windows-commands/diskpart",
        ],
    ),
    # ------------------------------------------------------------- capacity
    KnowledgeEntry(
        key="counterfeit-capacity",
        title="Counterfeit capacity (fake large USB drive)",
        symptoms=["drive is bigger than what the vendor sells", "files become corrupt after filling",
                  "photos become grey or 'file cannot be opened'", "copying hangs near the end"],
        likely_causes=[
            "Controller firmware reports a fake capacity and wraps writes to the start of real NAND",
            "Recycled or downgraded flash chips",
        ],
        software_fixable=False,
        steps=[
            "Run Performance → Capacity verification to measure the real usable size.",
            "Recover existing files immediately: anything written past the real capacity is already lost.",
            "Do not use the drive for any data you care about.",
            "A low-level re-mapping tool for the specific controller can cap the drive to real capacity, "
            "but it erases everything and is not supported for all controllers.",
        ],
        verification=["Capacity test passes with no read-back mismatches"],
        severity="critical",
        error_codes=["CAPACITY_MISMATCH", "FAT_VOLUME_BEYOND_DEVICE", "EXFAT_VOLUME_BEYOND_DEVICE"],
        sources=["https://www.usb.org/developers"],
    ),
    # --------------------------------------------------------------- memory
    KnowledgeEntry(
        key="flash-wear",
        title="NAND flash wear / bad blocks accumulating",
        symptoms=["SMART reallocated sectors increasing", "write speed collapses after a few GB",
                  "files appear with zero length", "drive becomes read-only under load"],
        likely_causes=["Flash write/erase cycles exhausted", "bad-block pool exhausted"],
        software_fixable=False,
        steps=[
            "Back up everything now; the drive is at the end of its useful life.",
            "Reallocated sector counts that keep growing between scans confirm the trend.",
            "Replace the drive — worn cells cannot be restored by software.",
        ],
        verification=["Reallocated sector count stable or unchanged over time"],
        severity="critical",
        error_codes=["SMART_ATTR_5_HIGH", "SMART_ATTR_197_HIGH", "SMART_SPARE_LOW", "SMART_WEAR_HIGH"],
        sources=["https://www.smartmontools.org/wiki/FAQ"],
    ),
    KnowledgeEntry(
        key="usb-crc-errors",
        title="USB connection errors (CRC/timeout counters rising)",
        symptoms=["drive disappears and reappears", "copy fails with 'The semaphore timeout period has expired'",
                  "SMART attribute 199 (UDMA CRC) increasing"],
        likely_causes=["Faulty cable", "weak USB port or unpowered hub", "loose connector",
                       "USB 3 port negotiating badly"],
        software_fixable=True,
        steps=[
            "Try a different cable and a port directly on the machine (no hub).",
            "Disable USB selective suspend for the device.",
            "On Linux check 'dmesg' for reset/error messages and try 'usbcore.usbfs_memory_mb'.",
            "If CRC counters keep rising with a known-good cable, the bridge or flash is failing.",
        ],
        verification=["No new disconnect events during a 5-minute read test"],
        severity="warning",
        error_codes=["SMART_ATTR_199_ELEVATED", "CONNECTION_UNSTABLE"],
        sources=["https://www.smartmontools.org/wiki/FAQ"],
    ),
    # ------------------------------------------------------------- firmware
    KnowledgeEntry(
        key="phison-mode-switch",
        title="Phison controller stuck in read-only/mode-switch state",
        symptoms=["drive enumerates but reports 0 bytes", "controller in 'boot mode'",
                  "flashing tool cannot see the drive"],
        likely_causes=["Firmware crash after power loss", "mode-switch pin state"],
        software_fixable=False,
        steps=[
            "Only the exact Phison MPALL tool version matching the controller revision can reflash.",
            "Flashing erases the drive completely — recover data first, on another machine if needed.",
            "Cross-flashing a different revision destroys the drive. Do not guess.",
        ],
        verification=["Drive reports its normal capacity again and passes a capacity test"],
        severity="critical",
        error_codes=["ZERO_CAPACITY", "ZERO_WHILE_REPORTING_CAPACITY"],
        vids=["13FE", "0951", "090C"],
        when_not_to=[
            "Never run a controller manufacturing tool found on a random forum: they are targeted, "
            "untested against your exact revision, and often carry malware.",
        ],
        sources=["https://www.usbdev.ru/", "https://www.flashboot.ru/"],
    ),
    KnowledgeEntry(
        key="sdcard-locked",
        title="SD card reports locked or capacity 0",
        symptoms=["card is read-only", "camera says 'card error'", "capacity reported as 0"],
        likely_causes=["Physical lock tab", "controller failure inside the card", "corrupted CID/CSD"],
        software_fixable=False,
        steps=[
            "Slide the lock tab up and re-insert.",
            "Test in another reader: a card that is dead in every reader has failed internally.",
            "If data matters, professional recovery services can sometimes read the NAND directly.",
        ],
        verification=["Card mounts read-write in a second reader"],
        severity="error",
        error_codes=["DEVICE_READ_ONLY", "ZERO_CAPACITY"],
        sources=["https://www.sdcard.org/downloads/pls/"],
    ),
    # -------------------------------------------------------- partitioning
    KnowledgeEntry(
        key="gpt-backup-missing",
        title="GPT backup header missing or corrupt",
        symptoms=["Windows disk management shows the disk as 'not initialized'",
                  "linux reports 'alternate GPT header is invalid'", "gdisk warns about a backup GPT"],
        likely_causes=["The last sectors were overwritten", "partition tool interrupted"],
        software_fixable=True,
        steps=[
            "Read the primary GPT and verify its CRC.",
            "Write the entry array and header to the last 33 sectors with corrected LBAs.",
        ],
        verification=["Both GPT headers parse with matching disk GUIDs"],
        severity="warning",
        error_codes=["GPT_BACKUP_MISSING", "GPT_BACKUP_CRC"],
        sources=["https://en.wikipedia.org/wiki/GUID_Partition_Table"],
    ),
    KnowledgeEntry(
        key="lost-partition-table",
        title="Partition table lost but data still on the drive",
        symptoms=["disk shows as unallocated", "no partitions listed", "single large RAW volume"],
        likely_causes=["MBR/GPT overwritten by a format or imaging tool", "first sectors damaged"],
        software_fixable=True,
        steps=[
            "Search for file-system boot sectors (the analyser probes the standard offsets).",
            "Rebuild the MBR entry pointing at the discovered volume.",
            "Verify by mounting read-only; if the wrong geometry was guessed, undo with the rollback file.",
        ],
        verification=["Partition mounts and lists the original files"],
        severity="error",
        error_codes=["MBR_NO_PARTITIONS", "VOLUME_START_MISMATCH"],
        sources=["https://www.cgsecurity.org/wiki/TestDisk_Step_By_Step"],
    ),
    # ------------------------------------------------------------- surface
    KnowledgeEntry(
        key="unreadable-sectors",
        title="Unreadable sectors spread across the drive",
        symptoms=["scan slows down and speeds up abruptly", "files fail to copy with I/O errors",
                  "drive clicks or stutters during reads"],
        likely_causes=[
            "Worn NAND cells or bad blocks the controller can no longer hide",
            "Physical damage from a drop, heat or a power surge",
        ],
        software_fixable=False,
        steps=[
            "Stop using the drive normally — each retry stresses failing cells further.",
            "Create a full image with retries and skip-ahead (Data Recovery → Image device).",
            "Work only from the image afterwards; recover files from it, never from the drive.",
            "Replace the drive. Software cannot restore physically damaged storage.",
        ],
        verification=["The image completes with a small, finite error count"],
        severity="critical",
        error_codes=["SURFACE_READ_ERRORS", "SURFACE_READ_ERRORS_CONFIRMED", "SECTOR_ZERO_UNREADABLE"],
        sources=["https://www.cgsecurity.org/wiki/Damaged_Hard_Disk"],
    ),
    KnowledgeEntry(
        key="filesystem-metadata",
        title="File-system metadata damaged but files intact",
        symptoms=["directory listing is empty or garbled", "file names show as ????",
                  "some folders refuse to open"],
        likely_causes=["Interrupted write to the FAT/directory area", "unsafe ejection during a copy"],
        software_fixable=True,
        steps=[
            "Image the device first, then work on the image.",
            "Rebuild the metadata from the backup copy if one is present.",
            "If the directory still cannot be trusted, recover files by content carving.",
        ],
        verification=["Directory listing shows the original names and sizes"],
        severity="warning",
        error_codes=["FILESYSTEM_METADATA_DAMAGED", "FAT_COPIES_DIFFER", "FAT32_FSINFO_INVALID"],
        sources=["https://learn.microsoft.com/windows/win32/fileio/fat32-file-system-specification"],
    ),
    KnowledgeEntry(
        key="zero-capacity",
        title="Drive enumerates but reports zero capacity",
        symptoms=["device appears in Device Manager / System Information",
                  "shows 0 bytes in every OS", "cannot be formatted"],
        likely_causes=[
            "Controller firmware crashed and did not initialise the flash translation layer",
            "NAND chip no longer answers the controller (dead chip or lifted solder joint)",
        ],
        software_fixable=False,
        steps=[
            "Test in a different port and machine to rule out a hub or cable fault.",
            "This is a hardware-level failure. Only a controller-specific manufacturing tool "
            "can reinitialise it, and it erases all data — recover first if possible.",
            "If the data matters, stop here and consult a professional recovery service.",
        ],
        verification=["The drive reports its normal capacity again"],
        severity="critical",
        error_codes=["ZERO_CAPACITY", "ZERO_WHILE_REPORTING_CAPACITY"],
        when_not_to=[
            "Never reflash firmware to chase a zero-capacity fault unless the exact model, "
            "controller revision and tool version are confirmed — a wrong flash makes it worse.",
        ],
        sources=["https://www.smartmontools.org/wiki/FAQ"],
    ),
    KnowledgeEntry(
        key="ntfs-mft-damage",
        title="NTFS master file table (MFT) damaged",
        symptoms=["volume mounts but file names are unreadable", "chkdsk reports MFT errors",
                  "folders appear as 'File or directory is corrupted'"],
        likely_causes=["Unsafe removal during a metadata write", "bad sectors in the MFT area"],
        software_fixable=True,
        steps=[
            "Image the volume and recover files from the image first.",
            "Let Windows run chkdsk /f only after the files you need are copied out.",
            "If the MFT itself is unreadable, recover by content carving instead of repairing.",
        ],
        verification=["chkdsk completes with no MFT errors", "File names list correctly"],
        severity="error",
        filesystems=["ntfs"],
        error_codes=["NTFS_MFT_DAMAGED", "NTFS_DIRTY"],
        sources=["https://learn.microsoft.com/windows-server/administration/windows-commands/chkdsk"],
    ),
    # ----------------------------------------------------- connection/power
    KnowledgeEntry(
        key="power-issues",
        title="Power-related disconnects",
        symptoms=["drive works on one port but not another", "disconnects during large writes",
                  "external HDD clicks and resets"],
        likely_causes=["Bus-powered drive exceeding port current", "unpowered hub", "thin cable",
                       "PSU/port undervoltage"],
        software_fixable=False,
        steps=[
            "Use a powered hub or a Y-cable for bus-powered drives.",
            "Prefer USB ports on the machine itself over front-panel or hub ports.",
            "For external HDDs, use the vendor's supplied power adapter.",
        ],
        verification=["No disconnect events during a sustained write test"],
        severity="warning",
        error_codes=["CONNECTION_UNSTABLE"],
        sources=["https://learn.microsoft.com/windows-hardware/design/component-guidelines/usb"],
    ),
)


def load_entries() -> tuple[KnowledgeEntry, ...]:
    return ENTRIES


def search(
    *,
    error_codes: Iterable[str] = (),
    filesystem: Optional[str] = None,
    brand: Optional[str] = None,
    vid: Optional[str] = None,
    symptom_text: str = "",
    limit: int = 12,
) -> list[tuple[int, KnowledgeEntry]]:
    """Rank entries against the observed evidence."""
    scored: list[tuple[int, KnowledgeEntry]] = []
    for entry in ENTRIES:
        score = entry.matches(
            error_codes=error_codes,
            filesystem=filesystem,
            brand=brand,
            vid=vid,
            symptom_text=symptom_text,
        )
        if score > 0:
            scored.append((score, entry))
    scored.sort(key=lambda item: (-item[0], -SEVERITY_ORDER.get(item[1].severity, 0)))
    return scored[:limit]


def get(key: str) -> Optional[KnowledgeEntry]:
    for entry in ENTRIES:
        if entry.key == key:
            return entry
    return None


def export_json(path: str) -> None:
    payload = [entry.to_dict() for entry in ENTRIES]
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
