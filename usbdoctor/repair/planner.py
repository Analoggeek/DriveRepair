"""Evidence-driven repair planning.

The planner is intentionally rule-based, deterministic and auditable: safety
critical decisions must never depend on an unpredictable model.  Each rule
matches *specific evidence* collected by the analyzers and emits a plan with an
explicit risk level, the effects it will have, and how it is verified.

The AI assistant layer (``usbdoctor.ai``) may explain and prioritise these plans
in natural language, but it cannot invent or execute one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from usbdoctor.analysis.filesystems.detect import analyze_volume, probe_boot_signatures, sniff_extended
from usbdoctor.analysis.health import HealthReport
from usbdoctor.analysis.partitions.tables import (
    analyze_gpt,
    parse_gpt_entries,
    parse_gpt_header,
    parse_mbr,
)
from usbdoctor.core.io import DiskReader
from usbdoctor.core.model import DeviceRef
from usbdoctor.core.platform import detect_system, is_admin
from usbdoctor.core.util import human_size, tool_available
from usbdoctor.repair.base import Effect, RepairPlan, Risk
from usbdoctor.repair import handlers_metadata as meta
from usbdoctor.repair import native_tools

#: Evidence codes that indicate a *hardware* problem software cannot fix.
HARDWARE_ONLY_CODES = {
    "SURFACE_READ_ERRORS",
    "SURFACE_READ_ERRORS_CONFIRMED",
    "CAPACITY_MISMATCH",
    "ZERO_CAPACITY",
    "ZERO_WHILE_REPORTING_CAPACITY",
    "SMART_HEALTH_FAILED",
    "SMART_NVME_CRITICAL",
    "SMART_SPARE_LOW",
    "SMART_ATTR_5_HIGH",
}


@dataclass
class ScanEvidence:
    """Everything the analysers learned, as input to planning."""

    device: DeviceRef
    filesystem: str = "unknown"
    filesystem_issues: list[Any] = field(default_factory=list)
    filesystem_patches: list[dict[str, Any]] = field(default_factory=list)
    partition_scheme: str = "none"
    partition_issues: list[Any] = field(default_factory=list)
    gpt_backup_missing: bool = False
    mbr_signature_ok: bool = True
    mbr_has_partitions: bool = True
    health: Optional[HealthReport] = None
    write_protected: bool = False
    commands_failing: bool = False
    read_only_is_hardware: bool = False
    smart_available: bool = False
    os_reports_fs: Optional[str] = None
    detected_fs: Optional[str] = None
    #: Offset of a valid volume found inside an unpartitioned range.
    volume_start_hint: Optional[int] = None
    #: Absolute byte offset of the volume the file-system findings describe.
    #: Repairs must target this, not LBA 0, or a partitioned disk loses its
    #: partition table to a "repair" of a healthy sector.
    volume_base_offset: int = 0

    def codes(self) -> set[str]:
        out = {getattr(i, "code", "") for i in self.filesystem_issues}
        out |= {getattr(i, "code", "") for i in self.partition_issues}
        if self.health:
            out |= {f.code for f in self.health.findings}
        return out


def collect_evidence(device: DeviceRef, *, health: Optional[HealthReport] = None) -> ScanEvidence:
    """Read the device's metadata and build the planner's evidence bundle."""
    evidence = ScanEvidence(device=device, health=health, os_reports_fs=device.filesystem)
    evidence.write_protected = bool(device.read_only)
    if not device.size_bytes:
        return evidence
    gpt_entry_list: list[Any] = []
    analysed_offset = 0
    base_offset = 0
    volume_size = device.size_bytes
    if device.kind == "partition" and device.partitions:
        base_offset = device.partitions[0].offset_bytes
        volume_size = device.partitions[0].size_bytes
    try:
        with DiskReader(device.path, sector_size=device.sector_size) as reader:
            sector = reader.read(0, 512)
            mbr = parse_mbr(sector, device.size_bytes or 0, sector_size=reader.sector_size)
            evidence.partition_scheme = mbr.scheme
            evidence.partition_issues = list(mbr.issues)
            evidence.mbr_signature_ok = sector[510:512] == b"\x55\xaa"
            evidence.mbr_has_partitions = bool(mbr.entries)
            if mbr.scheme == "gpt":
                primary = parse_gpt_header(reader.read(512, 512))
                entries_data = (
                    reader.read(primary["entries_lba"] * 512, primary["num_entries"] * primary["entry_size"])
                    if primary
                    else b""
                )
                entries, crc_ok = parse_gpt_entries(entries_data, primary) if primary else ([], False)
                gpt_entry_list = list(entries)
                backup_header = parse_gpt_header(reader.read((device.size_bytes or 0) - 512, 512))
                backup_entries: list[Any] = []
                backup_crc = False
                if backup_header:
                    backup_entries, backup_crc = parse_gpt_entries(
                        reader.read(backup_header["entries_lba"] * 512, 128 * 128), backup_header
                    )
                gpt = analyze_gpt(
                    primary,
                    entries,
                    crc_ok,
                    backup_header=backup_header,
                    backup_entries=backup_entries,
                    backup_crc_ok=backup_crc,
                    disk_size=device.size_bytes or 0,
                )
                evidence.partition_issues = list(gpt.issues)
                evidence.gpt_backup_missing = backup_header is None
                evidence.partition_scheme = "gpt"
            volume = analyze_volume(reader, base_offset, volume_size=volume_size)
            analysed_offset = base_offset
            evidence.filesystem = volume.filesystem
            evidence.filesystem_issues = list(volume.issues)
            if volume.filesystem == "unknown":
                # A partitioned device has no file system at byte 0.  Analyse the
                # first populated partition instead of reporting a false "RAW".
                first = None
                if mbr.entries:
                    first = min(mbr.entries, key=lambda e: e.offset)
                elif evidence.partition_scheme == "gpt" and gpt_entry_list:
                    ordered = sorted(gpt_entry_list, key=lambda e: e.offset)
                    first = ordered[0] if ordered else None
                if first is not None and getattr(first, "size", 0):
                    part_view = analyze_volume(reader, first.offset, volume_size=first.size)
                    evidence.filesystem = part_view.filesystem
                    evidence.filesystem_issues = list(part_view.issues)
                    if part_view.filesystem == "unknown":
                        # Do not surface "the whole disk is RAW" when the disk is
                        # simply unformatted inside a valid partition table.
                        evidence.filesystem_issues = [
                            i for i in part_view.issues if i.code not in ("FS_WIPED", "FS_BLANK", "FS_UNKNOWN")
                        ]
                    else:
                        evidence.detected_fs = part_view.filesystem
                    # Always adopt the partition view: it is the analysis that
                    # describes the volume being repaired. Its patches (e.g. a
                    # boot sector restorable from the backup at sector 6) are
                    # relative to that partition, not to the disk.
                    volume = part_view
                    evidence.device.native["analysed_partition_offset"] = first.offset
                    analysed_offset = first.offset
            # ``FullAnalysis.patches`` normalises object- and dict-based details;
            # reaching into ``volume.detail`` lost the patches for the
            # "boot sector destroyed" path, where detail is a plain dict.
            evidence.filesystem_patches = [
                p if isinstance(p, dict) else p.to_dict() for p in volume.patches
            ]
            if volume.filesystem == "unknown" and not (device.partitions or mbr.entries):
                candidates = probe_boot_signatures(reader, base_offset, sector_size=reader.sector_size)
                for cand in candidates:
                    if cand.offset == 0:
                        evidence.detected_fs = cand.filesystem
                        break
                else:
                    # A valid boot sector further in means the partition entry is
                    # wrong/missing, not that the data is gone.
                    for cand in candidates:
                        if cand.offset >= 64 * 1024:
                            evidence.detected_fs = cand.filesystem
                            evidence.volume_start_hint = cand.offset
                            break
        evidence.volume_base_offset = analysed_offset or 0
    except Exception as exc:  # unreadable device: record, do not crash
        evidence.partition_issues.append(
            type("Issue", (), {"code": "DEVICE_UNREADABLE", "severity": "critical", "message": str(exc)})()
        )
    return evidence


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def plan_repairs(evidence: ScanEvidence, *, mode: str = "smart", dry_run: bool = False) -> list[RepairPlan]:
    """Return every applicable repair, best-first, with risk levels attached.

    ``mode`` is ``safe`` (read-only checks only), ``smart`` (low-risk repairs
    included) or ``advanced`` (includes medium/high risk operations, each still
    requiring its own consent).
    """
    plans: list[RepairPlan] = []
    device = evidence.device
    codes = evidence.codes()

    # 0. Hardware-only conditions ----------------------------------------
    if codes & HARDWARE_ONLY_CODES:
        plans.append(
            RepairPlan(
                operation_id="backup-image",
                title="Create a full disk image (recommended first)",
                summary=(
                    "The device shows signs of hardware trouble. Imaging it now preserves every "
                    "recoverable byte before any repair attempt touches the original."
                ),
                risk=Risk.NONE,
                requires_admin=False,
                effects=[Effect("Read the device sector-by-sector into an image file")],
                data_loss_warning=None,
                rollback="The original device is never written to by this operation.",
                evidence={"codes": sorted(codes & HARDWARE_ONLY_CODES)},
            )
        )

    # 1. Partition signature ------------------------------------------------
    restore_patch = bool(
        set(p.get("reason_code", "") for p in evidence.filesystem_patches)
        & {"FAT32_RESTORE_PRIMARY_BOOT", "FAT_BOOT_RESTORE", "EXFAT_RESTORE_BOOT_REGION"}
    )
    boot_sector_repair_planned = restore_patch or bool(
        {"FAT_BOOT_SECTOR_DESTROYED", "EXFAT_BOOT_REGION_DESTROYED"} & codes
    )
    # Only defer the MBR signature when the file system at LBA 0 is itself the
    # thing being rebuilt — in that case the "missing" 0x55AA belongs to the
    # volume's boot record, not to a partition table. When a partition table is
    # present and readable, a missing 0x55AA is genuine MBR damage and must be
    # repaired, volume or no volume.
    table_present = evidence.mbr_has_partitions or evidence.partition_scheme == "gpt"
    if (
        not evidence.mbr_signature_ok
        and evidence.partition_scheme in ("none", "unknown", "mbr")
        and (table_present or not boot_sector_repair_planned)
    ):
        plans.append(
            RepairPlan(
                operation_id="mbr-signature",
                title="Restore the boot signature (0x55AA)",
                summary="The MBR boot marker is missing, which makes the OS treat the disk as RAW.",
                risk=Risk.LOW,
                effects=[Effect("Write 2 bytes at offset 510", offset=510, length=2, reversible=True)],
                rollback="Original bytes are saved to a rollback file first.",
                evidence={"codes": sorted(codes)},
                runner=meta.repair_mbr_signature,
            )
        )

    # 2. Partition table empty but data present -----------------------------
    table_lost = (
        evidence.mbr_signature_ok
        and evidence.partition_scheme != "gpt"
        and evidence.detected_fs
        and (not evidence.mbr_has_partitions or evidence.volume_start_hint is not None)
    )
    if table_lost:
        plans.append(
            RepairPlan(
                operation_id="partition-table-rebuild",
                title="Reconstruct the partition table",
                summary=(
                    f"A valid {evidence.detected_fs.upper()} file system was found on the device, but the "
                    "partition table no longer references it. The table can be rebuilt to point at the "
                    "existing data without formatting."
                ),
                risk=Risk.MEDIUM,
                effects=[
                    Effect("Rewrite the 4 MBR partition entries", offset=0x1BE, length=64, reversible=True)
                ],
                data_loss_warning=(
                    "If the reconstructed geometry is wrong, the volume may still not mount. The original "
                    "MBR is saved first so this can be undone."
                ),
                rollback="The original MBR sector is saved to a rollback file.",
                evidence={"detected_filesystem": evidence.detected_fs},
                runner=meta.repair_partition_table,
            )
        )

    # 3. GPT backup ---------------------------------------------------------
    if evidence.gpt_backup_missing:
        plans.append(
            RepairPlan(
                operation_id="gpt-backup",
                title="Rebuild the backup GPT header",
                summary="The backup GPT at the end of the disk is missing. It can be regenerated from "
                "the verified primary header.",
                risk=Risk.LOW,
                effects=[
                    Effect("Write backup GPT entry array", reversible=True),
                    Effect("Write backup GPT header with corrected LBAs and CRC", reversible=True),
                ],
                rollback="Both regions are snapshotted before writing.",
                runner=meta.repair_gpt_backup,
            )
        )

    # 4. File-system metadata ----------------------------------------------
    patch_codes = {p.get("reason_code", "") for p in evidence.filesystem_patches}
    filesystem_family = evidence.filesystem or evidence.detected_fs or ""
    if filesystem_family.startswith("fat") or filesystem_family in ("fat", "unknown", "") or evidence.detected_fs:
        # A primary boot sector wiped to zeroes does not identify as FAT any
        # more, so the analyser reports VOLUME_START_MISMATCH instead. The
        # backup copy 6 sectors in is still intact, which is exactly what makes
        # this repairable — do not hide that behind "file-level recovery only".
        wiped_but_backed_up = "VOLUME_START_MISMATCH" in codes and (
            evidence.volume_base_offset or 0
        ) >= 0 and bool(evidence.filesystem_patches)
        if (
            patch_codes & {"FAT32_RESTORE_PRIMARY_BOOT", "FAT_BOOT_RESTORE"}
            or wiped_but_backed_up
            or any(
                c in codes
                for c in ("FAT_BOOT_SIGNATURE_MISSING", "FAT_BOOT_SECTOR_DESTROYED", "FAT32_BACKUP_REQUIRED")
            )
        ):
            plans.append(
                RepairPlan(
                    operation_id="fat-boot",
                    title="Restore the FAT boot sector from its backup",
                    summary="A valid backup boot sector exists on the volume and can rebuild the destroyed "
                    "primary boot sector, making the files accessible again.",
                    risk=Risk.LOW,
                    effects=[Effect("Write one 512-byte boot sector", length=512, reversible=True)],
                    rollback="The damaged sector is snapshotted first.",
                    evidence={"patches": sorted(patch_codes)},
                    runner=meta.repair_fat_boot_sector,
                )
            )
        if "FAT_COPIES_DIFFER" in codes:
            plans.append(
                RepairPlan(
                    operation_id="fat-mirror",
                    title="Synchronise the two FAT copies",
                    summary="Both FAT tables must match. Copy #1 is intact, so copy #2 can be rewritten "
                    "from it without touching your files.",
                    risk=Risk.LOW,
                    effects=[Effect("Rewrite FAT copy #2 from copy #1", reversible=True)],
                    rollback="Copy #2 is snapshotted before rewriting.",
                    runner=meta.repair_fat_mirror,
                )
            )
        if any(c in codes for c in ("FAT32_FSINFO_INVALID", "FAT32_FSINFO_IMPLAUSIBLE", "FAT32_NO_FSINFO")):
            plans.append(
                RepairPlan(
                    operation_id="fat-fsinfo",
                    title="Rebuild the FAT32 FSInfo sector",
                    summary="The cached free-space data is corrupt. Rebuilding it restores correct free-space "
                    "reporting; file data is untouched.",
                    risk=Risk.LOW,
                    effects=[Effect("Rewrite the FSInfo sector", length=512, reversible=True)],
                    rollback="The sector is snapshotted before rewriting.",
                    runner=meta.repair_fat_fsinfo,
                )
            )

    # A wiped primary no longer identifies as exFAT, but the analyser still
    # found the intact backup region and produced the restore patch for it.
    exfat_restorable = "EXFAT_RESTORE_BOOT_REGION" in patch_codes
    if evidence.filesystem == "exfat" or evidence.detected_fs == "exfat" or exfat_restorable:
        if (
            "EXFAT_BOOT_CHECKSUM" in codes
            or "EXFAT_BOOT_REGION_DESTROYED" in codes
            or exfat_restorable
        ):
            plans.append(
                RepairPlan(
                    operation_id="exfat-boot",
                    title="Restore the exFAT boot region from its backup",
                    summary="The exFAT boot checksum fails but the backup copy verifies, so the primary can "
                    "be rebuilt exactly.",
                    risk=Risk.LOW,
                    effects=[Effect("Write the 12-sector exFAT boot region", length=6144, reversible=True)],
                    rollback="The damaged region is snapshotted first.",
                    runner=meta.repair_exfat_boot,
                )
            )
        if "EXFAT_DIRTY" in codes:
            plans.append(
                RepairPlan(
                    operation_id="exfat-dirty",
                    title="Clear the exFAT dirty flag",
                    summary="The volume was not unmounted cleanly. Clearing the flag marks it consistent so "
                    "that the OS stops refusing normal access.",
                    risk=Risk.LOW,
                    effects=[Effect("Clear dirty bit in the primary and backup boot regions", length=2, reversible=True)],
                    rollback="Both flag locations are snapshotted first.",
                    runner=meta.repair_exfat_dirty,
                )
            )

    if evidence.filesystem == "ntfs" and "NTFS_BOOT_SIGNATURE_MISSING" in codes:
        plans.append(
            RepairPlan(
                operation_id="ntfs-boot",
                title="Restore the NTFS boot signature",
                summary="The 0x55AA marker is missing from the NTFS boot sector, so the volume shows as RAW.",
                risk=Risk.LOW,
                effects=[Effect("Write 2 bytes at offset 510", offset=510, length=2, reversible=True)],
                rollback="Snapshotted first.",
                runner=meta.repair_ntfs_boot,
            )
        )

    # 5. Native file-system check (check-only first) ------------------------
    fs_known = evidence.filesystem not in ("unknown", "") and evidence.filesystem != "invalid"
    native_codes = {"NTFS_DIRTY", "EXT_NOT_CLEAN", "EXT_ERRORS_FLAGGED", "HFS_NOT_UNMOUNTED",
                    "FAT_ROOT_BAD_ENTRIES", "FAT_CHAIN_LOOP", "EXFAT_ROOT_LOOP"}
    # A checker is only run when there is a real, mountable file system and the
    # device is not an image file (native tools address OS volumes, not images).
    is_native_target = evidence.device.kind != "image"
    if is_native_target and fs_known and (codes & native_codes or mode in ("smart", "advanced")):
        plans.append(
            RepairPlan(
                operation_id="native-check",
                title="Run the operating system's file-system check (read-only)",
                summary=platform_check_description(evidence.filesystem),
                risk=Risk.NONE,
                requires_consent=False,
                tool_requirement=platform_check_tool(evidence.filesystem),
                effects=[Effect("Run the native checker in verify-only mode")],
                evidence={"filesystem": evidence.filesystem},
                runner=lambda ctx: native_tools.run_native_check(ctx, filesystem=evidence.filesystem, repair=False),
            )
        )
        if mode == "advanced" and codes & native_codes:
            plans.append(
                RepairPlan(
                    operation_id="native-repair",
                    title="Let the operating system repair the file system",
                    summary=platform_check_description(evidence.filesystem),
                    risk=Risk.MEDIUM,
                    tool_requirement=platform_check_tool(evidence.filesystem),
                    effects=[Effect("Run the native checker with repair enabled", reversible=False)],
                    data_loss_warning=(
                        "File-system repair tools may move or remove directory entries they consider "
                        "invalid. Ensure a backup or image exists first."
                    ),
                    rollback="A disk image taken beforehand is the rollback for this operation.",
                    runner=lambda ctx: native_tools.run_native_check(
                        ctx, filesystem=evidence.filesystem, repair=True
                    ),
                )
            )

    # 6. SMART self-test ----------------------------------------------------
    if evidence.smart_available and mode != "safe":
        plans.append(
            RepairPlan(
                operation_id="smart-selftest",
                title="Start the drive's built-in SMART self-test",
                summary="Asks the drive's own firmware to test its platters/flash and report the result on "
                "the next health check.",
                risk=Risk.NONE,
                requires_consent=False,
                tool_requirement="smartmontools",
                effects=[Effect("Send the self-test command to the device firmware")],
                runner=lambda ctx: native_tools.start_smart_selftest(ctx, test="short"),
            )
        )

    # 7. Write-protection diagnosis ----------------------------------------
    if evidence.write_protected:
        plans.append(
            RepairPlan(
                operation_id="write-protection-diagnosis",
                title="Diagnose the write-protection cause",
                summary="Determine whether the lock is a physical switch, an OS policy, or the controller "
                "locking itself because of failing flash.",
                risk=Risk.NONE,
                requires_consent=False,
                effects=[Effect("Read protection state via OS APIs (no writes)")],
                runner=lambda ctx: diagnose_write_protection(ctx, evidence),
            )
        )

    # Filter by mode --------------------------------------------------------
    if mode == "safe":
        plans = [p for p in plans if p.risk in (Risk.NONE,) and p.operation_id != "smart-selftest"]
    elif mode == "smart":
        plans = [p for p in plans if p.risk in (Risk.NONE, Risk.LOW)]
    # 'advanced' keeps everything
    # Boot-sector restorations first: they usually make the *other* findings
    # (which were computed on the damaged volume) obsolete.
    restart_order = {"fat-boot": 0, "exfat-boot": 0, "ntfs-boot": 0, "partition-table-rebuild": 1}
    order = {Risk.NONE: 0, Risk.LOW: 1, Risk.MEDIUM: 2, Risk.HIGH: 3, Risk.CRITICAL: 4}
    # File-system repairs are relative to the volume, not to LBA 0. Carry the
    # offset the analysis used so the runner patches the right bytes.
    volume_ops = {
        "fat-boot", "fat-mirror", "fat-fsinfo", "exfat-boot", "exfat-dirty", "ntfs-boot",
    }
    for plan in plans:
        if plan.operation_id in volume_ops:
            plan.evidence["volume_base_offset"] = evidence.volume_base_offset or 0

    plans.sort(key=lambda p: (restart_order.get(p.operation_id, 5), order[p.risk], p.operation_id))
    if not is_admin():
        for plan in plans:
            if plan.requires_admin:
                plan.effects.append(
                    Effect("Requires administrator privileges (currently not granted)", reversible=True)
                )
    if dry_run:
        for plan in plans:
            plan.evidence["dry_run"] = True
    return plans


def platform_check_tool(filesystem: str) -> str:
    system = detect_system()
    if system == "windows":
        return "chkdsk (built in)"
    if system == "macos":
        return "diskutil (built in)"
    fs = (filesystem or "").lower()
    if fs.startswith("fat"):
        return "dosfstools"
    if fs == "exfat":
        return "exfatprogs"
    if fs.startswith("ntfs"):
        return "ntfs-3g"
    return "e2fsprogs"


def platform_check_description(filesystem: str) -> str:
    system = detect_system()
    if system == "windows":
        return f"Run CHKDSK on the mounted {filesystem.upper()} volume in verify-only mode (no changes)."
    if system == "macos":
        return f"Run 'diskutil verifyVolume' against the {filesystem.upper()} volume (no changes)."
    return f"Run the native {filesystem} checker in read-only mode (no changes)."


# ---------------------------------------------------------------------------
# Write-protection diagnosis (real, read-only)
# ---------------------------------------------------------------------------


def diagnose_write_protection(ctx: Any, evidence: ScanEvidence) -> Any:
    from usbdoctor.repair.base import RepairResult, RepairStatus

    device: DeviceRef = ctx.device
    findings: list[str] = []
    cause = "unknown"
    system = detect_system()
    if system == "windows":
        findings.append("Windows reports the disk as read-only via the Storage API.")
        findings.append(
            "Check the physical write-protect switch, then run 'diskpart' → 'attributes disk clear readonly' "
            "if the lock is policy-based."
        )
        cause = "policy_or_switch"
    elif system == "macos":
        findings.append("macOS reports the media as read-only.")
        findings.append(
            "SD cards have a physical lock tab; USB enclosures may expose a switch. If neither applies, "
            "the controller may have locked the flash."
        )
        cause = "switch_or_controller"
    else:
        try:
            name = device.path.replace("/dev/", "")
            with open(f"/sys/block/{name}/ro", "r", encoding="ascii") as fh:
                ro_flag = fh.read().strip()
            findings.append(f"Kernel reports /sys/block/{name}/ro = {ro_flag}.")
            if ro_flag == "1":
                cause = "kernel_or_controller"
                findings.append(
                    "The block layer is enforcing read-only. This is usually the controller locking itself "
                    "after detecting failed flash writes — not a switch or a policy."
                )
        except OSError:
            findings.append("Kernel read-only flag could not be read for this device.")
    hardware_locked = any("controller" in f for f in findings) or (
        evidence.health is not None and bool(evidence.health.findings)
    )
    message = "Write protection is likely software/policy based." if not hardware_locked else (
        "Write protection is most likely enforced by the device controller (self-lock after fault). "
        "Software cannot remove it; recover the data and replace the device."
    )
    return RepairResult(
        status=RepairStatus.SUCCEEDED,
        message=message,
        verified=True,
        details={"cause": cause, "findings": findings},
    )
