"""Repair Center: evidence-matched operations, explicit approval, verification."""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from usbdoctor.app.widgets.common import Badge, Card, InfoBanner, label
from usbdoctor.service.engine import DoctorService

TONE = {"none": "good", "low": "good", "medium": "warn", "high": "bad", "critical": "critical"}


def format_repair_report(report: dict[str, Any]) -> str:
    lines = [f"{report.get('succeeded', 0)} succeeded, {report.get('failed', 0)} failed"]
    for step in report.get("steps") or []:
        result = step.get("result") or {}
        mark = {"succeeded": "✓", "failed": "✗", "skipped": "–"}.get(result.get("status"), "?")
        verified = "verified" if result.get("verified") else "not verified"
        lines.append(
            f"{mark} {step.get('plan', {}).get('title', '')} — {result.get('message', '')} ({verified})"
        )
    return "\n".join(lines)


class RepairPage(QWidget):
    SUBTITLE = "Evidence-matched repairs, each with its risk, rollback and verification."

    deviceChanged = Signal(str)

    def __init__(self, service: DoctorService, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.service = service
        self.device_path: Optional[str] = None
        self.plans: list[dict[str, Any]] = []
        self._rows: list[tuple[dict, QCheckBox]] = []

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

        controls = Card("Repair plan")
        row = QHBoxLayout()
        self.device_box = QComboBox()
        self.device_box.setMinimumWidth(280)
        self.device_box.currentIndexChanged.connect(self._device_changed)
        row.addWidget(label("Device", name="StatLabel"))
        row.addWidget(self.device_box, 2)
        self.mode_box = QComboBox()
        for text, value in (("SAFE", "safe"), ("SMART REPAIR", "smart"), ("ADVANCED", "advanced")):
            self.mode_box.addItem(text, value)
        self.mode_box.setCurrentIndex(1)
        self.mode_box.currentIndexChanged.connect(lambda: self.load_plans())
        row.addWidget(label("Mode", name="StatLabel"))
        row.addWidget(self.mode_box)
        self.load_button = QPushButton("Load plan")
        self.load_button.clicked.connect(self.load_plans)
        row.addWidget(self.load_button)
        controls.add_layout(row)
        layout.addWidget(controls)

        self.plan_card = Card("Applicable operations")
        self.plan_box = QVBoxLayout()
        self.plan_box.setSpacing(8)
        self.plan_card.add_layout(self.plan_box)
        action_row = QHBoxLayout()
        self.dry_button = QPushButton("Dry run")
        self.dry_button.setObjectName("Ghost")
        self.dry_button.clicked.connect(self.dry_run)
        self.dry_button.setEnabled(False)
        action_row.addWidget(self.dry_button)
        action_row.addStretch(1)
        self.select_all = QCheckBox("select all")
        self.select_all.setChecked(True)
        self.select_all.stateChanged.connect(self._toggle_all)
        action_row.addWidget(self.select_all)
        self.repair_button = QPushButton("Approve && Repair")
        self.repair_button.setObjectName("Primary")
        self.repair_button.clicked.connect(self.approve_and_repair)
        self.repair_button.setEnabled(False)
        action_row.addWidget(self.repair_button)
        self.plan_card.add_layout(action_row)
        layout.addWidget(self.plan_card)

        self.result_card = Card("Result")
        self.result_badge = Badge("nothing run")
        self.result_card.add_header_widget(self.result_badge)
        self.result_body = label("", wrap=True, name="Muted")
        self.result_card.add(self.result_body)
        layout.addWidget(self.result_card)

        failure = Card("Permanent failure")
        failure_text = label(
            "When a drive genuinely cannot be repaired, the app says so plainly:",
            name="Muted", wrap=True,
        )
        failure.add(failure_text)
        friendly = QLabel(
            "<div style='background: rgba(255,190,61,0.10); border: 1px solid #ffbe3d;"
            " border-radius: 10px; padding: 14px;'>"
            "<span style='font-size: 22px;'>😅</span><br>"
            "<b>Bhai, is pendrive ne apni duty kaafi kar li hai!</b><br>"
            "<span style='color: palette(mid)'>Isme hardware failure ke strong signs mil rahe hain. "
            "Data important hai toh professional recovery service try karo, warna nayi pendrive "
            "lena better rahega.</span></div>"
        )
        friendly.setWordWrap(True)
        failure.add(friendly)
        layout.addWidget(failure)
        layout.addStretch(1)

    # ------------------------------------------------------------------ data
    def set_device(self, path: str) -> None:
        index = self.device_box.findData(path)
        if index >= 0:
            self.device_box.blockSignals(True)
            self.device_box.setCurrentIndex(index)
            self.device_box.blockSignals(False)
            self.device_path = path
            self.load_plans()

    def refresh_devices(self) -> None:
        from usbdoctor.core.util import human_size

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
        self.load_plans()

    def load_plans(self) -> None:
        self._clear_plans()
        if not self.device_path:
            return
        try:
            payload = self.service.plan(self.device_path, mode=self.mode_box.currentData())
        except Exception as exc:
            self.banner.show_message(str(exc), "error")
            return
        self.plans = payload["plans"]
        if not self.plans:
            self.plan_box.addWidget(
                label("No repair applies to this device in this mode. "
                      "If files are missing, use Data Recovery.", name="Muted", wrap=True)
            )
            self.repair_button.setEnabled(False)
            self.dry_button.setEnabled(False)
            return
        for plan in self.plans:
            container = QWidget()
            box = QVBoxLayout(container)
            box.setContentsMargins(0, 4, 0, 4)
            box.setSpacing(3)
            head = QHBoxLayout()
            check = QCheckBox()
            check.setChecked(bool(plan.get("requires_consent")))
            check.setEnabled(bool(plan.get("requires_consent")))
            head.addWidget(check)
            head.addWidget(label(plan["title"], bold=True))
            head.addWidget(Badge(plan["risk"].upper(), TONE.get(plan["risk"], "neutral")))
            if plan.get("verified_required"):
                head.addWidget(Badge("VERIFIED", "neutral"))
            head.addStretch(1)
            box.addLayout(head)
            summary = label(plan.get("summary", ""), name="Muted", wrap=True)
            summary.setContentsMargins(24, 0, 0, 0)
            box.addWidget(summary)
            if plan.get("rationale"):
                why = label(plan["rationale"], name="Dim", wrap=True)
                why.setContentsMargins(24, 0, 0, 0)
                box.addWidget(why)
            self._rows.append((plan, check))
            self.plan_box.addWidget(container)
        self.repair_button.setEnabled(any(p.get("requires_consent") for p in self.plans))
        self.dry_button.setEnabled(True)

    def _clear_plans(self) -> None:
        self._rows.clear()
        while self.plan_box.count():
            item = self.plan_box.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _toggle_all(self, state: int) -> None:
        for plan, check in self._rows:
            if check.isEnabled():
                check.setChecked(bool(state))

    def _selected(self) -> list[str]:
        return [plan["operation_id"] for plan, check in self._rows if check.isChecked()]

    # --------------------------------------------------------------- actions
    def dry_run(self) -> None:
        if not self.device_path:
            return
        try:
            report = self.service.execute(
                self.device_path, consent_token=None, mode=self.mode_box.currentData(),
                operation_ids=self._selected(), dry_run=True,
            )
        except Exception as exc:
            self.banner.show_message(str(exc), "error")
            return
        self._render_report(report, dry_run=True)

    def approve_and_repair(self) -> None:
        if not self.device_path:
            return
        operations = self._selected()
        if not operations:
            self.banner.show_message("Select at least one operation.", "warn")
            return
        chosen = [plan for plan, _ in self._rows if plan["operation_id"] in operations]
        listing = "\n".join(f"• {p['title']}  ({p['risk']} risk)" for p in chosen)
        box = QMessageBox(self)
        box.setWindowTitle("Approve these operations?")
        box.setIcon(QMessageBox.Warning)
        box.setText(
            f"The following will modify:\n{self.device_path}\n\n{listing}\n\n"
            "A rollback copy is written before each change. Continue?"
        )
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel)
        box.setDefaultButton(QMessageBox.Cancel)
        if box.exec() != QMessageBox.Yes:
            return
        try:
            approval = self.service.approve(
                self.device_path, operation_ids=operations, mode=self.mode_box.currentData()
            )
            report = self.service.execute(
                self.device_path,
                consent_token=approval["consent_token"],
                mode=self.mode_box.currentData(),
                operation_ids=operations,
            )
        except Exception as exc:
            self.banner.show_message(str(exc), "error")
            return
        self._render_report(report)
        # Re-plan so the page reflects reality after the repair.
        self.load_plans()

    def _render_report(self, report: dict[str, Any], *, dry_run: bool = False) -> None:
        tone = "warn" if report.get("failed") else ("neutral" if dry_run else "good")
        text = "dry run — nothing written" if dry_run else (
            f"{report.get('succeeded', 0)} succeeded · {report.get('failed', 0)} failed"
        )
        self.result_badge.set_status(text, tone)
        lines = []
        for step in report.get("steps") or []:
            result = step.get("result") or {}
            plan = step.get("plan") or {}
            verified = (
                "verified" if result.get("verified") is True
                else "NOT verified" if result.get("verified") is False
                else "verification not applicable"
            )
            lines.append(
                f"<b>{plan.get('title', '')}</b> — {result.get('status', '')}, {verified}<br>"
                f"<span style='color: palette(mid)'>{result.get('message', '')}</span>"
            )
        self.result_body.setText("<br><br>".join(lines) or "Nothing ran.")

    def on_shown(self) -> None:
        self.refresh_devices()
