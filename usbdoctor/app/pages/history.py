"""History: every scan and repair, with verification status, from the local DB."""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from usbdoctor.app.widgets.common import Card, InfoBanner, label
from usbdoctor.core.util import human_size
from usbdoctor.service.engine import DoctorService


def _table(columns: list[str]) -> QTableWidget:
    table = QTableWidget(0, len(columns))
    table.setHorizontalHeaderLabels(columns)
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.verticalHeader().setVisible(False)
    table.setShowGrid(False)
    table.setAlternatingRowColors(False)
    header = table.horizontalHeader()
    header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
    header.setSectionResizeMode(1, QHeaderView.Stretch)
    for index in range(2, len(columns)):
        header.setSectionResizeMode(index, QHeaderView.ResizeToContents)
    return table


class HistoryPage(QWidget):
    SUBTITLE = "Every scan, repair and recovery stored locally, with verification status."

    def __init__(self, service: DoctorService, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.service = service
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(13)

        self.banner = InfoBanner()
        layout.addWidget(self.banner)

        scans = Card("Scan history")
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.setObjectName("Ghost")
        self.refresh_button.clicked.connect(self.reload)
        scans.add_header_widget(self.refresh_button)
        self.scan_table = _table(["When", "Device", "Verdict", "Confidence", "Mode"])
        scans.add(self.scan_table)
        layout.addWidget(scans)

        repairs = Card("Repair history")
        self.repair_table = _table(["When", "Operation", "Risk", "Status", "Verified", "Message"])
        repairs.add(self.repair_table)
        layout.addWidget(repairs)

        recoveries = Card("Recovery sessions")
        self.recovery_table = _table(["When", "Device", "Method", "Files", "Bytes"])
        recoveries.add(self.recovery_table)
        layout.addWidget(recoveries)

        footer = QHBoxLayout()
        self.stats = label("", name="Dim")
        footer.addWidget(self.stats)
        footer.addStretch(1)
        self.purge_button = QPushButton("Forget the selected device")
        self.purge_button.setObjectName("Ghost")
        self.purge_button.clicked.connect(self.purge)
        footer.addWidget(self.purge_button)
        layout.addLayout(footer)
        self._device_paths: list[str] = []

    # ------------------------------------------------------------------ data
    def reload(self) -> None:
        overview = self.service.history_overview()
        scans = overview["recent_scans"]
        self.scan_table.setRowCount(len(scans))
        self._device_paths = []
        for row, scan in enumerate(scans):
            self._device_paths.append(scan["device_path"])
            values = [
                (scan.get("started_at") or "")[:19].replace("T", " "),
                scan.get("device_name") or scan.get("device_path") or "",
                scan.get("verdict") or "—",
                scan.get("confidence") or "—",
                scan.get("mode") or "—",
            ]
            for column, value in enumerate(values):
                self.scan_table.setItem(row, column, QTableWidgetItem(str(value)))
            self.scan_table.setRowHeight(row, 32)

        repairs = overview["recent_repairs"]
        self.repair_table.setRowCount(len(repairs))
        for row, repair in enumerate(repairs):
            verified = {1: "verified", 0: "not verified", None: "—"}.get(repair.get("verified"), "—")
            values = [
                (repair.get("started_at") or "")[:19].replace("T", " "),
                repair.get("title") or repair.get("operation_id") or "",
                repair.get("risk") or "—",
                repair.get("status") or "—",
                verified,
                repair.get("message") or "",
            ]
            for column, value in enumerate(values):
                self.repair_table.setItem(row, column, QTableWidgetItem(str(value)))
            self.repair_table.setRowHeight(row, 32)

        recoveries = overview["recent_recoveries"]
        self.recovery_table.setRowCount(len(recoveries))
        for row, recovery in enumerate(recoveries):
            values = [
                (recovery.get("started_at") or "")[:19].replace("T", " "),
                recovery.get("device_path") or "",
                recovery.get("method") or "",
                recovery.get("files") or 0,
                human_size(recovery.get("bytes")),
            ]
            for column, value in enumerate(values):
                self.recovery_table.setItem(row, column, QTableWidgetItem(str(value)))
            self.recovery_table.setRowHeight(row, 32)

        stats = overview["statistics"]
        self.stats.setText(
            f"{stats['scans']} scans · {stats['repairs']} repairs "
            f"({stats['repairs_succeeded']} succeeded) · "
            f"{stats['files_recovered']} files recovered"
        )
        if not scans:
            self.banner.show_message(
                "No history yet. Run a scan and it will be recorded here automatically.", "info"
            )

    def purge(self) -> None:
        row = self.scan_table.currentRow()
        if row < 0 or row >= len(self._device_paths):
            self.banner.show_message("Select a device row first.", "warn")
            return
        from PySide6.QtWidgets import QMessageBox

        path = self._device_paths[row]
        box = QMessageBox(self)
        box.setWindowTitle("Forget this device?")
        box.setText(f"Delete every stored record for:\n{path}\n\nThis cannot be undone.")
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel)
        box.setDefaultButton(QMessageBox.Cancel)
        if box.exec() == QMessageBox.Yes:
            self.service.history.purge_device(path)
            self.service.audit.record("history:purged", device_path=path, outcome="ok")
            self.reload()
            self.banner.show_message(f"Records for {path} were deleted.", "ok")

    def on_shown(self) -> None:
        self.reload()
