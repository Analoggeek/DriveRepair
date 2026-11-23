"""Performance: measured throughput and counterfeit-capacity verification."""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from usbdoctor.app.widgets.common import Badge, Card, InfoBanner, KeyValueGrid, label
from usbdoctor.core.util import human_size
from usbdoctor.service.engine import DoctorService


class PerformancePage(QWidget):
    SUBTITLE = "Real measured throughput and proof of the drive's true capacity."

    deviceChanged = Signal(str)

    def __init__(self, service: DoctorService, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.service = service
        self.device_path: Optional[str] = None

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

        top = Card("Device")
        row = QHBoxLayout()
        self.device_box = QComboBox()
        self.device_box.setMinimumWidth(320)
        self.device_box.currentIndexChanged.connect(self._device_changed)
        row.addWidget(label("Device", name="StatLabel"))
        row.addWidget(self.device_box, 1)
        top.add_layout(row)
        layout.addWidget(top)

        read_card = Card("Read performance")
        read_card.add(label(
            "Samples are spread across the whole address space, so a fast cache at the start of the "
            "drive cannot hide a slow or failing device.", name="Muted", wrap=True,
        ))
        self.read_facts = KeyValueGrid(2)
        read_card.add(self.read_facts)
        self.rating_badge = Badge("not measured")
        read_card.add_header_widget(self.rating_badge)
        layout.addWidget(read_card)

        capacity_card = Card("Capacity verification")
        capacity_card.add(label(
            "Writes a pattern file across the claimed size, reads every block back and compares. "
            "The test file is always deleted afterwards. This is the only reliable way to prove a "
            "drive is genuine — counterfeit drives quietly wrap writes around to the start.",
            name="Muted", wrap=True,
        ))
        self.consent_check = QCheckBox(
            "I understand this writes and deletes temporary test data on the drive"
        )
        capacity_card.add(self.consent_check)
        row2 = QHBoxLayout()
        self.capacity_button = QPushButton("Verify real capacity")
        self.capacity_button.setObjectName("Primary")
        self.capacity_button.clicked.connect(self.verify_capacity)
        row2.addWidget(self.capacity_button)
        row2.addStretch(1)
        capacity_card.add_layout(row2)
        self.capacity_badge = Badge("not tested")
        capacity_card.add_header_widget(self.capacity_badge)
        self.capacity_facts = KeyValueGrid(2)
        capacity_card.add(self.capacity_facts)
        self.capacity_note = label("", name="Muted", wrap=True)
        capacity_card.add(self.capacity_note)
        layout.addWidget(capacity_card)
        layout.addStretch(1)

    # ------------------------------------------------------------------ data
    def set_device(self, path: str) -> None:
        index = self.device_box.findData(path)
        if index >= 0:
            self.device_box.blockSignals(True)
            self.device_box.setCurrentIndex(index)
            self.device_box.blockSignals(False)
            self.device_path = path
            self.apply_last_scan()

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
        self.apply_last_scan()

    def apply_last_scan(self) -> None:
        if not self.device_path:
            return
        scan = self.service._last_scan.get(self.device_path)  # noqa: SLF001
        if scan is None:
            self.read_facts.set_pairs([("Status", "run a health scan to measure speed")])
            self.rating_badge.set_status("not measured", "neutral")
            return
        self.apply_scan(scan.to_dict())

    def apply_scan(self, result: dict[str, Any]) -> None:
        perf = result.get("performance")
        if perf:
            self.read_facts.set_pairs([
                ("Median read", f"{perf.get('median_mbps', 0):.1f} MB/s"),
                ("Peak read", f"{perf.get('peak_mbps', 0):.1f} MB/s"),
                ("Slowest sample", f"{perf.get('minimum_mbps', 0):.1f} MB/s"),
                ("Interface", perf.get("interface") or "not reported"),
                ("Expected for this link", f"{perf.get('expected_mbps', 0):.0f} MB/s"),
                ("Read errors during test", perf.get("read_errors", 0)),
            ])
            rating = perf.get("rating") or "not measured"
            tone = {"excellent": "good", "good": "good", "slow": "warn",
                    "very slow": "bad"}.get(rating, "neutral")
            self.rating_badge.set_status(rating, tone)
        capacity = result.get("capacity")
        if capacity and capacity.get("reported"):
            self.capacity_facts.set_pairs([
                ("Reported size", human_size(capacity.get("reported"))),
                ("Tail addressable", "yes" if capacity.get("readable_tail") else "no"),
                ("Note", capacity.get("note") or "read-only probe only"),
            ])

    # --------------------------------------------------------------- actions
    def verify_capacity(self) -> None:
        if not self.device_path:
            self.banner.show_message("Select a drive first.", "warn")
            return
        if not self.consent_check.isChecked():
            self.banner.show_message("Tick the consent box first — this test writes to the drive.", "warn")
            return
        box = QMessageBox(self)
        box.setWindowTitle("Verify capacity?")
        box.setIcon(QMessageBox.Warning)
        box.setText(
            "USB Doctor AI will write a temporary pattern file across the drive, read it back and "
            "compare, then delete it.\n\nDo not unplug the drive during the test.\n\nContinue?"
        )
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel)
        box.setDefaultButton(QMessageBox.Cancel)
        if box.exec() != QMessageBox.Yes:
            return
        self.capacity_button.setEnabled(False)
        self.capacity_note.setText("writing and verifying…")
        try:
            approval = self.service.approve(self.device_path, mode="advanced", operation_ids=[])
            payload = self.service.capacity_test(
                self.device_path, consent_token=approval["consent_token"]
            )
        except Exception as exc:
            self.capacity_note.setText(str(exc))
            self.capacity_badge.set_status("failed", "bad")
            self.banner.show_message(str(exc), "error")
            self.capacity_button.setEnabled(True)
            return
        verdict = payload.get("verdict", "inconclusive")
        tone = {"genuine": "good", "counterfeit": "critical", "failing": "bad",
                "flaky": "warn"}.get(verdict, "neutral")
        self.capacity_badge.set_status(verdict, tone)
        self.capacity_facts.set_pairs([
            ("Verdict", verdict),
            ("Verified", human_size(payload.get("tested_bytes"))),
            ("Real capacity", human_size(payload["real_capacity_estimate"])
             if payload.get("real_capacity_estimate") else "not determined"),
            ("Bad blocks", payload.get("bad_blocks", 0)),
            ("Good blocks", payload.get("good_blocks", 0)),
        ])
        self.capacity_note.setText(payload.get("message", ""))
        self.capacity_button.setEnabled(True)

    def on_shown(self) -> None:
        self.refresh_devices()
        self.apply_last_scan()
