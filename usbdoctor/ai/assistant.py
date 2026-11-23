"""Offline diagnostic assistant.

This is NOT a wrapper around an LLM and it does not fabricate advice.  It is a
deterministic reasoning layer that:

1. reads the *actual* evidence collected by the analyzers,
2. retrieves matching entries from the curated knowledge base,
3. ranks the repair plans produced by the rule-based planner,
4. explains — in plain language — what the evidence means and what is safe next,
5. states clearly when nothing can be done in software.

Design constraint: the assistant can only *describe* and *order* operations that
the safety-critical planner already deemed applicable.  It has no capability to
create, approve or execute a repair, which makes prompt-injection style attacks
structurally impossible.

If an optional local LLM is configured by the user (Settings → AI), it is used
only to *re-phrase* the deterministic explanation; every number and verdict
still comes from the engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from usbdoctor.analysis.health import Health, HealthReport
from usbdoctor.knowledge.database import KnowledgeEntry, search
from usbdoctor.repair.base import RepairPlan, Risk

#: Explicit ordering for Risk (it is a str-Enum, so int() does not work and
#: string comparison would sort "critical" before "low").
_RISK_ORDER = {Risk.NONE: 0, Risk.LOW: 1, Risk.MEDIUM: 2, Risk.HIGH: 3, Risk.CRITICAL: 4}

#: Explanations for the most important evidence codes, in the user's own terms.
CODE_EXPLANATIONS: dict[str, str] = {
    "MBR_SIGNATURE_MISSING": (
        "The disk's first sector lost its 0x55AA end marker, so the OS cannot see any "
        "partitions. The partition list itself is usually still on the disk."
    ),
    "FAT_BOOT_SECTOR_DESTROYED": (
        "The FAT boot sector was overwritten, but a complete backup copy exists further into "
        "the volume. Restoring it usually brings every file back with its original name."
    ),
    "EXFAT_BOOT_REGION_DESTROYED": (
        "The exFAT boot area was overwritten. The card keeps a second, complete copy, so this "
        "is normally repairable without touching your photos."
    ),
    "EXFAT_BOOT_CHECKSUM": (
        "The exFAT boot area's checksum no longer matches its contents, so the OS refuses to "
        "mount the card. The backup copy can be used to rebuild it."
    ),
    "EXFAT_DIRTY": (
        "The card was unplugged while data was still being written. It is marked 'dirty' and "
        "some systems mount it read-only until that flag is cleared."
    ),
    "NTFS_BOOT_SIGNATURE_MISSING": (
        "The NTFS boot sector's end marker is missing, which makes Windows show the volume as RAW. "
        "A two-byte repair restores it."
    ),
    "NTFS_DIRTY": (
        "NTFS recorded an unclean shutdown. Windows will run CHKDSK, which normally repairs it."
    ),
    "FAT_COPIES_DIFFER": (
        "The two FAT allocation tables disagree. The first copy is normally authoritative and "
        "can be mirrored onto the second."
    ),
    "FAT32_FSINFO_INVALID": (
        "The cached free-space information is corrupt. Rebuilding it changes only the counters, "
        "never your files."
    ),
    "GPT_BACKUP_MISSING": (
        "The backup partition table at the end of the disk is gone. It can be regenerated from "
        "the valid primary copy."
    ),
    "VOLUME_START_MISMATCH": (
        "A valid file system was found, but it starts at a different offset than the partition "
        "table says. The partition entry needs rebuilding to point at the real data."
    ),
    "SURFACE_READ_ERRORS": (
        "Sectors that cannot be read were found. This is physical media damage; the healthy "
        "areas can still be copied."
    ),
    "SURFACE_READ_ERRORS_CONFIRMED": (
        "Unreadable sectors were confirmed on a second pass. This is genuine flash or platter "
        "damage and will not heal."
    ),
    "CAPACITY_MISMATCH": (
        "The drive advertises more space than it really has. Anything written past the real end "
        "is silently lost — that is why files 'become corrupt' when the drive fills up."
    ),
    "ZERO_CAPACITY": (
        "The controller reports zero usable space. In practice this means the firmware or the "
        "flash translation layer has failed."
    ),
    "ZERO_WHILE_REPORTING_CAPACITY": (
        "The controller still advertises a size but exposes no addressable storage — a "
        "firmware/controller lock-up rather than a file-system problem."
    ),
    "DEVICE_READ_ONLY": (
        "The device refuses all writes. This can be a switch, an OS policy, or the controller "
        "locking itself to protect the last readable data."
    ),
    "READ_ONLY_WITH_FAILURE": (
        "Read-only mode together with media errors is the classic final stage of flash failure. "
        "The controller stopped accepting writes so existing data can still be read."
    ),
    "CONNECTION_UNSTABLE": (
        "The drive disappeared from the bus during testing, which points at the cable, the port "
        "or power delivery rather than the storage itself."
    ),
    "SMART_HEALTH_FAILED": (
        "The drive's own firmware reports failing health. This is first-hand evidence — not a "
        "prediction — and cannot be repaired by software."
    ),
    "SMART_ATTR_5_HIGH": (
        "Sectors had to be physically remapped to spare areas. A growing count means the medium "
        "is degrading."
    ),
    "SMART_ATTR_197_HIGH": (
        "Sectors are waiting to be remapped but cannot be read reliably. Data held there is at risk."
    ),
    "SMART_ATTR_199_ELEVATED": (
        "The link recorded CRC errors, which indicates cabling or connection problems."
    ),
    "SMART_WEAR_HIGH": (
        "The flash has consumed most of its rated write endurance."
    ),
    "SMART_SPARE_LOW": (
        "The spare-block pool is nearly exhausted, so future failures can no longer be absorbed."
    ),
    "FILESYSTEM_METADATA_DAMAGED": (
        "Only the file-system bookkeeping is damaged; the file contents themselves are usually intact."
    ),
    "SECTOR_ZERO_UNREADABLE": (
        "The very first sector cannot be read, which is where the partition table lives."
    ),
}


@dataclass
class DiagnosisSection:
    heading: str
    bullets: list[str]
    severity: str = "info"

    def to_dict(self) -> dict:
        return {"heading": self.heading, "bullets": self.bullets, "severity": self.severity}


@dataclass
class Diagnosis:
    headline: str
    summary: str
    sections: list[DiagnosisSection] = field(default_factory=list)
    recommended_steps: list[str] = field(default_factory=list)
    recommended_plans: list[str] = field(default_factory=list)
    knowledge: list[KnowledgeEntry] = field(default_factory=list)
    can_repair_in_software: bool = True
    data_at_risk: bool = False
    professional_recovery_advised: bool = False
    replacement_advised: bool = False
    friendly_message: str = ""
    confidence: str = "low"

    def to_dict(self) -> dict:
        return {
            "headline": self.headline,
            "summary": self.summary,
            "sections": [s.to_dict() for s in self.sections],
            "recommended_steps": self.recommended_steps,
            "recommended_plans": self.recommended_plans,
            "knowledge": [k.to_dict() for k in self.knowledge],
            "can_repair_in_software": self.can_repair_in_software,
            "data_at_risk": self.data_at_risk,
            "professional_recovery_advised": self.professional_recovery_advised,
            "replacement_advised": self.replacement_advised,
            "friendly_message": self.friendly_message,
            "confidence": self.confidence,
        }


FRIENDLY_HARDWARE_FAILURE = (
    "Bhai, is pendrive ne apni duty kaafi kar li hai! 😅 Isme hardware failure ke strong signs "
    "mil rahe hain. Isko software se permanently repair karna possible nahi lag raha. Agar data "
    "important hai toh professional recovery service try karo, warna nayi pendrive lena better rahega."
)

FRIENDLY_REPAIRABLE = (
    "Achhi khabar: ye problem software se theek ho sakti hai. Pehle backup/scan chalao, "
    "phir repair apply karo — data safe rahega."
)


class DiagnosticAssistant:
    """Turns evidence into a plain-language, prioritised diagnosis."""

    def __init__(self, *, max_knowledge: int = 4) -> None:
        self.max_knowledge = max_knowledge

    def diagnose(
        self,
        *,
        device_name: str,
        health: Optional[HealthReport],
        filesystem: str = "unknown",
        evidence_codes: Optional[list[str]] = None,
        plans: Optional[list[RepairPlan]] = None,
        brand: Optional[str] = None,
        vid: Optional[str] = None,
        capacity_claimed: Optional[int] = None,
        capacity_real: Optional[int] = None,
        smart_available: bool = False,
        filesystem_analysis: Optional[dict[str, Any]] = None,
        partition_analysis: Optional[dict[str, Any]] = None,
    ) -> Diagnosis:
        codes = list(evidence_codes or [])
        if health:
            codes.extend(f.code for f in health.findings)
        codes = _unique(codes)
        plans = plans or []
        verdict = health.verdict if health else Health.UNKNOWN

        sections: list[DiagnosisSection] = []
        steps: list[str] = []
        data_at_risk = verdict in (Health.WARNING, Health.CRITICAL, Health.FAILING)
        can_repair = False

        # 1. What the evidence means ------------------------------------------
        explained = [CODE_EXPLANATIONS[c] for c in codes if c in CODE_EXPLANATIONS]
        if explained:
            sections.append(
                DiagnosisSection("What the scan found", explained[:8], severity="warning")
            )

        # 2. Hardware vs software decision ------------------------------------
        hardware_only = bool(health and health.permanent_failure_suspected)
        low_risk_plans = sorted(
            (p for p in plans if p.risk in (Risk.NONE, Risk.LOW)),
            key=lambda p: _RISK_ORDER[p.risk],
        )
        repairable_metadata = any(
            c in codes
            for c in (
                "FAT_BOOT_SECTOR_DESTROYED",
                "EXFAT_BOOT_REGION_DESTROYED",
                "EXFAT_BOOT_CHECKSUM",
                "NTFS_BOOT_SIGNATURE_MISSING",
                "MBR_SIGNATURE_MISSING",
                "FAT_COPIES_DIFFER",
                "FAT32_FSINFO_INVALID",
                "GPT_BACKUP_MISSING",
                "VOLUME_START_MISMATCH",
                "FILESYSTEM_METADATA_DAMAGED",
            )
        )
        can_repair = bool(low_risk_plans) and not hardware_only

        if hardware_only:
            sections.append(
                DiagnosisSection(
                    "Hardware condition",
                    [
                        "The failure evidence comes from the drive itself "
                        "(SMART, unreadable media or a capacity mismatch).",
                        "Software can still copy what is readable, but it cannot restore the "
                        "damaged hardware.",
                    ],
                    severity="critical",
                )
            )
        elif repairable_metadata:
            sections.append(
                DiagnosisSection(
                    "Repairable problem",
                    [
                        "This is a file-system metadata problem, not a dead drive.",
                        "The repair rewrites a few hundred bytes of bookkeeping. "
                        "Your files are not touched.",
                    ],
                    severity="info",
                )
            )

        # 3. Plan recommendation ----------------------------------------------
        self._describe_plans(plans, steps, sections)

        # 4. Capacity facts ----------------------------------------------------
        if capacity_claimed and capacity_real and capacity_real < capacity_claimed:
            sections.append(
                DiagnosisSection(
                    "Capacity",
                    [
                        f"Advertised: {capacity_claimed:,} bytes",
                        f"Really usable: {capacity_real:,} bytes",
                    ],
                    severity="critical",
                )
            )

        # 5. Knowledge base ----------------------------------------------------
        knowledge = [
            entry
            for _, entry in search(
                error_codes=codes,
                filesystem=filesystem if filesystem not in ("unknown", "") else None,
                brand=brand,
                vid=vid,
                symptom_text=" ".join(codes[:5]),
                limit=self.max_knowledge,
            )
        ]

        professional = hardware_only and data_at_risk
        replacement = hardware_only

        # 6. Overall message ---------------------------------------------------
        headline, summary = self._headline(verdict, can_repair, data_at_risk, repairable_metadata)
        friendly = ""
        if hardware_only:
            friendly = FRIENDLY_HARDWARE_FAILURE
        elif repairable_metadata and not hardware_only:
            friendly = FRIENDLY_REPAIRABLE

        return Diagnosis(
            headline=headline,
            summary=summary,
            sections=sections,
            recommended_steps=steps,
            recommended_plans=[p.operation_id for p in low_risk_plans],
            knowledge=knowledge,
            can_repair_in_software=can_repair,
            data_at_risk=data_at_risk,
            professional_recovery_advised=professional,
            replacement_advised=replacement,
            friendly_message=friendly,
            confidence=(health.health_confidence if health else "low"),
        )

    # ------------------------------------------------------------------ parts
    def _describe_plans(self, plans: list[RepairPlan], steps: list[str], sections: list[DiagnosisSection]) -> None:
        if not plans:
            sections.append(
                DiagnosisSection(
                    "Next steps",
                    [
                        "No automatic repair applies to this device.",
                        "If files are missing, use Data Recovery to image the device and carve files.",
                    ],
                    severity="info",
                )
            )
            steps.append("Analyse the device read-only to gather evidence.")
            return
        ordered = sorted(plans, key=lambda p: _RISK_ORDER[p.risk])
        bullets = []
        steps.append("Create a backup image first if the device shows any hardware warnings.")
        for plan in ordered[:6]:
            bullets.append(f"{plan.title} — {plan.summary}")
            steps.append(plan.title)
        sections.append(DiagnosisSection("Recommended operations (in order)", bullets, severity="info"))

    @staticmethod
    def _headline(verdict: Health, can_repair: bool, data_at_risk: bool, repairable: bool) -> tuple[str, str]:
        if verdict == Health.FAILING:
            return (
                "Permanent hardware failure suspected",
                "The evidence shows physical damage that software cannot repair. "
                "Recover your data before doing anything else.",
            )
        if can_repair and repairable:
            return (
                "Repairable file-system damage",
                "The drive itself is healthy; its file-system bookkeeping is damaged and can be "
                "rebuilt without formatting.",
            )
        if verdict == Health.CRITICAL:
            return (
                "Serious problems — back up immediately",
                "Multiple errors were detected. Copy important files now; repairs may still help "
                "but reliability is already compromised.",
            )
        if verdict == Health.WARNING:
            return (
                "Problems detected",
                "Errors were found that are usually repairable. Back up first, then repair.",
            )
        if verdict == Health.EXCELLENT:
            return ("Device is healthy", "All checks passed and the device reports good health.")
        if verdict == Health.GOOD:
            return (
                "Looks healthy",
                "No errors were found. This device does not report full hardware health data, so "
                "the verdict is based on read-only surface and metadata checks.",
            )
        return (
            "Not enough information",
            "The device does not expose hardware health data. Run a surface scan to gather real evidence.",
        )


def _unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    output = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            output.append(item)
    return output


def render_text(diagnosis: Diagnosis) -> str:
    """Plain-text report (used by the CLI and saved reports)."""
    lines = [f"{diagnosis.headline}", "=" * len(diagnosis.headline), diagnosis.summary, ""]
    for section in diagnosis.sections:
        lines.append(section.heading.upper())
        for bullet in section.bullets:
            lines.append(f"  • {bullet}")
        lines.append("")
    if diagnosis.recommended_steps:
        lines.append("RECOMMENDED ORDER")
        for index, step in enumerate(diagnosis.recommended_steps, start=1):
            lines.append(f"  {index}. {step}")
        lines.append("")
    if diagnosis.knowledge:
        lines.append("RELATED KNOWN ISSUES")
        for entry in diagnosis.knowledge:
            fixable = "software-repairable" if entry.software_fixable else "hardware — not software-repairable"
            lines.append(f"  • {entry.title} ({fixable})")
            for source in entry.sources[:2]:
                lines.append(f"      source: {source}")
        lines.append("")
    if diagnosis.friendly_message:
        lines.append(diagnosis.friendly_message)
        lines.append("")
    lines.append(f"Confidence: {diagnosis.confidence}")
    return "\n".join(lines)
