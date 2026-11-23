"""Dashboard: at-a-glance state of the newest device and the app's honest limits."""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from usbdoctor.app.widgets.common import Badge, Card, KeyValueGrid, StatTile, label
from usbdoctor.core.util import human_size
from usbdoctor.service.engine import DoctorService


class DashboardPage(QWidget):
    SUBTITLE = "Connect a drive and start with a read-only health scan."

    deviceSelected = Signal(str)
    scanRequested = Signal(str)

    def __init__(self, service: DoctorService, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.service = service
        self.selected: Optional[str] = None
        self.last_scan: Optional[dict[str, Any]] = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll)
        body = QWidget()
        scroll.setWidget(body)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 6, 0)
        layout.setSpacing(14)

        stats = QHBoxLayout()
        stats.setSpacing(14)
        self.stat_devices = StatTile("Devices connected")
        self.stat_scans = StatTile("Scans recorded")
        self.stat_repairs = StatTile("Repairs completed")
        for tile in (self.stat_devices, self.stat_scans, self.stat_repairs):
            stats.addWidget(tile)
        layout.addLayout(stats)

        cards = QHBoxLayout()
        cards.setSpacing(14)

        self.device_card = Card("Selected drive")
        self.device_badge = Badge("none")
        self.device_card.add_header_widget(self.device_badge)
        self.device_name = label("No drive selected", name="PageSub")
        self.device_path = label("Pick a drive on the Devices page.", name="Mono")
        self.device_card.add(self.device_name)
        self.device_card.add(self.device_path)
        self.device_facts = KeyValueGrid(2)
        self.device_card.add(self.device_facts)
        actions = QHBoxLayout()
        self.scan_button = QPushButton("Run health scan")
        self.scan_button.setObjectName("Primary")
        self.scan_button.clicked.connect(self._request_scan)
        actions.addWidget(self.scan_button)
        self.details_button = QPushButton("Open in Devices")
        self.details_button.setObjectName("Ghost")
        actions.addWidget(self.details_button)
        actions.addStretch(1)
        self.device_card.add_layout(actions)
        cards.addWidget(self.device_card, 3)

        truth = Card("What this app can and cannot do")
        for text, ok in (
            ("Recovers files from deleted, formatted and RAW volumes — always working on a copy.", True),
            ("Repairs file-system metadata: boot sectors, FAT mirrors, GPT backups, FS info.", True),
            ("Detects counterfeit capacity and failing flash from real measurements.", True),
            ("Cannot restore physically worn-out memory or extend a drive's lifespan.", False),
            ("Cannot revive a dead controller — that needs hardware-level recovery.", False),
        ):
            row = QLabel(f"{'✓' if ok else '✕'}   {text}")
            row.setWordWrap(True)
            row.setStyleSheet(f"color: {'#2ecc9a' if ok else '#ff5d6e'}; padding: 3px 0;")
            truth.add(row)
        cards.addWidget(truth, 2)
        layout.addLayout(cards)

        self.strip = Card("Connected devices")
        self.strip_refresh = QPushButton("Refresh")
        self.strip_refresh.setObjectName("Ghost")
        self.strip_refresh.clicked.connect(self.reload)
        self.strip.add_header_widget(self.strip_refresh)
        self.strip_grid = QGridLayout()
        self.strip_grid.setSpacing(10)
        self.strip.add_layout(self.strip_grid)
        layout.addWidget(self.strip)

        self.verdict_card = Card("Latest scan")
        self.verdict_body = label("No scan yet for this drive.", name="Muted", wrap=True)
        self.verdict_card.add(self.verdict_body)
        layout.addWidget(self.verdict_card)
        layout.addStretch(1)

        self.details_button.clicked.connect(self._open_in_devices)

    # ------------------------------------------------------------------ data
    def reload(self) -> None:
        listing = self.service.devices()
        self.stat_devices.set_value(len(listing["devices"]))
        stats = self.service.history.statistics()
        self.stat_scans.set_value(stats["scans"])
        self.stat_repairs.set_value(stats["repairs_succeeded"])
        while self.strip_grid.count():
            item = self.strip_grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for index, device in enumerate(listing["devices"][:8]):
            self.strip_grid.addWidget(self._device_chip(device), index // 4, index % 4)
        if not listing["devices"]:
            empty = label("No storage devices detected. Plug in a drive and refresh.", name="Muted")
            self.strip_grid.addWidget(empty, 0, 0)

    def _device_chip(self, device: dict[str, Any]) -> QWidget:
        chip = Card("")
        chip.setMinimumWidth(220)
        name = label(device.get("display_name") or "Unknown device", bold=True)
        name.setWordWrap(True)
        chip.add(name)
        facts = QLabel(
            f"{human_size(device.get('size_bytes'))} · "
            f"{(device.get('filesystem') or 'unknown').upper()}"
        )
        facts.setObjectName("Dim")
        chip.add(facts)
        row = QHBoxLayout()
        badge = Badge(
            (device.get("bus") or "disk").upper(),
            "good" if device.get("bus") == "usb" else "neutral",
        )
        row.addWidget(badge)
        row.addStretch(1)
        button = QPushButton("Scan")
        button.setObjectName("Ghost")
        path = device["path"]
        button.clicked.connect(lambda _=False, p=path: self.scanRequested.emit(p))
        row.addWidget(button)
        chip.add_layout(row)
        chip.mousePressEvent = lambda _e, p=path: self.deviceSelected.emit(p)  # type: ignore[method-assign]
        return chip

    def set_device(self, path: str) -> None:
        self.selected = path
        device = self.service.device(path)
        if device is None:
            self.device_name.setText("No drive selected")
            self.device_badge.set_status("none", "neutral")
            return
        data = device.to_dict()
        self.device_badge.set_status((data.get("bus") or "disk").upper(), "neutral")
        self.device_name.setText(data["display_name"])
        self.device_path.setText(path)
        self.device_facts.set_pairs([
            ("Capacity", human_size(data.get("size_bytes"))),
            ("File system", (data.get("filesystem") or "unknown").upper()),
            ("USB speed", data.get("usb_speed") or "not reported"),
            ("VID / PID", f"{data.get('vid') or '—'} / {data.get('pid') or '—'}"),
            ("Serial", data.get("serial") or "not reported"),
            ("Firmware", data.get("firmware") or "not reported"),
        ])
        self.last_scan = None
        self.verdict_body.setText("No scan yet for this drive in this session.")
        try:
            self.last_scan = self.service.last_scan(path)
        except Exception:
            self.last_scan = None
        if self.last_scan is not None:
            self.apply_scan(self.last_scan.to_dict())

    def apply_scan(self, result: dict) -> None:
        health = result.get("health") or {}
        diagnosis = result.get("diagnosis") or {}
        self.verdict_body.setText(
            f"<b>{health.get('verdict', 'UNKNOWN')}</b> — {health.get('verdict_summary', '')}<br>"
            f"{diagnosis.get('headline', '')}<br>"
            f"<span style='color:#6d7c91'>confidence {health.get('health_confidence', 'low')} · "
            f"{result.get('duration_s', 0):.1f}s</span>"
        )

    def _request_scan(self) -> None:
        if self.selected:
            self.scanRequested.emit(self.selected)

    def _open_in_devices(self) -> None:
        if self.selected:
            self.deviceSelected.emit(self.selected)

    def on_shown(self) -> None:
        self.reload()
