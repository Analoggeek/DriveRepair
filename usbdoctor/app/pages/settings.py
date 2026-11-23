"""Settings: safety policy, tool detection, privacy and about."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QGridLayout,
    QHBoxLayout,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from usbdoctor.app.widgets.common import Badge, Card, InfoBanner, label
from usbdoctor.service.engine import DoctorService
from usbdoctor.version import APP_NAME, APP_TAGLINE, __version__

TOOL_NOTES = {
    "smartctl": "SMART attributes and self-test results",
    "ddrescue": "resumable imaging with retry/skip",
    "testdisk": "partition recovery reference tool",
    "photorec": "file carving (alternative engine)",
    "fsck.fat": "FAT file-system verification",
    "dosfsck": "FAT verification (alternative name)",
    "fsck.exfat": "exFAT verification",
    "ntfsfix": "clears the NTFS dirty flag",
    "e2fsck": "ext2/3/4 verification",
    "diskutil": "macOS volume repair",
    "chkdsk": "Windows file-system checker",
}


class SettingsPage(QWidget):
    SUBTITLE = "Safety policy, detected tooling, privacy and limits."

    def __init__(self, service: DoctorService, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.service = service

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

        safety = Card("Safety policy")
        safety.add(label(
            "These switches drive the same policy object the repair executor enforces. "
            "The user interface cannot bypass them.", name="Muted", wrap=True,
        ))
        self.internal_check = QCheckBox(
            "Allow internal disks (the running system disk is always refused)"
        )
        self.internal_check.stateChanged.connect(self.save)
        safety.add(self.internal_check)
        self.mounted_check = QCheckBox(
            "Allow repairs on mounted volumes (not recommended — the OS may cache stale data)"
        )
        self.mounted_check.stateChanged.connect(self.save)
        safety.add(self.mounted_check)
        self.offline_check = QCheckBox("Offline mode — never contact vendor sites")
        self.offline_check.stateChanged.connect(self.save)
        safety.add(self.offline_check)
        self.backup_check = QCheckBox(
            "Always remind me to create an image before medium-risk repairs (recommended)"
        )
        self.backup_check.setChecked(True)
        self.backup_check.stateChanged.connect(self.save)
        safety.add(self.backup_check)
        layout.addWidget(safety)

        tools = Card("External tools detected")
        self.tool_grid = QGridLayout()
        self.tool_grid.setSpacing(8)
        tools.add_layout(self.tool_grid)
        tools.add(label(
            "Missing tools only limit optional features — the built-in imager, FAT/exFAT recovery, "
            "carving and all metadata repairs work without them.",
            name="Dim", wrap=True,
        ))
        layout.addWidget(tools)

        privacy = Card("Privacy")
        self.refresh_button = QPushButton("Delete all local history")
        self.refresh_button.setObjectName("Ghost")
        self.refresh_button.clicked.connect(self.purge_history)
        privacy.add_header_widget(self.refresh_button)
        privacy.add(label(
            "History lives in a local SQLite file; serial numbers are stored as one-way hashes. "
            "Nothing is uploaded anywhere. Network research only fetches allow-listed official pages "
            "and never sends device data.", name="Muted", wrap=True,
        ))
        self.storage_label = label("", name="Mono", wrap=True)
        privacy.add(self.storage_label)
        layout.addWidget(privacy)

        about = Card(f"About {APP_NAME.replace(chr(38), chr(38) * 2)}")
        about.add(label(f"<b>{APP_NAME}</b> {__version__} — {APP_TAGLINE}", wrap=True))
        limits = [
            "Works only with the data your hardware and OS expose — nothing is simulated.",
            "Cannot restore physically damaged memory or worn-out flash.",
            "Cannot flash firmware; controller tools are never run automatically.",
            "Never formats, never overwrites source data during recovery.",
            "Every write needs your explicit approval and produces a rollback file.",
        ]
        for text in limits:
            about.add(label(f"•  {text}", name="Muted", wrap=True))
        self.rights_badge = Badge("rights unknown")
        about.add_header_widget(self.rights_badge)
        layout.addWidget(about)
        layout.addStretch(1)

    # ------------------------------------------------------------------ data
    def reload(self) -> None:
        settings = self.service.history.all_settings()
        self.internal_check.blockSignals(True)
        self.mounted_check.blockSignals(True)
        self.offline_check.blockSignals(True)
        self.internal_check.setChecked(bool(settings.get("allow_internal_disks")))
        self.mounted_check.setChecked(bool(settings.get("allow_mounted_write")))
        self.offline_check.setChecked(bool(settings.get("offline_mode")))
        self.internal_check.blockSignals(False)
        self.mounted_check.blockSignals(False)
        self.offline_check.blockSignals(False)

        status = self.service.status()
        while self.tool_grid.count():
            item = self.tool_grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for index, (tool, present) in enumerate(sorted(status["tools"].items())):
            row, col = divmod(index, 3)
            holder = QWidget()
            box = QHBoxLayout(holder)
            box.setContentsMargins(0, 0, 0, 0)
            box.setSpacing(7)
            box.addWidget(Badge("found" if present else "missing", "good" if present else "neutral"))
            box.addWidget(label(tool, name="Mono"))
            box.addStretch(1)
            holder.setToolTip(TOOL_NOTES.get(tool, ""))
            self.tool_grid.addWidget(holder, row, col)
        self.storage_label.setText(
            f"state directory: {status['state_dir']}\n"
            f"scans stored: {status['stats']['scans']} · repairs: {status['stats']['repairs']} · "
            f"files recovered: {status['stats']['files_recovered']}"
        )
        self.rights_badge.set_status(
            "administrator" if status["is_admin"] else "standard user",
            "good" if status["is_admin"] else "warn",
        )

    def save(self) -> None:
        self.service.set_setting("allow_internal_disks", self.internal_check.isChecked())
        self.service.set_setting("allow_mounted_write", self.mounted_check.isChecked())
        self.service.set_setting("offline_mode", self.offline_check.isChecked())
        self.service.set_setting("auto_backup_before_repair", self.backup_check.isChecked())
        self.banner.show_message("Settings saved and applied to the safety policy.", "ok")

    def purge_history(self) -> None:
        from PySide6.QtWidgets import QMessageBox

        box = QMessageBox(self)
        box.setWindowTitle("Delete all local history?")
        box.setText(
            "Every stored scan, repair and recovery record will be deleted.\n"
            "Report files and recovered data on disk are NOT deleted.\n\nContinue?"
        )
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel)
        box.setDefaultButton(QMessageBox.Cancel)
        if box.exec() != QMessageBox.Yes:
            return
        store = self.service.history
        for scan in store.recent_scans(limit=10000):
            store.purge_device(scan["device_path"])
        self.service.audit.record("history:purged_all", outcome="ok")
        self.reload()
        self.banner.show_message("All local history deleted.", "ok")

    def on_shown(self) -> None:
        self.reload()
