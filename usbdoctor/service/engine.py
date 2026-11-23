"""Application service: the single orchestration point used by both frontends.

Holds the safety-critical singletons (policy, consent broker, audit log, history)
and exposes coarse operations that emit fine-grained progress events:

    scan_device()      read-only diagnosis (safe mode by default)
    plan_for()         what *could* be repaired, with risks, for user review
    execute_repairs()  run approved plans (consent token required)
    image_device()     resumable backup image
    recover_device()   carve files / walk the file system
    capacity_test()    counterfeit-drive verification
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from usbdoctor.ai.assistant import DiagnosticAssistant, Diagnosis
from usbdoctor.analysis.filesystems.detect import analyze_volume
from usbdoctor.analysis.health import HealthReport, evaluate_health
from usbdoctor.analysis.partitions.tables import (
    analyze_gpt,
    parse_gpt_entries,
    parse_gpt_header,
    parse_mbr,
)
from usbdoctor.analysis.probe import check_reported_capacity, scan_surface
from usbdoctor.analysis.smart.reader import read_smart
from usbdoctor.core.errors import UsbDoctorError
from usbdoctor.core.events import EventBus
from usbdoctor.core.io import DiskReader
from usbdoctor.core.logging import AuditLog, default_state_dir
from usbdoctor.core.model import DeviceRef, DeviceSignature
from usbdoctor.core.platform import detect_system, is_admin
from usbdoctor.core.util import human_size, iso_now, which
from usbdoctor.db.store import HistoryStore
from usbdoctor.devices import enumerate_devices
from usbdoctor.knowledge.online import OnlineResearcher
from usbdoctor.performance.benchmark import benchmark_read
from usbdoctor.performance.capacity import CapacityTester
from usbdoctor.recovery.carver import Carver
from usbdoctor.recovery.fat_recovery import FatRecovery
from usbdoctor.recovery.imaging import ImageCreator, create_image_ddrescue, ddrescue_available
from usbdoctor.repair.base import RepairPlan, RepairStatus
from usbdoctor.repair.executor import ExecutionReport, RepairExecutor
from usbdoctor.repair.planner import ScanEvidence, collect_evidence, plan_repairs
from usbdoctor.security.consent import ConsentBroker
from usbdoctor.security.policy import PolicySettings, SafetyPolicy
from usbdoctor.security.writer_guard import compute_signature
from usbdoctor.version import APP_NAME, __version__

LOG = logging.getLogger(__name__)

MODES = ("safe", "smart", "advanced")


@dataclass
class ScanOptions:
    """What to run during a scan.  Every default is non-destructive."""

    mode: str = "smart"
    read_smart: bool = True
    surface_scan: bool = True
    surface_limit_bytes: Optional[int] = None
    surface_time_budget_s: Optional[float] = 300.0
    benchmark: bool = True
    verify_capacity: bool = True
    deep_usb_smart: bool = True
    analyze_filesystem: bool = True

    def validate(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")


@dataclass
class ScanResult:
    device: DeviceRef
    signature: DeviceSignature
    health: HealthReport
    evidence: ScanEvidence
    plans: list[RepairPlan]
    diagnosis: Diagnosis
    smart: Optional[dict[str, Any]] = None
    surface: Optional[dict[str, Any]] = None
    filesystem: Optional[dict[str, Any]] = None
    partition: Optional[dict[str, Any]] = None
    performance: Optional[dict[str, Any]] = None
    capacity: Optional[dict[str, Any]] = None
    limitations: list[str] = field(default_factory=list)
    scan_id: Optional[int] = None
    started_at: float = 0.0
    duration_s: float = 0.0
    mode: str = "smart"

    def to_dict(self) -> dict[str, Any]:
        return {
            "device": self.device.to_dict(),
            "signature": self.signature.to_dict(),
            "health": self.health.to_dict(),
            "diagnosis": self.diagnosis.to_dict(),
            "plans": [p.to_dict() for p in self.plans],
            "smart": self.smart,
            "surface": self.surface,
            "filesystem": self.filesystem,
            "partition": self.partition,
            "performance": self.performance,
            "capacity": self.capacity,
            "limitations": self.limitations,
            "scan_id": self.scan_id,
            "duration_s": round(self.duration_s, 2),
            "mode": self.mode,
            "evidence_codes": sorted(self.evidence.codes()),
        }


class DoctorService:
    """Stateful application service (one instance per process)."""

    def __init__(
        self,
        *,
        state_dir: Optional[Path] = None,
        bus: Optional[EventBus] = None,
        settings: Optional[PolicySettings] = None,
    ) -> None:
        # Accept str or Path: callers (CLI, tests, the desktop shell) all pass
        # different things and a TypeError deep in a scan would be confusing.
        self.state_dir = Path(state_dir) if state_dir else default_state_dir()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.bus = bus or EventBus()
        self.policy = SafetyPolicy(settings or PolicySettings())
        self.consent = ConsentBroker()
        self.audit = AuditLog(self.state_dir / "audit.jsonl")
        self.history = HistoryStore(self.state_dir / "history.db")
        self.executor = RepairExecutor(policy=self.policy, broker=self.consent, audit=self.audit)
        self.assistant = DiagnosticAssistant()
        self.researcher = OnlineResearcher(offline=bool(self.history.get_setting("offline_mode", False)))
        self._lock = threading.Lock()
        self._last_scan: dict[str, ScanResult] = {}
        self._jobs: dict[str, dict[str, Any]] = {}
        self._cancels: dict[str, threading.Event] = {}
        self._offline_filesystems = os.environ.get("USBDOCTOR_OFFLINE_FS") == "1"

    # ------------------------------------------------------------------ jobs
    def start_job(self, kind: str, *, device_path: str = "") -> str:
        job_id = f"{kind}-{int(time.time() * 1000)}-{os.getpid()}"
        self._jobs[job_id] = {
            "id": job_id,
            "kind": kind,
            "device_path": device_path,
            "started_at": iso_now(),
            "status": "running",
            "progress": 0.0,
            "stage": "starting",
            "result": None,
            "error": None,
        }
        self._cancels[job_id] = threading.Event()
        self.bus.publish(job_id, "job_started", {"kind": kind, "device_path": device_path})
        return job_id

    def finish_job(self, job_id: str, *, result: Any = None, error: Optional[str] = None) -> None:
        job = self._jobs.get(job_id)
        if job is None:
            return
        job["status"] = "failed" if error else "completed"
        job["result"] = result
        job["error"] = error
        job["finished_at"] = iso_now()
        job["progress"] = 100.0 if not error else job.get("progress", 0.0)
        self.bus.publish(job_id, "job_finished", {"status": job["status"], "error": error})
        self._cancels.pop(job_id, None)

    def job(self, job_id: str) -> Optional[dict[str, Any]]:
        return self._jobs.get(job_id)

    def jobs(self) -> list[dict[str, Any]]:
        return sorted(self._jobs.values(), key=lambda j: j["started_at"], reverse=True)

    def cancel(self, job_id: str) -> bool:
        event = self._cancels.get(job_id)
        if event is None:
            return False
        event.set()
        self.bus.publish(job_id, "cancel_requested", {})
        return True

    def cancelled(self, job_id: str) -> bool:
        event = self._cancels.get(job_id)
        return bool(event and event.is_set())

    def _emitter(self, job_id: str) -> Callable[[str, Optional[dict]], None]:
        def emit(type_: str, payload: Optional[dict[str, Any]] = None) -> None:
            payload = payload or {}
            if type_ == "progress":
                job = self._jobs.get(job_id)
                if job is not None:
                    job["progress"] = payload.get("percent", job.get("progress", 0.0))
                    job["stage"] = payload.get("stage", job.get("stage", ""))
            self.bus.publish(job_id, type_, payload)

        return emit

    # -------------------------------------------------------------- devices
    def devices(self, *, refresh_policy: bool = True) -> dict[str, Any]:
        if refresh_policy:
            self.policy.refresh()
        result = enumerate_devices(include_internal=True)
        payload = {
            "devices": [self._device_payload(d) for d in result.devices],
            "warnings": result.warnings,
            "source": result.source,
            "is_admin": is_admin(),
            "platform": detect_system(),
            "policy": self.policy.describe(),
        }
        return payload

    def _device_payload(self, device: DeviceRef) -> dict[str, Any]:
        data = device.to_dict()
        last = self.history.last_scan_for(device.path)
        data["last_scan"] = last
        data["can_write"] = self._can_write(device)
        try:
            data["signature"] = compute_signature(device.path, size_bytes=device.size_bytes).to_dict()
        except Exception:
            data["signature"] = None
        return data

    def _can_write(self, device: DeviceRef) -> tuple[bool, Optional[str]]:
        try:
            self.policy.check_writable(device, "repair")
            return True, None
        except UsbDoctorError as exc:
            return False, exc.user_message

    def device(self, path: str) -> Optional[DeviceRef]:
        for candidate in enumerate_devices(include_internal=True).devices:
            if candidate.path == path:
                return candidate
        # Allow analysing a disk image directly (used by tests and power users).
        if os.path.isfile(path):
            return DeviceRef(
                path=path,
                kind="image",
                platform=detect_system(),
                size_bytes=os.path.getsize(path),
                sector_size=512,
            )
        return None

    # ------------------------------------------------------------------ scan
    def scan(self, device_path: str, options: Optional[ScanOptions] = None) -> ScanResult:
        """Full read-only diagnosis. Never writes to the device."""
        options = options or ScanOptions()
        options.validate()
        started = time.time()
        cancel = threading.Event()

        device = self.device(device_path)
        if device is None:
            raise UsbDoctorError(
                f"{device_path} is not currently connected.",
                hint="Re-plug the device and refresh the device list.",
            )
        job_id = self.start_job("scan", device_path=device_path)
        self._cancels[job_id] = cancel
        emit = self._emitter(job_id)
        limitations: list[str] = list(device.limitations)
        result_holder: dict[str, Any] = {}

        def run() -> None:
            try:
                result = self._run_scan(device, options, job_id, cancel, emit, limitations)
                result_holder["result"] = result
                self.complete_scan(job_id, result, started)
            except Exception as exc:  # surface every failure to the UI
                LOG.exception("scan failed")
                emit("error", {"message": str(exc)})
                self.finish_job(job_id, error=str(exc))

        # Scans run synchronously for the CLI and in a thread for the web UI;
        # the API always calls scan_async, so this composes cleanly.
        run()
        if "result" not in result_holder:
            raise UsbDoctorError(
                self._jobs[job_id].get("error") or "Scan failed for an unknown reason."
            )
        return result_holder["result"]

    def scan_async(self, device_path: str, options: Optional[ScanOptions] = None) -> str:
        options = options or ScanOptions()
        device = self.device(device_path)
        if device is None:
            raise UsbDoctorError(f"{device_path} is not currently connected.")
        job_id = self.start_job("scan", device_path=device_path)
        cancel = self._cancels[job_id]
        emit = self._emitter(job_id)
        limitations: list[str] = list(device.limitations)
        started = time.time()

        def worker() -> None:
            try:
                result = self._run_scan(device, options, job_id, cancel, emit, limitations)
                self.complete_scan(job_id, result, started)
            except Exception as exc:
                LOG.exception("async scan failed")
                emit("error", {"message": str(exc)})
                self.finish_job(job_id, error=str(exc))

        thread = threading.Thread(target=worker, name=f"scan-{job_id}", daemon=True)
        thread.start()
        return job_id

    def complete_scan(self, job_id: str, result: ScanResult, started: float) -> None:
        result.duration_s = time.time() - started
        self._last_scan[result.device.path] = result
        scan_id = self.history.start_scan(
            device_path=result.device.path,
            device_name=result.device.display_name,
            serial_hash=_serial_hash(result.device.serial),
            size_bytes=result.device.size_bytes,
            mode=result.mode,
        )
        result.scan_id = scan_id
        self.history.finish_scan(
            scan_id,
            verdict=result.health.verdict.name,
            confidence=result.health.health_confidence,
            report=result.to_dict(),
        )
        self.finish_job(job_id, result=result.to_dict())

    def _run_scan(
        self,
        device: DeviceRef,
        options: ScanOptions,
        job_id: str,
        cancel: threading.Event,
        emit: Callable[[str, Optional[dict]], None],
        limitations: list[str],
    ) -> ScanResult:
        emit("stage", {"stage": "identity", "message": "Reading device identity"})
        signature = compute_signature(device.path, size_bytes=device.size_bytes)

        # ---- partition table -------------------------------------------------
        emit("stage", {"stage": "partition", "message": "Inspecting the partition table"})
        partition_info: dict[str, Any] = {}
        partition_issues = []
        gpt_backup_missing = False
        if device.size_bytes and device.kind != "image" or (device.size_bytes or 0) > 0:
            try:
                with DiskReader(device.path, sector_size=device.sector_size) as reader:
                    sector = reader.read(0, 512)
                    mbr = parse_mbr(sector, device.size_bytes or 0, sector_size=reader.sector_size)
                    partition_issues = list(mbr.issues)
                    partition_info = mbr.to_dict()
                    if mbr.scheme == "gpt":
                        primary = parse_gpt_header(reader.read(512, 512))
                        entries_data = (
                            reader.read(primary["entries_lba"] * 512, primary["num_entries"] * primary["entry_size"])
                            if primary
                            else b""
                        )
                        entries, crc_ok = parse_gpt_entries(entries_data, primary) if primary else ([], False)
                        backup = (
                            parse_gpt_header(reader.read((device.size_bytes or 0) - 512, 512))
                        )
                        backup_entries, backup_crc = (
                            parse_gpt_entries(reader.read(backup["entries_lba"] * 512, 128 * 128), backup)
                            if backup
                            else ([], False)
                        )
                        gpt = analyze_gpt(
                            primary,
                            entries,
                            crc_ok,
                            backup_header=backup,
                            backup_entries=backup_entries,
                            backup_crc_ok=backup_crc,
                            disk_size=device.size_bytes or 0,
                        )
                        partition_issues = list(gpt.issues)
                        gpt_backup_missing = backup is None
                        partition_info = gpt.to_dict()
                        partition_info["scheme"] = "gpt"
            except Exception as exc:
                limitations.append(f"Partition table could not be read: {exc}")

        # ---- file system -----------------------------------------------------
        filesystem_info: Optional[dict[str, Any]] = None
        fs_issues: list[Any] = []
        detected_fs = "unknown"
        if options.analyze_filesystem:
            emit("stage", {"stage": "filesystem", "message": "Verifying file-system structures"})
            base_offset = 0
            volume_size = device.size_bytes
            if device.kind == "partition" and device.partitions:
                base_offset = device.partitions[0].offset_bytes
                volume_size = device.partitions[0].size_bytes
            try:
                with DiskReader(device.path, sector_size=device.sector_size) as reader:
                    analysis = analyze_volume(reader, base_offset, volume_size=volume_size)
                    first = None
                    if analysis.filesystem == "unknown":
                        # Prefer what the OS enumerated; fall back to our parsed GPT.
                        for entry in device.partitions:
                            if entry.offset_bytes and entry.size_bytes:
                                first = (entry.offset_bytes, entry.size_bytes)
                                break
                        if first is None:
                            for entry in partition_info.get("entries") or []:
                                offset = entry.get("offset") or 0
                                size = entry.get("size") or 0
                                if offset and size:
                                    first = (offset, size)
                                    break
                    if first is not None:
                        if True:
                            analysis = analyze_volume(
                                reader, first[0], volume_size=first[1]
                            )
                            limitations.append(
                                f"File system analysed at partition offset {human_size(first[0])}."
                            )
                    filesystem_info = analysis.to_dict()
                    fs_issues = list(analysis.issues)
                    detected_fs = analysis.filesystem
            except Exception as exc:
                limitations.append(f"File-system analysis was not possible: {exc}")

        # ---- SMART -----------------------------------------------------------
        smart_payload: Optional[dict[str, Any]] = None
        smart_result = None
        if options.read_smart and device.kind != "image":
            emit("stage", {"stage": "smart", "message": "Querying SMART / health counters"})
            try:
                smart_result = read_smart(
                    device.path, bus=device.bus, deep_usb=options.deep_usb_smart
                )
                smart_payload = smart_result.to_dict()
                if not smart_result.available:
                    limitations.append(
                        "SMART is unavailable: " + (smart_result.reason or "not reported by this device.")
                    )
            except Exception as exc:
                limitations.append(f"SMART query failed: {exc}")
        elif device.kind == "image":
            limitations.append("SMART cannot be read from an image file (no hardware behind it).")

        # ---- surface scan ----------------------------------------------------
        surface_payload: Optional[dict[str, Any]] = None
        surface_read_errors = 0
        surface_error_bytes = 0
        surface_scanned = 0
        read_speed_bps: Optional[float] = None
        if options.surface_scan and (device.size_bytes or 0) > 0:
            emit("stage", {"stage": "surface", "message": "Scanning for unreadable sectors"})
            try:
                with DiskReader(device.path, sector_size=device.sector_size) as reader:
                    surface = scan_surface(
                        reader,
                        length=options.surface_limit_bytes,
                        deadline_s=options.surface_time_budget_s,
                        progress=lambda cur, total, stage: emit(
                            "progress",
                            {
                                "percent": round(cur / total * 100, 1) if total else 0,
                                "stage": "surface scan",
                                "current": cur,
                                "total": total,
                            },
                        ),
                    )
                    surface_payload = surface.to_dict()
                    surface_read_errors = len(surface.read_errors)
                    surface_error_bytes = sum(region.length for region in surface.read_errors)
                    surface_scanned = surface.scanned_bytes
                    read_speed_bps = surface.read_speed_bps
                    if surface.aborted:
                        limitations.append(f"Surface scan was partial: {surface.abort_reason}")
            except Exception as exc:
                limitations.append(f"Surface scan failed: {exc}")

        # ---- capacity verification (read-only) --------------------------------
        capacity_payload: Optional[dict[str, Any]] = None
        capacity_verified: Optional[bool] = None
        if options.verify_capacity and (device.size_bytes or 0) > 0:
            emit("stage", {"stage": "capacity", "message": "Verifying reported capacity"})
            try:
                with DiskReader(device.path, sector_size=device.sector_size) as reader:
                    capacity_payload = check_reported_capacity(reader)
                    capacity_payload["claimed"] = device.size_bytes
                    if capacity_payload.get("readable_tail") is False:
                        capacity_verified = False
                    elif surface_scanned and surface_read_errors == 0 and options.surface_limit_bytes is None:
                        capacity_verified = True
            except Exception as exc:
                limitations.append(f"Capacity check failed: {exc}")

        # ---- benchmark -------------------------------------------------------
        performance_payload: Optional[dict[str, Any]] = None
        if options.benchmark and (device.size_bytes or 0) > 0:
            emit("stage", {"stage": "benchmark", "message": "Measuring read speed"})
            try:
                with DiskReader(device.path, sector_size=device.sector_size) as reader:
                    bench = benchmark_read(reader, total_bytes=min(16 << 20, device.size_bytes or 0))
                    performance_payload = bench.to_dict()
                    if read_speed_bps is None and bench.mbps:
                        read_speed_bps = bench.mbps * 1e6
            except Exception as exc:
                limitations.append(f"Read benchmark failed: {exc}")

        if cancel.is_set():
            limitations.append("Scan was cancelled; results are partial.")

        # ---- evidence, health, plans, diagnosis -------------------------------
        emit("stage", {"stage": "analysis", "message": "Evaluating evidence"})
        device_snapshot = self.device(device.path) or device
        evidence = ScanEvidence(
            device=device_snapshot,
            filesystem=detected_fs,
            filesystem_issues=fs_issues,
            partition_issues=partition_issues,
            gpt_backup_missing=gpt_backup_missing,
            partition_scheme=partition_info.get("scheme", "none"),
        )
        evidence.detected_fs = detected_fs if detected_fs != "unknown" else None
        # Re-use the full evidence collector for patch availability, but keep the
        # scan's own findings (they may cover a partition rather than the disk).
        try:
            full_evidence = collect_evidence(device_snapshot)
            evidence.filesystem_patches = full_evidence.filesystem_patches
            evidence.detected_fs = evidence.detected_fs or full_evidence.detected_fs
            evidence.volume_start_hint = full_evidence.volume_start_hint
            evidence.mbr_signature_ok = full_evidence.mbr_signature_ok
            evidence.mbr_has_partitions = full_evidence.mbr_has_partitions
            if full_evidence.filesystem != "unknown":
                detected_fs = full_evidence.filesystem
                evidence.filesystem = detected_fs
        except Exception:
            pass

        health = evaluate_health(
            smart=smart_result,
            surface_read_errors=surface_read_errors,
            surface_error_bytes=surface_error_bytes,
            surface_scanned_bytes=surface_scanned,
            filesystem_findings=fs_issues,
            partition_findings=partition_issues,
            capacity_verified=capacity_verified,
            capacity_claimed=device.size_bytes,
            capacity_real=(capacity_payload or {}).get("reported"),
            disconnect_count=0,
            write_protected=bool(device.read_only),
            zero_capacity=not device.size_bytes,
            reported_capacity=device.size_bytes,
            read_speed_bps=read_speed_bps,
            connection_speed_label=device.usb_speed,
        )
        plans = plan_repairs(evidence, mode=options.mode)
        diagnosis = self.assistant.diagnose(
            device_name=device.display_name,
            health=health,
            filesystem=detected_fs,
            evidence_codes=sorted(evidence.codes()),
            plans=plans,
            brand=device.vendor,
            vid=device.vid,
            capacity_claimed=device.size_bytes,
            capacity_real=(capacity_payload or {}).get("reported"),
            smart_available=bool(smart_payload and smart_payload.get("available")),
            filesystem_analysis=filesystem_info,
            partition_analysis=partition_info,
        )
        emit("stage", {"stage": "done", "message": "Scan complete"})
        return ScanResult(
            device=device_snapshot,
            signature=signature,
            health=health,
            evidence=evidence,
            plans=plans,
            diagnosis=diagnosis,
            smart=smart_payload,
            surface=surface_payload,
            filesystem=filesystem_info,
            partition=partition_info,
            performance=performance_payload,
            capacity=capacity_payload,
            limitations=limitations,
            started_at=time.time(),
            mode=options.mode,
        )

    # ----------------------------------------------------------------- plans
    def plan(self, device_path: str, *, mode: str = "smart") -> dict[str, Any]:
        device = self.device(device_path)
        if device is None:
            raise UsbDoctorError(f"{device_path} is not currently connected.")
        evidence = collect_evidence(device)
        plans = plan_repairs(evidence, mode=mode)
        return {
            "device": device.to_dict(),
            "mode": mode,
            "evidence_codes": sorted(evidence.codes()),
            "plans": [p.to_dict() for p in plans],
            "signature": compute_signature(device_path, size_bytes=device.size_bytes).to_dict(),
        }

    def approve(
        self,
        device_path: str,
        *,
        operation_ids: Optional[list[str]] = None,
        mode: str = "smart",
    ) -> dict[str, Any]:
        """Register the user's approval and return a single-use consent token."""
        plan_payload = self.plan(device_path, mode=mode)
        plans = [p for p in plan_payload["plans"] if not operation_ids or p["operation_id"] in operation_ids]
        if not plans:
            raise UsbDoctorError("No repair operations were selected.")
        resolved = self.device(device_path)
        signature = compute_signature(device_path, size_bytes=(resolved.size_bytes if resolved else None))
        risks = [f"{p['title']}: {p['risk']} risk" for p in plans]
        token = self.consent.grant(
            operation_id=",".join(p["operation_id"] for p in plans),
            device_path=device_path,
            signature=signature,
            risks=risks,
        )
        self.audit.record(
            "consent:granted",
            device_path=device_path,
            outcome="ok",
            detail={"operations": [p["operation_id"] for p in plans], "risks": risks},
        )
        return {
            "consent_token": token.token,
            "operations": [p["operation_id"] for p in plans],
            "risks": risks,
            "expires_in_seconds": int(token.expires_at - time.time()),
        }

    def execute(
        self,
        device_path: str,
        *,
        consent_token: Optional[str],
        mode: str = "smart",
        operation_ids: Optional[list[str]] = None,
        dry_run: bool = False,
        stop_on_failure: bool = False,
    ) -> dict[str, Any]:
        device = self.device(device_path)
        if device is None:
            raise UsbDoctorError(f"{device_path} is not currently connected.")
        job_id = self.start_job("repair", device_path=device_path)
        emit = self._emitter(job_id)
        evidence = collect_evidence(device)
        plans = plan_repairs(evidence, mode=mode)
        if operation_ids:
            plans = [p for p in plans if p.operation_id in operation_ids]
        signature = compute_signature(device_path, size_bytes=device.size_bytes)
        try:
            report = self.executor.execute(
                device,
                plans,
                signature=signature,
                consent_token=consent_token,
                dry_run=dry_run,
                emitter=emit,
                stop_on_failure=stop_on_failure,
            )
        except Exception as exc:
            self.finish_job(job_id, error=str(exc))
            raise
        for plan, result in report.results:
            rollback_file = (result.details or {}).get("rollback_file")
            self.history.record_repair(
                scan_id=(self._last_scan.get(device.path).scan_id if self._last_scan.get(device.path) else None),
                device_path=device.path,
                operation_id=plan.operation_id,
                title=plan.title,
                risk=plan.risk.value,
                status=result.status.value,
                verified=result.verified,
                message=result.message,
                duration_s=result.duration_s,
                rollback_file=rollback_file,
            )
        payload = report.to_dict()
        payload["device"] = device.to_dict()
        self.finish_job(job_id, result=payload)
        return payload

    # -------------------------------------------------------------- recovery
    def image_device(
        self,
        device_path: str,
        output_path: str,
        *,
        prefer_ddrescue: bool = True,
    ) -> dict[str, Any]:
        device = self.device(device_path)
        if device is None:
            raise UsbDoctorError(f"{device_path} is not currently connected.")
        job_id = self.start_job("image", device_path=device_path)
        emit = self._emitter(job_id)
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        self.audit.record("image:start", device_path=device_path, detail={"output": output_path})
        try:
            if prefer_ddrescue and ddrescue_available():
                result = create_image_ddrescue(
                    device_path,
                    output_path,
                    progress=lambda c, t, s: emit(
                        "progress", {"percent": round(c / t * 100, 1) if t else 0, "stage": s}
                    ),
                )
            else:
                creator = ImageCreator(
                    device_path,
                    output_path,
                    total_bytes=device.size_bytes,
                    sector_size=device.sector_size,
                    progress=lambda c, t, s: emit(
                        "progress", {"percent": round(c / t * 100, 1) if t else 0, "stage": s}
                    ),
                )
                result = creator.run()
            payload = result.to_dict()
            self.history.record_recovery(
                device_path=device_path,
                method=f"image ({result.engine})",
                output_dir=str(Path(output_path).parent),
                files=1,
                bytes_written=result.good_bytes,
                details=payload,
            )
            self.audit.record("image:finish", device_path=device_path, outcome="ok", detail=payload)
            self.finish_job(job_id, result=payload)
            return payload
        except Exception as exc:
            self.audit.record("image:finish", device_path=device_path, outcome="error", detail={"error": str(exc)})
            self.finish_job(job_id, error=str(exc))
            raise

    def recover(
        self,
        device_path: str,
        *,
        method: str = "auto",
        output_dir: Optional[str] = None,
        image_path: Optional[str] = None,
        categories: Optional[list[str]] = None,
        include_deleted: bool = True,
        length: Optional[int] = None,
    ) -> dict[str, Any]:
        """Recover files.  Never writes to the source device."""
        device = self.device(device_path)
        if device is None:
            raise UsbDoctorError(f"{device_path} is not currently connected.")
        if output_dir is None:
            output_dir = str(self.state_dir / "recovered" / time.strftime("%Y%m%d-%H%M%S"))
        output = Path(output_dir).resolve()
        source = Path(device_path).resolve() if device.kind == "image" else None
        if source and (output == source or output in source.parents or source.parent == output):
            raise UsbDoctorError(
                "Recovered files may not be written next to or over the source device.",
                hint="Choose an output folder on a different drive.",
            )
        job_id = self.start_job("recovery", device_path=device_path)
        emit = self._emitter(job_id)
        self.audit.record("recovery:start", device_path=device_path, detail={"output": str(output)})
        target = image_path or device_path
        try:
            with DiskReader(target, sector_size=device.sector_size) as reader:
                payload: dict[str, Any]
                if method in ("auto", "filesystem"):
                    fat = FatRecovery(
                        reader,
                        str(output),
                        include_deleted=include_deleted,
                        progress=lambda n, total, stage: emit("progress", {"stage": stage, "percent": 0}),
                    ).recover()
                    payload = fat.to_dict()
                    payload["method"] = "filesystem"
                    # A destroyed boot sector means there is no walkable file system.
                    # 'auto' must then fall through to carving instead of returning
                    # an empty (and misleadingly successful) result.
                    if method == "auto" and fat.files_recovered == 0:
                        emit("stage", {"stage": "carving", "message": "No directory entries — carving content"})
                        scan_len = length or (reader.size() - 0)
                        carver = Carver(
                            reader,
                            str(output / "carved"),
                            categories=set(categories) if categories else None,
                            progress=lambda c, t, s: emit(
                                "progress", {"percent": round(c / t * 100, 1) if t else 0, "stage": "carving"}
                            ),
                        )
                        carved = carver.carve(length=scan_len)
                        payload["carved"] = carved.to_dict()
                        payload["files_recovered"] = len(carved.files)
                        payload["bytes_recovered"] = carved.bytes_written
                        # Report only what actually happened.
                        if fat.volume_type in ("unknown", ""):
                            payload["method"] = "carve"
                        elif fat.entries_seen:
                            payload["method"] = "filesystem+carve"
                        else:
                            payload["method"] = "carve"
                elif method == "carve":
                    carver = Carver(
                        reader,
                        str(output),
                        categories=set(categories) if categories else None,
                        progress=lambda c, t, s: emit(
                            "progress", {"percent": round(c / t * 100, 1) if t else 0, "stage": "carving"}
                        ),
                    )
                    carved = carver.carve(length=length)
                    payload = carved.to_dict()
                    payload["method"] = "carve"
                    payload["files_recovered"] = len(carved.files)
                    payload["bytes_recovered"] = carved.bytes_written
                else:
                    raise UsbDoctorError(f"Unknown recovery method: {method}")
            payload["output_dir"] = str(output)
            self.history.record_recovery(
                device_path=device_path,
                method=payload.get("method", method),
                output_dir=str(output),
                files=int(payload.get("files_recovered") or payload.get("files") or 0),
                bytes_written=int(payload.get("bytes_recovered") or payload.get("bytes_written") or 0),
                details=payload,
            )
            self.audit.record("recovery:finish", device_path=device_path, outcome="ok",
                              detail={"files": payload.get("files_recovered")})
            self.finish_job(job_id, result=payload)
            return payload
        except Exception as exc:
            self.audit.record("recovery:finish", device_path=device_path, outcome="error",
                              detail={"error": str(exc)})
            self.finish_job(job_id, error=str(exc))
            raise

    # -------------------------------------------------------------- capacity
    def capacity_test(
        self,
        device_path: str,
        *,
        size_bytes: Optional[int] = None,
        consent_token: Optional[str] = None,
    ) -> dict[str, Any]:
        """Destructive-in-scope capacity test: requires consent, uses a scratch file."""
        device = self.device(device_path)
        if device is None:
            raise UsbDoctorError(f"{device_path} is not currently connected.")
        mount = None
        for candidate in device.mountpoints:
            if os.path.isdir(candidate):
                mount = candidate
                break
        for part in device.partitions:
            if part.mounted_at and os.path.isdir(part.mounted_at):
                mount = part.mounted_at
                break
        if not mount:
            raise UsbDoctorError(
                "The volume is not mounted, so a safe file-based capacity test is not possible.",
                hint="Mount the drive (read-write) and retry. USB Doctor AI will not write raw "
                "sectors for this test.",
            )
        if consent_token is None:
            raise UsbDoctorError("Capacity testing needs explicit approval.")
        signature = compute_signature(device_path, size_bytes=device.size_bytes)
        self.consent.consume(consent_token, device_path=device_path, signature=signature)
        job_id = self.start_job("capacity", device_path=device_path)
        emit = self._emitter(job_id)
        try:
            import shutil

            free = shutil.disk_usage(mount).free
            total = size_bytes or min(free // 2, device.size_bytes or free // 2, 8 << 30)
            tester = CapacityTester(
                block_size=1 << 20,
                max_bytes=total,
                progress=lambda c, t, s: emit(
                    "progress", {"percent": round(c / t * 100, 1) if t else 0, "stage": s}
                ),
            )
            result = tester.test_device_fileonly(device_path, mount, claimed_bytes=total)
            payload = result.to_dict()
            payload["mount"] = mount
            self.finish_job(job_id, result=payload)
            return payload
        except Exception as exc:
            self.finish_job(job_id, error=str(exc))
            raise

    # --------------------------------------------------------------- history
    def history_overview(self) -> dict[str, Any]:
        return {
            "statistics": self.history.statistics(),
            "recent_scans": self.history.recent_scans(20),
            "recent_repairs": self.history.recent_repairs(40),
            "recent_recoveries": self.history.recent_recoveries(20),
            "audit": self.audit.tail(60),
        }

    def status(self) -> dict[str, Any]:
        return {
            "app": APP_NAME,
            "version": __version__,
            "platform": detect_system(),
            "is_admin": is_admin(),
            "state_dir": str(self.state_dir),
            "policy": self.policy.describe(),
            "settings": self.history.all_settings(),
            "tools": {
                name: bool(which(name))
                for name in (
                    "smartctl",
                    "ddrescue",
                    "testdisk",
                    "photorec",
                    "fsck.fat",
                    "dosfsck",
                    "fsck.exfat",
                    "ntfsfix",
                    "e2fsck",
                    "diskutil",
                    "chkdsk",
                )
            },
            "stats": self.history.statistics(),
        }

    def set_setting(self, key: str, value: Any) -> None:
        self.history.set_setting(key, value)
        if key == "offline_mode":
            self.researcher.offline = bool(value)
        if key == "allow_internal_disks":
            self.policy.settings.allow_internal_disks = bool(value)
            self.policy.refresh()
        if key == "allow_mounted_write":
            self.policy.settings.allow_mounted_write = bool(value)

    def research(self, device_path: str) -> dict[str, Any]:
        device = self.device(device_path)
        if device is None:
            raise UsbDoctorError(f"{device_path} is not currently connected.")
        evidence = collect_evidence(device)
        return self.researcher.research(
            brand=device.vendor,
            model=device.model,
            vid=device.vid,
            pid=device.pid,
            firmware=device.firmware,
            error_codes=sorted(evidence.codes()),
            filesystem=evidence.filesystem,
        )


def _serial_hash(serial: Optional[str]) -> Optional[str]:
    if not serial:
        return None
    return hashlib.sha256(serial.encode()).hexdigest()[:32]
