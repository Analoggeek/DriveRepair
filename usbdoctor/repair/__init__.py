"""Repair planning and execution."""

from usbdoctor.repair.base import (
    Effect,
    RepairPlan,
    RepairResult,
    RepairStatus,
    Risk,
    RunContext,
)
from usbdoctor.repair.executor import ExecutionReport, RepairExecutor
from usbdoctor.repair.planner import ScanEvidence, collect_evidence, plan_repairs

__all__ = [
    "Effect",
    "RepairPlan",
    "RepairResult",
    "RepairStatus",
    "Risk",
    "RunContext",
    "ExecutionReport",
    "RepairExecutor",
    "ScanEvidence",
    "collect_evidence",
    "plan_repairs",
]
