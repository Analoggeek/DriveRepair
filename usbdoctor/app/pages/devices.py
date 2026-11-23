"""Devices page: everything the OS reports, with what could be verified."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from usbdoctor.app.widgets.common import Card, DeviceTable, InfoBanner, label
from usbdoctor.service.engine import DoctorService


class DevicesPage(QWidget):
    SUBTITLE = "Every drive the operating system reports, plus what we could verify about it."

    deviceSelected = Signal(str)
    scanRequested = Signal(str)

    def __init__(self, service: DoctorService, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.service = service
        self._all_devices: list[dict] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        self.banner = InfoBanner()
        layout.addWidget(self.banner)

        card = Card("Storage devices")
        self.refresh_button = QPushButton("Rescan")
        self.refresh_button.setObjectName("Ghost")
        self.refresh_button.clicked.connect(self.refresh)
        card.add_header_widget(self.refresh_button)
        self.internal_check = QCheckBox("show internal disks")
        self.internal_check.stateChanged.connect(self._filter)
        card.add_header_widget(self.internal_check)

        self.table = DeviceTable()
        self.table.setMinimumHeight(320)
        self.table.deviceSelected.connect(self.deviceSelected.emit)
        self.table.scanRequested.connect(self.scanRequested.emit)
        card.add(self.table)

        self.summary = label("", name="Dim")
        card.add(self.summary)
        layout.addWidget(card)

        self.warnings_card = Card("Notes about this enumeration")
        self.warning_rows = QVBoxLayout()
        self.warnings_card.add_layout(self.warning_rows)
        self.warnings_card.setVisible(False)
        layout.addWidget(self.warnings_card)
        layout.addStretch(1)

        self.detail_card = Card("Device detail")
        self.detail_label = label("Select a device to see its raw identity.", name="Mono", wrap=True)
        self.detail_card.add(self.detail_label)
        layout.addWidget(self.detail_card)

    # ------------------------------------------------------------------ data
    def refresh(self) -> None:
        listing = self.service.devices()
        self._all_devices = listing["devices"]
        self._filter()
        rights = "administrator" if listing["is_admin"] else "standard user"
        self.summary.setText(
            f"{len(listing['devices'])} device(s) · enumerated via {listing['source']} · running as {rights}"
        )
        while self.warning_rows.count():
            item = self.warning_rows.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        warnings = listing.get("warnings") or []
        self.warnings_card.setVisible(bool(warnings))
        for warning in warnings:
            self.warning_rows.addWidget(label(f"!  {warning}", name="Muted", wrap=True))
        if not listing["is_admin"]:
            self.banner.show_message(
                "USB Doctor AI is running without administrator/root rights, so raw device reads "
                "and writes may be refused by the OS. Restart it elevated for complete results.",
                "warn",
            )

    def _filter(self) -> None:
        show_internal = self.internal_check.isChecked()
        devices = [
            device for device in self._all_devices
            if show_internal or not (
                device.get("is_system_disk")
                or (device.get("bus") or "").lower() in ("sata", "nvme", "scsi")
            )
        ]
        self.table.set_devices(devices)
        if not devices and self._all_devices:
            self.banner.show_message(
                "Only internal disks were found. Tick 'show internal disks' to list them "
                "(the system disk is always protected).",
                "info",
            )

    def device_paths(self) -> list[str]:
        return [device["path"] for device in self._all_devices]

    def select_path(self, path: str) -> None:
        self.table.select_path(path)
        device = self.service.device(path)
        if device is None:
            return
        data = device.to_dict()
        partitions = data.get("partitions") or []
        lines = [
            f"{key}: {data.get(key)}"
            for key in ("path", "kind", "bus", "usb_speed", "vid", "pid", "serial", "firmware",
                        "size_bytes", "sector_size", "filesystem", "read_only", "removable")
        ]
        if partitions:
            lines.append("partitions:")
            for part in partitions:
                lines.append(
                    f"  [{part['index']}] offset={part['offset_bytes']:,} size={part['size_bytes']:,} "
                    f"fs={part.get('filesystem') or '?'} label={part.get('label') or '-'}"
                )
        if device.limitations:
            lines.append("limitations: " + "; ".join(device.limitations))
        self.detail_label.setText("\n".join(lines))

    def on_shown(self) -> None:
        self.refresh()
