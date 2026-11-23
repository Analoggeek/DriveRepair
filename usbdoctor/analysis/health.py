"""Health verdict engine.

Combines *evidence* (SMART, surface scans, file-system findings, connection
stability, capacity verification) into one of six verdicts.  The scoring is
deliberately conservative and explainable:

* A verdict is never better than the worst *confirmed* hardware signal.
* If no hardware-health information exists at all, the verdict is UNKNOWN —
  never a made-up percentage.
* Each verdict carries the findings that produced it, so the UI can show
  exactly why.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Optional

from usbdoctor.analysis.smart.reader import SmartResult, smart_attribute_issues


class Health(IntEnum):
    UNKNOWN = 0
    EXCELLENT = 1
    GOOD = 2
    WARNING = 3
    CRITICAL = 4
    FAILING = 5

    @property
    def label(self) -> str:
        return self.name

    @property
    def color(self) -> str:
        return {
            "UNKNOWN": "#8b95a7",
            "EXCELLENT": "#3ddc97",
            "GOOD": "#7fd858",
            "WARNING": "#ffc857",
            "CRITICAL": "#ff8a3d",
            "FAILING": "#ff4d5e",
        }[self.name]

    @property
    def summary(self) -> str:
        return {
            "EXCELLENT": "Device appears healthy.",
            "GOOD": "Minor warnings only.",
            "WARNING": "Errors detected — back up your data now.",
            "CRITICAL": "Serious errors or unreliable storage.",
            "FAILING": "Strong evidence of physical failure.",
            "UNKNOWN": "Not enough hardware health information available.",
        }[self.name]


@dataclass
class Finding:
    severity: str  # info | warning | error | critical
    code: str
    message: str
    source: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "source": self.source,
            "evidence": self.evidence,
        }


@dataclass
class HealthReport:
    verdict: Health
    findings: list[Finding] = field(default_factory=list)
    health_confidence: str = "low"  # low | medium | high
    metrics: dict[str, Any] = field(default_factory=dict)
    explanation: list[str] = field(default_factory=list)
    permanent_failure_suspected: bool = False
    confirmed_failure: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.name,
            "verdict_label": self.verdict.label,
            "verdict_color": self.verdict.color,
            "verdict_summary": self.verdict.summary,
            "findings": [f.to_dict() for f in self.findings],
            "health_confidence": self.health_confidence,
            "metrics": self.metrics,
            "explanation": self.explanation,
            "permanent_failure_suspected": self.permanent_failure_suspected,
            "confirmed_failure": self.confirmed_failure,
            "counts": self.counts(),
        }

    @property
    def verdict_summary(self) -> str:
        """Short human description of the verdict (mirrors the JSON field)."""
        return self.verdict.summary

    @property
    def verdict_color(self) -> str:
        return self.verdict.color

    def counts(self) -> dict[str, int]:
        out = {"info": 0, "warning": 0, "error": 0, "critical": 0}
        for finding in self.findings:
            out[finding.severity] = out.get(finding.severity, 0) + 1
        return out

    def add(self, severity: str, code: str, message: str, *, source: str = "", **evidence: Any) -> None:
        self.findings.append(
            Finding(severity=severity, code=code, message=message, source=source, evidence=evidence)
        )


def _escalate(current: Health, candidate: Health) -> Health:
    if current == Health.UNKNOWN:
        return candidate
    if candidate == Health.UNKNOWN:
        return current
    return candidate if candidate > current else current


def evaluate_health(
    *,
    smart: Optional[SmartResult] = None,
    surface_read_errors: int = 0,
    surface_error_bytes: int = 0,
    surface_scanned_bytes: int = 0,
    filesystem_findings: Optional[list[Any]] = None,
    partition_findings: Optional[list[Any]] = None,
    capacity_verified: Optional[bool] = None,
    capacity_claimed: Optional[int] = None,
    capacity_real: Optional[int] = None,
    disconnect_count: int = 0,
    write_protected: Optional[bool] = None,
    zero_capacity: bool = False,
    reported_capacity: Optional[int] = None,
    repeated_error_regions: bool = False,
    read_speed_bps: Optional[float] = None,
    expected_speed_bps: Optional[float] = None,
    connection_speed_label: Optional[str] = None,
) -> HealthReport:
    """Produce the honest, evidence-backed verdict for one device."""
    findings: list[Finding] = []
    metrics: dict[str, Any] = {}
    explanation: list[str] = []
    confidence = "low"

    def add(severity: str, code: str, message: str, *, source: str = "", **evidence: Any) -> None:
        findings.append(Finding(severity, code, message, source, evidence))

    # ---------------- SMART (strongest evidence when present) -------------
    if smart is not None and smart.available:
        confidence = "high"
        metrics["smart_source"] = smart.source
        if smart.temperature_c is not None:
            metrics["temperature_c"] = smart.temperature_c
        if smart.power_on_hours is not None:
            metrics["power_on_hours"] = smart.power_on_hours
        for issue in smart_attribute_issues(smart):
            add(
                issue["severity"],
                issue["code"],
                issue["message"],
                source="smart",
                value=issue.get("value"),
            )
        if smart.health_text:
            metrics["smart_health_text"] = smart.health_text
    elif smart is not None:
        explanation.append(
            "SMART is not available for this device: " + (smart.reason or "the device does not report it.")
        )

    # ---------------- Surface read errors ---------------------------------
    metrics["surface_read_errors"] = surface_read_errors
    metrics["surface_error_bytes"] = surface_error_bytes
    metrics["surface_scanned_bytes"] = surface_scanned_bytes
    surface_confirmed = False
    if surface_read_errors:
        # One unreachable region can be an intermittent cable/port problem; a
        # cluster of them, or the same region failing a verification re-read,
        # is real media damage.
        severity = "critical"
        code = "SURFACE_READ_ERRORS"
        if surface_read_errors >= 8 or repeated_error_regions:
            surface_confirmed = True
            code = "SURFACE_READ_ERRORS_CONFIRMED"
        add(
            severity,
            code,
            f"{surface_read_errors} unreadable region(s) found "
            f"({surface_error_bytes:,} bytes) during a read-only surface scan"
            + (" (confirmed by a second pass)." if repeated_error_regions else "."),
            source="surface",
            regions=surface_read_errors,
            repeated=repeated_error_regions,
        )
        confidence = "high" if surface_scanned_bytes else confidence
    elif surface_scanned_bytes > 0:
        from usbdoctor.core.util import human_size

        add(
            "info",
            "SURFACE_READABLE",
            f"Every sector in the scanned range ({human_size(surface_scanned_bytes)}) was read successfully.",
            source="surface",
        )

    # ---------------- Capacity verification -------------------------------
    if zero_capacity:
        if reported_capacity:
            add(
                "critical",
                "ZERO_WHILE_REPORTING_CAPACITY",
                f"The controller advertises {reported_capacity:,} bytes but exposes zero addressable "
                "capacity — a classic controller/firmware lock-up.",
                source="os",
            )
        else:
            add(
                "critical",
                "ZERO_CAPACITY",
                "The device reports zero usable capacity.",
                source="os",
            )
    if capacity_verified is False and capacity_claimed and capacity_real:
        add(
            "critical",
            "CAPACITY_MISMATCH",
            f"Reported capacity is {capacity_claimed:,} bytes but only {capacity_real:,} bytes are "
            "addressable — a strong sign of a counterfeit drive.",
            source="capacity",
            claimed=capacity_claimed,
            real=capacity_real,
        )

    # ---------------- Connection stability --------------------------------
    if disconnect_count:
        severity = "critical" if disconnect_count >= 3 else "warning"
        add(
            severity,
            "CONNECTION_UNSTABLE",
            f"The device dropped off the bus {disconnect_count} time(s) during diagnostics.",
            source="usb",
        )
    if connection_speed_label:
        metrics["usb_speed"] = connection_speed_label

    # ---------------- Read-only state -------------------------------------
    if write_protected:
        add(
            "warning",
            "DEVICE_READ_ONLY",
            "The device is currently read-only. It may be a switch, a policy lock, or a "
            "controller-level lock triggered by failing flash.",
            source="os",
        )
    if write_protected and (surface_read_errors or (smart and smart.available and smart.health_passed is False)):
        add(
            "critical",
            "READ_ONLY_WITH_FAILURE",
            "Read-only mode combined with media errors or failing SMART usually means the "
            "controller has locked the drive to protect your data. Copy data off now.",
            source="combined",
        )

    # ---------------- File-system / partition findings --------------------
    for finding in (filesystem_findings or []):
        severity = getattr(finding, "severity", "warning")
        code = getattr(finding, "code", "FS_ISSUE")
        message = getattr(finding, "message", "")
        if severity in ("critical", "error") and code in (
            "FAT_BOOT_SIGNATURE_MISSING",
            "EXFAT_BOOT_REGION_DESTROYED",
            "NTFS_BOOT_SIGNATURE_MISSING",
            "FAT_BOOT_SECTOR_DESTROYED",
            "NTFS_BOOT_SIGNATURE_MISSING",
        ):
            # Metadata-only damage is repairable; it must not read as failure.
            add(
                "warning",
                "FILESYSTEM_METADATA_DAMAGED",
                f"{message} (the data itself may be intact)",
                source="filesystem",
                original_code=code,
            )
        else:
            add(severity, code, message, source="filesystem")
    for finding in (partition_findings or []):
        add(
            getattr(finding, "severity", "warning"),
            getattr(finding, "code", "PARTITION_ISSUE"),
            getattr(finding, "message", ""),
            source="partition",
        )

    # ---------------- Performance signal ----------------------------------
    if read_speed_bps and expected_speed_bps and read_speed_bps < expected_speed_bps * 0.15:
        add(
            "warning",
            "READ_SPEED_LOW",
            f"Read speed {read_speed_bps / 1e6:.1f} MB/s is far below what this interface "
            f"should sustain (~{expected_speed_bps / 1e6:.0f} MB/s).",
            source="performance",
        )

    # ---------------- Verdict --------------------------------------------
    verdict = Health.UNKNOWN if not findings else Health.EXCELLENT
    if not findings:
        verdict = Health.UNKNOWN
    for finding in findings:
        if finding.severity == "critical":
            verdict = _escalate(verdict, Health.FAILING if _is_failure_evidence(finding) else Health.CRITICAL)
        elif finding.severity == "error":
            verdict = _escalate(verdict, Health.CRITICAL)
        elif finding.severity == "warning":
            verdict = _escalate(verdict, Health.WARNING)
    if verdict == Health.UNKNOWN and findings:
        verdict = Health.GOOD
    if findings and all(f.severity == "info" for f in findings):
        verdict = Health.EXCELLENT if confidence == "high" else Health.GOOD

    # A clean surface scan plus clean metadata and no SMART: GOOD, not EXCELLENT.
    permanent_suspected = verdict == Health.FAILING
    confirmed = (
        (smart is not None and smart.available and smart.health_passed is False)
        or surface_confirmed
        or (capacity_verified is False)
        or (zero_capacity and bool(reported_capacity))
    )

    if verdict == Health.FAILING and not confirmed:
        # Downgrade "suspected" to CRITICAL unless something concrete confirms it.
        verdict = Health.CRITICAL

    explanation.extend(_explain(verdict, findings))

    return HealthReport(
        verdict=verdict,
        findings=findings,
        health_confidence=confidence if verdict != Health.UNKNOWN else "low",
        metrics=metrics,
        explanation=explanation,
        permanent_failure_suspected=permanent_suspected,
        confirmed_failure=confirmed,
    )


_FAILURE_CODES = {
    "SMART_HEALTH_FAILED",
    "SMART_ATTR_5_HIGH",       # >= 10 physically remapped sectors
    "SMART_NVME_CRITICAL",     # controller-declared critical warning
    "SMART_SPARE_LOW",         # spare-block pool exhausted
    "CAPACITY_MISMATCH",       # counterfeit capacity (data loss is certain)
    "ZERO_CAPACITY",
    "ZERO_WHILE_REPORTING_CAPACITY",
    "SURFACE_READ_ERRORS_CONFIRMED",  # failing regions proven twice
}
#: Pending/uncorrectable sector counts are *serious* but a handful can still be
#: remapped by the controller, so they alone never constitute proof of death.
_ESCALATING_ONLY_CODES = {
    "SMART_ATTR_187_HIGH",
    "SMART_ATTR_197_HIGH",
    "SMART_ATTR_198_HIGH",
    "SMART_ATTR_188_HIGH",
    "SMART_ATTR_199_HIGH",
    "SMART_ERROR_LOG_LARGE",
}


def _is_failure_evidence(finding: Finding) -> bool:
    return finding.code in _FAILURE_CODES


def _explain(verdict: Health, findings: list[Finding]) -> list[str]:
    notes: list[str] = []
    if verdict == Health.UNKNOWN:
        notes.append("No usable hardware health information was reported by this device.")
        notes.append("This is normal for many USB flash drives and SD cards: their bridge chip does not expose SMART.")
        notes.append("Run a surface scan and a capacity test to gain real evidence.")
    elif verdict == Health.EXCELLENT:
        notes.append("All performed checks passed and the device supplied health data confirming it is healthy.")
    elif verdict == Health.GOOD:
        notes.append("No errors were found, but the device does not provide full hardware health data.")
    elif verdict == Health.WARNING:
        notes.append("Some problems were found. They may be repairable, but back up important files first.")
    elif verdict == Health.CRITICAL:
        notes.append("Serious problems were found. Treat the device as unreliable.")
    elif verdict == Health.FAILING:
        notes.append("There is strong evidence of a physical hardware failure that software cannot repair.")
    codes = {f.code for f in findings}
    if "FILESYSTEM_METADATA_DAMAGED" in codes:
        notes.append("The file-system metadata is damaged, but this is typically repairable without data loss.")
    return notes


def merge_health(*reports: HealthReport) -> HealthReport:
    """Combine partial reports (e.g. quick + deep scan) keeping the worst verdict."""
    valid = [r for r in reports if r is not None]
    if not valid:
        return evaluate_health()
    worst = max(valid, key=lambda r: int(r.verdict))
    merged = HealthReport(
        verdict=worst.verdict,
        findings=[f for r in valid for f in r.findings],
        health_confidence=max((r.health_confidence for r in valid), key=lambda c: ["low", "medium", "high"].index(c)),
        metrics={k: v for r in valid for k, v in r.metrics.items()},
        explanation=[e for r in valid for e in r.explanation],
        permanent_failure_suspected=any(r.permanent_failure_suspected for r in valid),
        confirmed_failure=any(r.confirmed_failure for r in valid),
    )
    return merged
