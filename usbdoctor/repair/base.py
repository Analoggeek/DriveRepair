"""Repair operation model: plans, verification and results.

Every repair is expressed as an :class:`Operation` with a *plan* that lists the
exact effects and risks **before** anything is written.  Execution always runs
through :class:`usbdoctor.security.writer_guard.GuardedWriter`, and each
operation must implement ``verify()`` — a repair that cannot be proven to have
worked is reported as failed, not as "probably fine".
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

from usbdoctor.core.model import DeviceRef


class Risk(str, Enum):
    NONE = "none"  # read-only / metadata rewrite with verified backup
    LOW = "low"  # rewrites metadata, data untouched
    MEDIUM = "medium"  # may lose the volume's directory structure
    HIGH = "high"  # can destroy data
    CRITICAL = "critical"  # makes the device unusable if it fails


class RepairStatus(str, Enum):
    PLANNED = "planned"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PARTIAL = "partial"
    REQUIRED_TOOL_MISSING = "tool_missing"
    NOT_APPLICABLE = "not_applicable"


@dataclass
class Effect:
    """One concrete change a repair will make."""

    description: str
    offset: Optional[int] = None
    length: Optional[int] = None
    reversible: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "description": self.description,
            "offset": self.offset,
            "length": self.length,
            "reversible": self.reversible,
        }


@dataclass
class RepairPlan:
    operation_id: str
    title: str
    summary: str
    risk: Risk
    requires_admin: bool = True
    requires_consent: bool = True
    effects: list[Effect] = field(default_factory=list)
    data_loss_warning: Optional[str] = None
    rollback: Optional[str] = None
    tool_requirement: Optional[str] = None
    evidence: dict[str, Any] = field(default_factory=dict)
    #: Implementation of the actual repair (set by the factory, never by the UI)
    runner: Optional[Callable[["RunContext"], "RepairResult"]] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "title": self.title,
            "summary": self.summary,
            "risk": self.risk.value,
            "requires_admin": self.requires_admin,
            "requires_consent": self.requires_consent,
            "effects": [e.to_dict() for e in self.effects],
            "data_loss_warning": self.data_loss_warning,
            "rollback": self.rollback,
            "tool_requirement": self.tool_requirement,
            "evidence": self.evidence,
        }


@dataclass
class RunContext:
    """Everything a repair runner is allowed to use."""

    device: DeviceRef
    signature: Any
    policy: Any
    audit: Any
    emitter: Optional[Callable[[str, Optional[dict]], None]] = None
    dry_run: bool = False
    options: dict[str, Any] = field(default_factory=dict)

    def log(self, message: str, *, level: str = "info", **extra: Any) -> None:
        if self.emitter:
            self.emitter("log", {"message": message, "level": level, **extra})

    def progress(self, current: int, total: int, stage: str = "") -> None:
        if self.emitter:
            pct = (current / total * 100.0) if total else 0.0
            self.emitter("progress", {"current": current, "total": total, "percent": round(pct, 1), "stage": stage})


@dataclass
class RepairResult:
    status: RepairStatus
    message: str
    verified: Optional[bool] = None
    verification_note: str = ""
    bytes_written: int = 0
    details: dict[str, Any] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    duration_s: float = 0.0

    @classmethod
    def success(cls, message: str, **kwargs: Any) -> "RepairResult":
        return cls(status=RepairStatus.SUCCEEDED, message=message, verified=kwargs.pop("verified", True), **kwargs)

    @classmethod
    def failure(cls, message: str, **kwargs: Any) -> "RepairResult":
        return cls(status=RepairStatus.FAILED, message=message, verified=False, **kwargs)

    @classmethod
    def skipped(cls, message: str, **kwargs: Any) -> "RepairResult":
        return cls(status=RepairStatus.SKIPPED, message=message, **kwargs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "message": self.message,
            "verified": self.verified,
            "verification_note": self.verification_note,
            "bytes_written": self.bytes_written,
            "details": self.details,
            "started_at": self.started_at,
            "duration_s": round(self.duration_s, 3),
        }
