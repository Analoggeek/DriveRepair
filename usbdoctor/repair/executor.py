"""Executes approved repair plans with full auditing and verification."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from usbdoctor.core.logging import AuditLog
from usbdoctor.core.model import DeviceRef, DeviceSignature
from usbdoctor.repair.base import RepairPlan, RepairResult, RepairStatus, RunContext
from usbdoctor.security.consent import ConsentBroker
from usbdoctor.security.policy import SafetyPolicy


@dataclass
class ExecutionReport:
    device_path: str
    started_at: float
    results: list[tuple[RepairPlan, RepairResult]] = field(default_factory=list)
    aborted: bool = False
    abort_reason: str = ""

    @property
    def succeeded(self) -> int:
        return sum(1 for _, r in self.results if r.status == RepairStatus.SUCCEEDED)

    @property
    def failed(self) -> int:
        return sum(1 for _, r in self.results if r.status == RepairStatus.FAILED)

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_path": self.device_path,
            "started_at": self.started_at,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "aborted": self.aborted,
            "abort_reason": self.abort_reason,
            "steps": [
                {
                    "plan": plan.to_dict(),
                    "result": result.to_dict(),
                }
                for plan, result in self.results
            ],
        }


class RepairExecutor:
    """Runs plans in order, honouring consent, privileges and device identity."""

    def __init__(
        self,
        *,
        policy: SafetyPolicy,
        broker: ConsentBroker,
        audit: Optional[AuditLog] = None,
    ) -> None:
        self.policy = policy
        self.broker = broker
        self.audit = audit

    def execute(
        self,
        device: DeviceRef,
        plans: list[RepairPlan],
        *,
        signature: DeviceSignature,
        consent_token: Optional[str] = None,
        dry_run: bool = False,
        emitter: Optional[Callable[[str, Optional[dict]], None]] = None,
        stop_on_failure: bool = False,
    ) -> ExecutionReport:
        report = ExecutionReport(device_path=device.path, started_at=time.time())
        # Any modifying plan needs one valid, device-bound consent token.
        needs_consent = [p for p in plans if p.requires_consent and p.risk.value != "none"]
        if needs_consent and not dry_run:
            if not consent_token:
                raise_permission = True
            else:
                self.broker.consume(consent_token, device_path=device.path, signature=signature)
                raise_permission = False
            if raise_permission:
                from usbdoctor.core.errors import UnsafeOperationError

                raise UnsafeOperationError(
                    "Repair cannot start without your explicit approval.",
                    hint="Approve the listed operations in the UI so a consent token can be issued.",
                )

        for plan in plans:
            if plan.runner is None:
                report.results.append(
                    (plan, RepairResult.skipped("This operation has no executable implementation on this "
                                                "platform."))
                )
                continue
            if emitter:
                emitter("step_start", {"operation_id": plan.operation_id, "title": plan.title, "risk": plan.risk.value})
            ctx = RunContext(
                device=device,
                signature=signature,
                policy=self.policy,
                audit=self.audit,
                emitter=emitter,
                dry_run=dry_run,
            )
            started = time.time()
            try:
                result = plan.runner(ctx)
            except Exception as exc:  # never let one bad plugin stop the run
                result = RepairResult.failure(f"{plan.title} failed: {exc}")
            result.duration_s = time.time() - started
            report.results.append((plan, result))
            if emitter:
                emitter(
                    "step_end",
                    {
                        "operation_id": plan.operation_id,
                        "status": result.status.value,
                        "message": result.message,
                        "verified": result.verified,
                    },
                )
            if self.audit:
                self.audit.record(
                    f"repair:{plan.operation_id}",
                    device_path=device.path,
                    serial=device.serial,
                    outcome=result.status.value,
                    detail=result.to_dict(),
                )
            if result.status == RepairStatus.FAILED and stop_on_failure:
                report.aborted = True
                report.abort_reason = f"stopped after failure in '{plan.title}'"
                break
        return report
