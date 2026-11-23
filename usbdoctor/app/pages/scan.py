"""Health Scan page: the 10-step workflow with live, real progress."""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from usbdoctor.app.widgets.common import Badge, Card, InfoBanner, ProgressRow, label
from usbdoctor.core.util import human_size
from usbdoctor.service.engine import MODES, DoctorService, ScanOptions

WORKFLOW = (
    "Detect the device and read its identity",
    "Identify controller, interface and real capacity",
    "Scan metadata and surface for errors",
    "Analyse error codes against the knowledge base",
    "Research this exact model on official sources",
    "Compare repair procedures and pick the safest",
    "Back up / image the device before any write",
    "Apply only verified-safe repairs",
    "Verify the result by re-reading and re-checking",
    "Produce the report",
)


class ScanPage(QWidget):
    SUBTITLE = "Read-only diagnosis: identity, partitions, file system, SMART, surface and speed."

    scanCompleted = Signal(dict)
    deviceChanged = Signal(str)

    def __init__(self, service: DoctorService, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.service = service
        self.device_path: Optional[str] = None
        self.job_id: Optional[str] = None
        self.auto_repair = False
        self._saw_plan = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll)
        body = QWidget()
        scroll.setWidget(body)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 6, 0)
        layout.setSpacing(13)

        self.banner = InfoBanner()
        layout.addWidget(self.banner)

        controls = Card("Scan setup")
        row = QHBoxLayout()
        row.setSpacing(10)
        self.device_box = QComboBox()
        self.device_box.setMinimumWidth(280)
        self.device_box.currentIndexChanged.connect(self._device_changed)
        row.addWidget(label("Device", name="StatLabel"))
        row.addWidget(self.device_box, 2)

        self.mode_box = QComboBox()
        self.mode_box.addItem("SAFE — read-only checks only", "safe")
        self.mode_box.addItem("SMART REPAIR — safe fixes included", "smart")
        self.mode_box.addItem("ADVANCED — everything, each step confirmed", "advanced")
        self.mode_box.setCurrentIndex(1)
        row.addWidget(label("Mode", name="StatLabel"))
        row.addWidget(self.mode_box, 1)

        self.budget_box = QComboBox()
        for text, value in (("1 minute", 60.0), ("5 minutes", 300.0), ("15 minutes", 900.0),
                            ("Full scan", 0.0)):
            self.budget_box.addItem(text, value)
        self.budget_box.setCurrentIndex(1)
        row.addWidget(label("Surface", name="StatLabel"))
        row.addWidget(self.budget_box)
        controls.add_layout(row)

        row2 = QHBoxLayout()
        self.smart_check = QCheckBox("read SMART")
        self.smart_check.setChecked(True)
        self.bench_check = QCheckBox("benchmark speed")
        self.bench_check.setChecked(True)
        self.capacity_check = QCheckBox("verify capacity")
        self.capacity_check.setChecked(True)
        for widget in (self.smart_check, self.bench_check, self.capacity_check):
            row2.addWidget(widget)
        row2.addStretch(1)
        self.start_button = QPushButton("Start scan")
        self.start_button.setObjectName("Primary")
        self.start_button.clicked.connect(self.start_scan)
        row2.addWidget(self.start_button)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setObjectName("Ghost")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_scan)
        row2.addWidget(self.cancel_button)
        controls.add_layout(row2)

        self.progress = ProgressRow()
        controls.add(self.progress)
        layout.addWidget(controls)

        steps_card = Card("10-step workflow")
        self.step_labels: list[QLabel] = []
        for index, step in enumerate(WORKFLOW, start=1):
            row_label = QLabel(f"{index:>2}.  {step}")
            row_label.setStyleSheet("padding: 3px 0; color: palette(mid);")
            self.step_labels.append(row_label)
            steps_card.add(row_label)
        layout.addWidget(steps_card)

        self.result_card = Card("Result")
        self.verdict_badge = Badge("not scanned")
        self.result_card.add_header_widget(self.verdict_badge)
        self.headline = label("", name="PageSub", wrap=True)
        self.result_card.add(self.headline)
        self.summary = label("", name="Muted", wrap=True)
        self.result_card.add(self.summary)
        self.findings_box = QVBoxLayout()
        self.findings_box.setSpacing(6)
        self.result_card.add_layout(self.findings_box)
        self.notes = label("", name="Dim", wrap=True)
        self.result_card.add(self.notes)
        layout.addWidget(self.result_card)
        layout.addStretch(1)

        self._error_timer = QTimer(self)
        self._error_timer.setSingleShot(True)

    # ------------------------------------------------------------------ data
    def set_device(self, path: str) -> None:
        index = self.device_box.findData(path)
        if index >= 0:
            self.device_box.blockSignals(True)
            self.device_box.setCurrentIndex(index)
            self.device_box.blockSignals(False)
            self.device_path = path

    def refresh_devices(self) -> None:
        current = self.device_path or self.device_box.currentData()
        self.device_box.blockSignals(True)
        self.device_box.clear()
        for device in self.service.devices()["devices"]:
            self.device_box.addItem(
                f"{device['display_name']} — {device['path']} ({human_size(device.get('size_bytes'))})",
                device["path"],
            )
        if current:
            index = self.device_box.findData(current)
            if index >= 0:
                self.device_box.setCurrentIndex(index)
        self.device_box.blockSignals(False)
        self.device_path = self.device_box.currentData()

    def _device_changed(self) -> None:
        self.device_path = self.device_box.currentData()
        if self.device_path:
            self.deviceChanged.emit(self.device_path)

    # ----------------------------------------------------------------- control
    def start_scan(self, *, auto_repair: bool = False) -> None:
        if not self.device_path:
            self.banner.show_message("No drive selected. Connect a drive and refresh the list.", "warn")
            return
        self.auto_repair = auto_repair
        budget = float(self.budget_box.currentData() or 0.0)
        options = ScanOptions(
            mode=self.mode_box.currentData(),
            read_smart=self.smart_check.isChecked(),
            surface_scan=True,
            surface_time_budget_s=budget or None,
            benchmark=self.bench_check.isChecked(),
            verify_capacity=self.capacity_check.isChecked(),
        )
        self._reset_results()
        self.progress.begin("queueing…")
        self.start_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self._mark_step(0)
        try:
            self.job_id = self.service.scan_async(self.device_path, options)
        except Exception as exc:
            self.progress.fail(str(exc))
            self.banner.show_message(str(exc), "error")
            self.start_button.setEnabled(True)
            self.cancel_button.setEnabled(False)

    def cancel_scan(self) -> None:
        if self.job_id:
            self.service.cancel(self.job_id)
            self.progress.set_stage("cancelling…")

    def _reset_results(self) -> None:
        self.verdict_badge.set_status("scanning", "neutral")
        self.headline.setText("")
        self.summary.setText("")
        self.notes.setText("")
        while self.findings_box.count():
            item = self.findings_box.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for step in self.step_labels:
            step.setStyleSheet("padding: 3px 0; color: palette(mid);")

    def _mark_step(self, completed: int) -> None:
        for index, step in enumerate(self.step_labels):
            if index < completed:
                step.setStyleSheet("padding: 3px 0; color: #2ecc9a;")
            elif index == completed:
                step.setStyleSheet("padding: 3px 0; color: #3d8bff; font-weight: 600;")
            else:
                step.setStyleSheet("padding: 3px 0; color: palette(mid);")

    # ------------------------------------------------------------------ events
    def on_engine_event(self, event: dict) -> None:
        if self.job_id is None or event.get("job_id") != self.job_id:
            return
        kind = event["type"]
        payload = event.get("payload") or {}
        if kind == "stage":
            message = payload.get("message") or ""
            self.progress.set_stage(message)
            mapping = {
                "partition": 0, "filesystem": 1, "smart": 2, "surface": 2,
                "capacity": 3, "benchmark": 5, "analysis": 8, "done": 10,
            }
            stage_key = payload.get("stage", "")
            if stage_key in mapping:
                self._mark_step(mapping[stage_key])
        elif kind == "progress":
            self.progress.update(float(payload.get("percent") or 0), payload.get("stage"))
        elif kind == "job_finished":
            self._on_finished(payload)
        elif kind == "error":
            self.progress.fail(payload.get("message", "failed"))
            self.banner.show_message(payload.get("message", "Scan failed."), "error")
            self.start_button.setEnabled(True)
            self.cancel_button.setEnabled(False)
            self.job_id = None

    def _on_finished(self, payload: dict) -> None:
        job_id, self.job_id = self.job_id, None
        self.start_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        if payload.get("error"):
            self.progress.fail(payload["error"])
            self.banner.show_message(f"Scan failed: {payload['error']}", "error")
            return
        # job_finished only reports the status; the payload lives on the job record.
        job = self.service.job(job_id) if job_id else None
        result = (job or {}).get("result")
        if result is None:
            self.progress.finish("complete")
            return
        self.progress.finish("scan complete")
        self._mark_step(len(WORKFLOW))
        self.render_result(result)
        self.scanCompleted.emit(result)
        if self.auto_repair:
            self._offer_auto_repair(result)

    def render_result(self, result: dict) -> None:
        health = result.get("health") or {}
        diagnosis = result.get("diagnosis") or {}
        verdict = health.get("verdict", "UNKNOWN")
        tone = {
            "EXCELLENT": "good", "GOOD": "good", "WARNING": "warn",
            "CRITICAL": "bad", "FAILING": "critical",
        }.get(verdict, "neutral")
        self.verdict_badge.set_status(verdict, tone)
        self.headline.setText(f"<b>{diagnosis.get('headline', '')}</b>")
        bits = [diagnosis.get("summary", "")]
        if result.get("smart"):
            smart = result["smart"]
            if smart.get("available"):
                bits.append(f"SMART: {smart.get('health_text') or 'reported'}")
            else:
                bits.append(f"SMART unavailable — {smart.get('reason')}")
        if result.get("surface"):
            surface = result["surface"]
            bits.append(
                f"Surface: {human_size(surface.get('scanned_bytes'))} scanned, "
                f"{len(surface.get('read_errors') or [])} unreadable region(s)"
            )
        if result.get("performance"):
            perf = result["performance"]
            if perf.get("rating"):
                bits.append(
                    f"Speed: {perf.get('median_mbps', 0):.1f} MB/s ({perf['rating']})"
                )
        bits.append(
            f"Confidence: {health.get('health_confidence', 'low')} · "
            f"{result.get('duration_s', 0):.1f}s · mode {result.get('mode')}"
        )
        self.summary.setText("\n".join(bits))

        while self.findings_box.count():
            item = self.findings_box.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for finding in health.get("findings") or []:
            color = {
                "critical": "#ff3b5c", "error": "#ff5d6e",
                "warning": "#ffbe3d", "info": "#6d7c91",
            }.get(finding.get("severity"), "#6d7c91")
            row = QLabel(
                f"<span style='color:{color}; font-weight:600'>"
                f"{finding.get('severity', '').upper()}</span>&nbsp;&nbsp;"
                f"{finding.get('message', '')}<br>"
                f"<span style='color:#6d7c91; font-family:monospace; font-size:10px'>"
                f"{finding.get('code', '')}</span>"
            )
            row.setWordWrap(True)
            self.findings_box.addWidget(row)

        limitations = result.get("limitations") or []
        self.notes.setText(
            "\n".join(f"note: {note}" for note in limitations[:6]) if limitations else ""
        )

    def _offer_auto_repair(self, result: dict) -> None:
        from PySide6.QtWidgets import QMessageBox

        plans = [p for p in (result.get("plans") or []) if p.get("risk") in ("none", "low")]
        if not plans:
            self.banner.show_message(
                "No automatically safe repairs apply to this drive. See the AI Assistant for next steps.",
                "info",
            )
            return
        listing = "\n".join(f"• {p['title']}  ({p['risk']} risk)" for p in plans)
        box = QMessageBox(self)
        box.setWindowTitle("Apply safe repairs?")
        box.setText(
            f"The scan found {len(plans)} safely repairable problem(s):\n\n{listing}\n\n"
            "A rollback copy is written before each change, and the result is verified afterwards."
        )
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        box.setDefaultButton(QMessageBox.No)
        if box.exec() == QMessageBox.Yes:
            approval = self.service.approve(
                self.device_path or "", operation_ids=[p["operation_id"] for p in plans],
                mode=self.mode_box.currentData(),
            )
            report = self.service.execute(
                self.device_path or "",
                consent_token=approval["consent_token"],
                mode=self.mode_box.currentData(),
                operation_ids=[p["operation_id"] for p in plans],
            )
            from usbdoctor.app.pages.repair import format_repair_report

            self.banner.show_message(format_repair_report(report), "ok")
            self.start_scan()

    def apply_scan(self, result: dict) -> None:
        self.render_result(result)

    def on_shown(self) -> None:
        self.refresh_devices()
