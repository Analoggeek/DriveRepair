"""Data Recovery: image first, then recover — the source is never written."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from usbdoctor.app.widgets.common import Badge, Card, InfoBanner, ProgressRow, label
from usbdoctor.core.util import human_size
from usbdoctor.service.engine import DoctorService


class RecoveryPage(QWidget):
    SUBTITLE = "Image the drive first, then recover files — your source device is never written to."

    deviceChanged = Signal(str)

    def __init__(self, service: DoctorService, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.service = service
        self.device_path: Optional[str] = None
        self.job_kind: Optional[str] = None

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

        top = Card("Source")
        row = QHBoxLayout()
        self.device_box = QComboBox()
        self.device_box.setMinimumWidth(320)
        self.device_box.currentIndexChanged.connect(self._device_changed)
        row.addWidget(label("Device", name="StatLabel"))
        row.addWidget(self.device_box, 1)
        top.add_layout(row)
        layout.addWidget(top)

        image_card = Card("1 · Create a resumable image")
        image_card.add(label(
            "Every readable sector is copied, unreadable regions are skipped and recorded, and an "
            "interrupted run resumes from its map instead of starting over.", name="Muted", wrap=True,
        ))
        image_row = QHBoxLayout()
        self.image_path = QLineEdit()
        self.image_path.setPlaceholderText("where to save the image…")
        image_row.addWidget(self.image_path, 1)
        browse = QPushButton("Choose…")
        browse.setObjectName("Ghost")
        browse.clicked.connect(self._browse_image)
        image_row.addWidget(browse)
        self.image_button = QPushButton("Create image")
        self.image_button.setObjectName("Primary")
        self.image_button.clicked.connect(self.create_image)
        image_row.addWidget(self.image_button)
        image_card.add_layout(image_row)
        self.image_progress = ProgressRow()
        image_card.add(self.image_progress)
        layout.addWidget(image_card)

        recovery_card = Card("2 · Recover files")
        recovery_card.add(label(
            "File-system walk keeps the original names (including deleted entries). "
            "Content carving finds files by their signatures when the metadata is gone. "
            "The automatic method tries the walk first, then carves.",
            name="Muted", wrap=True,
        ))
        row2 = QHBoxLayout()
        self.method_box = QComboBox()
        self.method_box.addItem("Automatic (walk, then carve)", "auto")
        self.method_box.addItem("File-system walk (keeps names)", "filesystem")
        self.method_box.addItem("Content carving", "carve")
        row2.addWidget(label("Method", name="StatLabel"))
        row2.addWidget(self.method_box)
        self.deleted_check = QCheckBox("include deleted entries")
        self.deleted_check.setChecked(True)
        row2.addWidget(self.deleted_check)
        row2.addStretch(1)
        recovery_card.add_layout(row2)

        row3 = QHBoxLayout()
        self.output_dir = QLineEdit()
        self.output_dir.setPlaceholderText("output folder (blank = app data folder)")
        row3.addWidget(self.output_dir, 1)
        browse_out = QPushButton("Choose…")
        browse_out.setObjectName("Ghost")
        browse_out.clicked.connect(self._browse_output)
        row3.addWidget(browse_out)
        self.recover_button = QPushButton("Recover files")
        self.recover_button.setObjectName("Primary")
        self.recover_button.clicked.connect(self.recover)
        row3.addWidget(self.recover_button)
        recovery_card.add_layout(row3)
        self.recovery_progress = ProgressRow()
        recovery_card.add(self.recovery_progress)
        layout.addWidget(recovery_card)

        self.result_card = Card("Result")
        self.result_badge = Badge("nothing recovered")
        self.result_card.add_header_widget(self.result_badge)
        self.result_body = label("", wrap=True, name="Muted")
        self.result_card.add(self.result_body)
        self.open_folder = QPushButton("Open output folder")
        self.open_folder.setObjectName("Ghost")
        self.open_folder.setEnabled(False)
        self.open_folder.clicked.connect(self._open_output)
        self.result_card.add(self.open_folder)
        layout.addWidget(self.result_card)
        layout.addStretch(1)
        self._last_output: Optional[str] = None
        self._last_job_id: Optional[str] = None

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
        if self.device_path and not self.image_path.text():
            stem = Path(self.device_path).stem or "device"
            self.image_path.setText(str(Path.home() / "usbdoctor-images" / f"{stem}.img"))

    def _browse_image(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Save image as", self.image_path.text() or str(Path.home()), "Disk images (*.img);;All files (*)"
        )
        if path:
            self.image_path.setText(path)

    def _browse_output(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Recovered files folder", str(Path.home()))
        if path:
            self.output_dir.setText(path)

    def _open_output(self) -> None:
        if not self._last_output:
            return
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        QDesktopServices.openUrl(QUrl.fromLocalFile(self._last_output))

    # --------------------------------------------------------------- actions
    def create_image(self) -> None:
        if not self.device_path:
            self.banner.show_message("Select a drive first.", "warn")
            return
        destination = self.image_path.text().strip()
        if not destination:
            self.banner.show_message("Choose where to save the image.", "warn")
            return
        self.job_kind = "image"
        self.image_progress.begin("preparing…")
        try:
            payload = self.service.image_device(
                self.device_path, destination, prefer_ddrescue=True
            )
        except Exception as exc:
            self.image_progress.fail(str(exc))
            self.banner.show_message(str(exc), "error")
            return
        self.image_progress.finish("image complete")
        self.result_badge.set_status("image ready", "good")
        self.result_body.setText(
            f"<b>{payload.get('image_path')}</b><br>"
            f"good bytes: {human_size(payload.get('good_bytes'))} · "
            f"bad regions: {len(payload.get('bad_regions') or [])} · "
            f"engine: {payload.get('engine')}<br>"
            "<span style='color: palette(mid)'>You can now recover files from the image by "
            "entering it as the source path.</span>"
        )

    def recover(self) -> None:
        if not self.device_path:
            self.banner.show_message("Select a drive first.", "warn")
            return
        self.job_kind = "recovery"
        self.recovery_progress.begin("recovering…")
        try:
            payload = self.service.recover(
                self.device_path,
                method=self.method_box.currentData(),
                output_dir=self.output_dir.text().strip() or None,
                include_deleted=self.deleted_check.isChecked(),
            )
        except Exception as exc:
            self.recovery_progress.fail(str(exc))
            self.banner.show_message(str(exc), "error")
            return
        self.recovery_progress.finish("done")
        files = payload.get("files_recovered") or 0
        self.result_badge.set_status(f"{files} file(s)", "good" if files else "warn")
        self._last_output = payload.get("output_dir")
        self.open_folder.setEnabled(bool(self._last_output))
        categories = (payload.get("carved") or {}).get("by_category") or {}
        category_text = ", ".join(f"{k}={v}" for k, v in categories.items()) if categories else ""
        self.result_body.setText(
            f"<b>{files}</b> file(s), {human_size(payload.get('bytes_recovered'))}<br>"
            f"method: {payload.get('method')}<br>"
            f"output: {self._last_output or '—'}"
            + (f"<br>{category_text}" if category_text else "")
            + ("" if files else "<br><span style='color:#ffbe3d'>Nothing was recovered. If the "
               "metadata is unreadable, try content carving, or image the device and carve from "
               "the image.</span>")
        )

    # ---------------------------------------------------------------- events
    def on_engine_event(self, event: dict) -> None:
        if not self._last_job_id and self.job_kind is None:
            return
        payload = event.get("payload") or {}
        progress = self.image_progress if self.job_kind == "image" else self.recovery_progress
        if event["type"] == "progress":
            progress.update(float(payload.get("percent") or 0), payload.get("stage"))
        elif event["type"] == "stage" and payload.get("message"):
            progress.set_stage(payload["message"])

    def on_shown(self) -> None:
        self.refresh_devices()
