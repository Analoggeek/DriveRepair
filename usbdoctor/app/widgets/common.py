"""Reusable widgets: cards, badges, stat tiles, progress rows, device tables."""

from __future__ import annotations

from typing import Any, Callable, Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from usbdoctor.core.util import human_size


def label(text: str = "", *, name: Optional[str] = None, wrap: bool = False,
          size: Optional[int] = None, bold: bool = False) -> QLabel:
    widget = QLabel(text)
    if name:
        widget.setObjectName(name)
    widget.setWordWrap(wrap)
    if wrap:
        # QLabel only asks for the extra height a wrapped line needs when the
        # size policy advertises heightForWidth; without this the last line is
        # clipped inside layouts.
        policy = widget.sizePolicy()
        policy.setHeightForWidth(True)
        policy.setVerticalPolicy(QSizePolicy.Minimum)
        widget.setSizePolicy(policy)
    if size:
        font = widget.font()
        font.setPointSize(size)
        widget.setFont(font)
    if bold:
        font = widget.font()
        font.setBold(True)
        widget.setFont(font)
    return widget


def hline() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.HLine)
    line.setFixedHeight(1)
    line.setStyleSheet("background: palette(mid); border: none;")
    return line


class Card(QFrame):
    """A titled panel with an optional header action row."""

    def __init__(self, title: str = "", *, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self._root = QVBoxLayout(self)
        self._root.setContentsMargins(18, 16, 18, 16)
        self._root.setSpacing(12)
        self.header = QHBoxLayout()
        self.header.setSpacing(10)
        if title:
            self.title_label = label(title, name="CardTitle")
            self.header.addWidget(self.title_label)
            self.header.addStretch(1)
            self._root.addLayout(self.header)
        else:
            self.title_label = None
        self.body = QVBoxLayout()
        self.body.setSpacing(9)
        self._root.addLayout(self.body)
        # Vertical policy must be Minimum: with Maximum Qt compresses the card's
        # own children (labels get clipped) to satisfy the hint instead of
        # letting the surrounding scroll area provide the space.
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)

    def add_header_widget(self, widget: QWidget) -> None:
        self.header.addWidget(widget)

    def add(self, widget: QWidget) -> QWidget:
        self.body.addWidget(widget)
        return widget

    def add_layout(self, layout) -> None:
        self.body.addLayout(layout)


class Badge(QLabel):
    """Status pill. ``tone`` is good / warn / bad / critical / neutral."""

    def __init__(self, text: str = "", tone: str = "neutral", parent: Optional[QWidget] = None) -> None:
        super().__init__(text)
        self.setParent(parent)
        self.setAlignment(Qt.AlignCenter)
        self.set_tone(tone)

    def set_tone(self, tone: str) -> None:
        suffix = "" if tone in ("neutral", "") else f"--{tone}"
        self.setObjectName(f"Badge{suffix}")
        self.style().unpolish(self)
        self.style().polish(self)

    def set_status(self, text: str, tone: str) -> None:
        self.setText(text)
        self.set_tone(tone)


class StatTile(Card):
    def __init__(self, caption: str, value: str = "—", parent: Optional[QWidget] = None) -> None:
        super().__init__("", parent=parent)
        self.caption = label(caption.upper(), name="StatLabel")
        self.value = label(value, name="StatValue")
        self.add(self.caption)
        self.add(self.value)

    def set_value(self, value: Any) -> None:
        self.value.setText(str(value))


class ProgressRow(QWidget):
    """Progress bar + stage label, driven by real engine events."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.setTextVisible(False)
        self.stage = label("idle", name="Dim")
        meta = QHBoxLayout()
        meta.setContentsMargins(0, 0, 0, 0)
        meta.addWidget(self.stage)
        meta.addStretch(1)
        self.percent = label("0%", name="Dim")
        meta.addWidget(self.percent)
        layout.addWidget(self.bar)
        layout.addLayout(meta)
        self.setVisible(False)

    def begin(self, text: str = "starting…") -> None:
        self.bar.setValue(0)
        self.percent.setText("0%")
        self.stage.setText(text)
        self.setVisible(True)

    def update(self, percent: float, stage: Optional[str] = None) -> None:
        self.bar.setValue(int(max(0, min(100, percent))))
        self.percent.setText(f"{percent:.0f}%")
        if stage:
            self.stage.setText(stage)

    def set_stage(self, text: str) -> None:
        if text:
            self.stage.setText(text)

    def finish(self, text: str = "complete") -> None:
        self.bar.setValue(100)
        self.percent.setText("100%")
        self.stage.setText(text)

    def fail(self, text: str) -> None:
        self.stage.setText(text)
        self.percent.setText("—")

    def reset(self) -> None:
        self.bar.setValue(0)
        self.percent.setText("0%")
        self.stage.setText("idle")
        self.setVisible(False)


class DeviceTable(QTableWidget):
    """Device list with honest rendering of unknown values."""

    deviceSelected = Signal(str)
    scanRequested = Signal(str)

    COLUMNS = ("Drive", "Capacity", "Interface", "File system", "Health", "Write", "")

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(0, len(self.COLUMNS), parent)
        self.setHorizontalHeaderLabels(self.COLUMNS)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.verticalHeader().setVisible(False)
        self.setShowGrid(False)
        self.setAlternatingRowColors(False)
        header = self.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        for index in range(1, len(self.COLUMNS)):
            header.setSectionResizeMode(index, QHeaderView.ResizeToContents)
        self.cellClicked.connect(self._on_click)
        self._paths: list[str] = []

    def set_devices(self, devices: list[dict[str, Any]]) -> None:
        self._paths = [device["path"] for device in devices]
        self.setRowCount(len(devices))
        for row, device in enumerate(devices):
            name = device.get("display_name") or "Unknown device"
            path = device.get("path", "")
            name_item = QTableWidgetItem(f"{name}\n{path}")
            name_item.setData(Qt.UserRole, path)
            self.setItem(row, 0, name_item)
            self.setItem(row, 1, QTableWidgetItem(human_size(device.get("size_bytes"))))
            interface = (device.get("bus") or "unknown").upper()
            if device.get("usb_speed"):
                interface += f" · {device['usb_speed']}"
            self.setItem(row, 2, QTableWidgetItem(interface))
            self.setItem(row, 3, QTableWidgetItem((device.get("filesystem") or "unknown").upper()))
            health = device.get("health_hint") or "not reported"
            self.setItem(row, 4, QTableWidgetItem(health))
            can_write = bool((device.get("can_write") or [False])[0])
            self.setItem(row, 5, QTableWidgetItem("writable" if can_write else "protected"))
            button = QPushButton("Scan")
            button.setObjectName("Ghost")
            button.setCursor(Qt.PointingHandCursor)
            button.clicked.connect(lambda _=False, p=path: self.scanRequested.emit(p))
            self.setCellWidget(row, 6, button)
            self.setRowHeight(row, 46)

    def _on_click(self, row: int, _column: int) -> None:
        item = self.item(row, 0)
        if item is not None:
            self.deviceSelected.emit(item.data(Qt.UserRole))

    def select_path(self, path: str) -> None:
        if path in self._paths:
            self.selectRow(self._paths.index(path))


class KeyValueGrid(QWidget):
    """Two-column facts grid; missing values render as 'not reported'."""

    def __init__(self, columns: int = 2, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        from PySide6.QtWidgets import QGridLayout

        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(22)
        self.grid.setVerticalSpacing(10)
        self.columns = columns

    def set_pairs(self, pairs: list[tuple[str, Any]]) -> None:
        while self.grid.count():
            item = self.grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for index, (key, value) in enumerate(pairs):
            row, col = divmod(index, self.columns)
            holder = QWidget()
            box = QVBoxLayout(holder)
            box.setContentsMargins(0, 0, 0, 0)
            box.setSpacing(1)
            box.addWidget(label(key.upper(), name="StatLabel"))
            shown = "not reported" if value in (None, "", "None") else str(value)
            box.addWidget(label(shown))
            self.grid.addWidget(holder, row, col)
        self.grid.setColumnStretch(self.columns, 1)


class InfoBanner(QFrame):
    """Inline message with a tone; used for errors and scan notes."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(13, 10, 13, 10)
        self.text = label("", wrap=True)
        layout.addWidget(self.text, 1)
        self.close_button = QPushButton("✕")
        self.close_button.setObjectName("LinkButton")
        self.close_button.setFixedWidth(24)
        self.close_button.clicked.connect(lambda: self.setVisible(False))
        layout.addWidget(self.close_button)
        self.setVisible(False)

    def show_message(self, text: str, tone: str = "info") -> None:
        colors = {
            "info": ("#1f6ff0", "rgba(31,111,240,0.12)"),
            "ok": ("#2ecc9a", "rgba(46,204,154,0.12)"),
            "warn": ("#ffbe3d", "rgba(255,190,61,0.12)"),
            "error": ("#ff5d6e", "rgba(255,93,110,0.12)"),
        }
        border, background = colors.get(tone, colors["info"])
        self.setStyleSheet(
            f"InfoBanner {{ background: {background}; border: 1px solid {border};"
            f" border-radius: 9px; }}"
        )
        self.text.setText(text)
        self.setVisible(True)


def copy_to_clipboard(text: str) -> None:
    clipboard = QGuiApplication.clipboard()
    if clipboard is not None:
        clipboard.setText(text)


class Toast(QLabel):
    """Transient message anchored to the parent's bottom-right."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self.setWordWrap(True)
        self.setMaximumWidth(420)
        self.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.hide()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

    def show_message(self, text: str, msec: int = 4200) -> None:
        self.setText(f"  {text}  ")
        self.adjustSize()
        parent = self.parentWidget()
        if parent is not None:
            self.move(max(12, parent.width() - self.width() - 26),
                      max(12, parent.height() - self.height() - 30))
        self.show()
        self.raise_()
        self._timer.start(msec)
