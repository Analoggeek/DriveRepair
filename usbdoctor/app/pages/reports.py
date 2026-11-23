"""Reports: render and export the last scan in Markdown, HTML or JSON."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QPushButton,
    QSplitter,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from usbdoctor.app.widgets.common import Card, InfoBanner, label
from usbdoctor.core.util import human_size
from usbdoctor.report import ReportBundle, to_html, to_markdown
from usbdoctor.service.engine import DoctorService


class ReportsPage(QWidget):
    SUBTITLE = "Export the health report as Markdown, a self-contained HTML file, or raw JSON."

    def __init__(self, service: DoctorService, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.service = service
        self.device_path: Optional[str] = None
        self._last_paths: dict[str, str] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(13)

        self.banner = InfoBanner()
        layout.addWidget(self.banner)

        top = Card("Report")
        row = QHBoxLayout()
        self.device_box = QComboBox()
        self.device_box.setMinimumWidth(300)
        self.device_box.currentIndexChanged.connect(self._device_changed)
        row.addWidget(label("Device", name="StatLabel"))
        row.addWidget(self.device_box, 1)
        self.write_button = QPushButton("Save report files…")
        self.write_button.setObjectName("Primary")
        self.write_button.clicked.connect(self.save_files)
        row.addWidget(self.write_button)
        self.open_button = QPushButton("Open HTML")
        self.open_button.setObjectName("Ghost")
        self.open_button.setEnabled(False)
        self.open_button.clicked.connect(self._open_html)
        row.addWidget(self.open_button)
        top.add_layout(row)
        self.summary = label("Run a scan, then export.", name="Dim", wrap=True)
        top.add(self.summary)
        layout.addWidget(top)

        splitter = QSplitter(Qt.Horizontal)
        self.preview = QTextBrowser()
        self.preview.setObjectName("Diagnosis")
        self.preview.setOpenExternalLinks(True)
        splitter.addWidget(self.preview)
        layout.addWidget(splitter, 1)

    # ------------------------------------------------------------------ data
    def set_device(self, path: str) -> None:
        index = self.device_box.findData(path)
        if index >= 0:
            self.device_box.blockSignals(True)
            self.device_box.setCurrentIndex(index)
            self.device_box.blockSignals(False)
            self.device_path = path
            self.refresh_preview()

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
        self.refresh_preview()

    def _device_changed(self) -> None:
        self.device_path = self.device_box.currentData()
        self.refresh_preview()

    def _bundle(self) -> Optional[ReportBundle]:
        if not self.device_path:
            return None
        scan = self.service._last_scan.get(self.device_path)  # noqa: SLF001
        if scan is None:
            return None
        data = scan.to_dict()
        return ReportBundle(
            device=data["device"], health=data.get("health"), filesystem=data.get("filesystem"),
            partition=data.get("partition"), smart=data.get("smart"), surface=data.get("surface"),
            performance=data.get("performance"), diagnosis=data.get("diagnosis"),
            limitations=data.get("limitations"),
        )

    def refresh_preview(self) -> None:
        bundle = self._bundle()
        if bundle is None:
            self.preview.setMarkdown(
                "No scan has been run on this device in this session.\n\n"
                "Run a Health Scan first — the report is built only from data the hardware "
                "actually provided."
            )
            self.summary.setText("Run a scan, then export.")
            self.open_button.setEnabled(False)
            return
        self.preview.setHtml(to_html(bundle))
        data = bundle.to_dict()
        findings = (data.get("health") or {}).get("findings") or []
        self.summary.setText(
            f"{len(findings)} finding(s) · verdict {(data.get('health') or {}).get('verdict')} · "
            "unavailable values are printed as 'not reported'"
        )
        self.open_button.setEnabled("html" in self._last_paths)

    def save_files(self) -> None:
        bundle = self._bundle()
        if bundle is None:
            self.banner.show_message("Run a scan first so there is something to report.", "warn")
            return
        stem = Path(self.device_path or "report").stem
        base, _ = QFileDialog.getSaveFileName(
            self, "Save report as", str(Path.home() / f"usbdoctor-{stem}"),
            "Report base name (*)",
        )
        if not base:
            return
        from usbdoctor.report import save

        self._last_paths = save(bundle, base)
        self.open_button.setEnabled(True)
        self.banner.show_message(
            "Saved:\n" + "\n".join(f"{kind}: {path}" for kind, path in self._last_paths.items()), "ok"
        )

    def _open_html(self) -> None:
        path = self._last_paths.get("html")
        if path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def apply_scan(self, result: dict[str, Any]) -> None:
        self.refresh_preview()

    def on_shown(self) -> None:
        self.refresh_devices()
