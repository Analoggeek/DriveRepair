"""Desktop application shell: sidebar, page stack, live event wiring."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from usbdoctor.app.theme import stylesheet
from usbdoctor.app.widgets.common import Toast
from usbdoctor.service.engine import DoctorService
from usbdoctor.version import APP_NAME, APP_TAGLINE, __version__

LOG = logging.getLogger(__name__)

PAGES = (
    ("dashboard", "◉", "Dashboard"),
    ("devices", "▤", "Devices"),
    ("scan", "◎", "Health Scan"),
    ("repair", "✚", "Repair Center"),
    ("recovery", "⟲", "Data Recovery"),
    ("performance", "⚡", "Performance"),
    ("assistant", "✦", "AI Assistant"),
    ("history", "☰", "History"),
    ("reports", "▦", "Reports"),
    ("settings", "⚙", "Settings"),
)


class EventBridge(QObject):
    """Marshals engine events from worker threads onto the GUI thread."""

    event = Signal(dict)

    def __init__(self, service: DoctorService, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.service = service
        self._timer = QTimer(self)
        self._timer.setInterval(120)
        self._timer.timeout.connect(self._drain)
        self._queue = service.bus.subscribe()
        self._timer.start()

    def _drain(self) -> None:
        import queue

        drained = 0
        while drained < 60:
            try:
                event = self._queue.get_nowait()
            except queue.Empty:
                break
            self.event.emit(event.to_dict())
            drained += 1


class MainWindow(QMainWindow):
    def __init__(self, service: DoctorService, *, theme: str = "dark") -> None:
        super().__init__()
        self.service = service
        self.theme = theme
        self.setWindowTitle(f"{APP_NAME} — {APP_TAGLINE}")
        self.resize(1280, 820)
        self.setMinimumSize(1040, 680)

        self.pages: dict[str, QWidget] = {}
        self.nav_buttons: dict[str, QPushButton] = {}
        self.current_page = "dashboard"

        self._build_ui()
        self.bridge = EventBridge(service, self)
        self.bridge.event.connect(self.on_engine_event)
        self.apply_theme(theme)
        self.refresh_status()
        QTimer.singleShot(120, self.devices_page.refresh)

    # ------------------------------------------------------------------ ui
    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("Root")
        self.setCentralWidget(root)
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._build_sidebar())
        layout.addWidget(self._build_main(), 1)

        status = QStatusBar()
        self.setStatusBar(status)
        self.status_right = QLabel("ready")
        self.status_right.setObjectName("Dim")
        status.addWidget(QLabel(f"{APP_NAME} {__version__}"))
        status.addPermanentWidget(self.status_right)
        self._status_default = f"{APP_NAME} {__version__}"

        self._build_shortcuts()

    def _build_sidebar(self) -> QWidget:
        sidebar = QWidget()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(232)
        box = QVBoxLayout(sidebar)
        box.setContentsMargins(14, 18, 14, 16)
        box.setSpacing(4)

        brand = QHBoxLayout()
        brand.setSpacing(10)
        mark = QLabel("◎")
        mark.setObjectName("BrandMark")
        mark.setFixedSize(36, 36)
        mark.setAlignment(Qt.AlignCenter)
        brand.addWidget(mark)
        names = QVBoxLayout()
        names.setSpacing(0)
        title = QLabel("USB Doctor <span style='color:#3d8bff'>AI</span>")
        title.setObjectName("BrandTitle")
        title.setTextFormat(Qt.RichText)
        names.addWidget(title)
        version = QLabel(f"v{__version__}")
        version.setObjectName("BrandVersion")
        names.addWidget(version)
        brand.addLayout(names)
        brand.addStretch(1)
        box.addLayout(brand)
        box.addSpacing(18)

        for key, icon, caption in PAGES:
            button = QPushButton(f"  {icon}   {caption}")
            button.setObjectName("NavButton")
            button.setCheckable(True)
            button.setCursor(Qt.PointingHandCursor)
            button.setToolTip(f"{caption}  (Ctrl+{PAGES.index((key, icon, caption)) + 1})")
            button.clicked.connect(lambda _=False, k=key: self.show_page(k))
            self.nav_buttons[key] = button
            box.addWidget(button)

        box.addStretch(1)
        self.rights_label = QLabel("checking rights…")
        self.rights_label.setObjectName("Dim")
        self.rights_label.setWordWrap(True)
        box.addWidget(self.rights_label)

        self.theme_button = QPushButton("◐  Switch to light")
        self.theme_button.setObjectName("Ghost")
        self.theme_button.setCursor(Qt.PointingHandCursor)
        self.theme_button.clicked.connect(self.toggle_theme)
        box.addWidget(self.theme_button)
        return sidebar

    def _build_main(self) -> QWidget:
        main = QWidget()
        layout = QVBoxLayout(main)
        layout.setContentsMargins(26, 22, 26, 12)
        layout.setSpacing(14)

        head = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(2)
        self.page_title = QLabel("Dashboard")
        self.page_title.setObjectName("PageTitle")
        self.page_sub = QLabel("")
        self.page_sub.setObjectName("PageSub")
        titles.addWidget(self.page_title)
        titles.addWidget(self.page_sub)
        head.addLayout(titles)
        head.addStretch(1)
        self.scan_any_button = QPushButton("Scan && Repair Automatically")
        self.scan_any_button.setObjectName("Primary")
        self.scan_any_button.setCursor(Qt.PointingHandCursor)
        self.scan_any_button.clicked.connect(self.auto_scan_repair)
        head.addWidget(self.scan_any_button)
        layout.addLayout(head)

        self.stack = QStackedWidget()
        layout.addWidget(self.stack, 1)

        from usbdoctor.app.pages.assistant import AssistantPage
        from usbdoctor.app.pages.dashboard import DashboardPage
        from usbdoctor.app.pages.devices import DevicesPage
        from usbdoctor.app.pages.history import HistoryPage
        from usbdoctor.app.pages.performance import PerformancePage
        from usbdoctor.app.pages.recovery import RecoveryPage
        from usbdoctor.app.pages.repair import RepairPage
        from usbdoctor.app.pages.reports import ReportsPage
        from usbdoctor.app.pages.scan import ScanPage
        from usbdoctor.app.pages.settings import SettingsPage

        self.devices_page = DevicesPage(self.service)
        self.scan_page = ScanPage(self.service)
        self.repair_page = RepairPage(self.service)
        self.recovery_page = RecoveryPage(self.service)
        self.performance_page = PerformancePage(self.service)
        self.assistant_page = AssistantPage(self.service)
        self.history_page = HistoryPage(self.service)
        self.reports_page = ReportsPage(self.service)
        self.settings_page = SettingsPage(self.service)
        self.dashboard_page = DashboardPage(self.service)

        order = {
            "dashboard": self.dashboard_page,
            "devices": self.devices_page,
            "scan": self.scan_page,
            "repair": self.repair_page,
            "recovery": self.recovery_page,
            "performance": self.performance_page,
            "assistant": self.assistant_page,
            "history": self.history_page,
            "reports": self.reports_page,
            "settings": self.settings_page,
        }
        for key, page in order.items():
            self.pages[key] = page
            self.stack.addWidget(page)

        # Pages talk to each other through the window, never by reaching across.
        self.devices_page.deviceSelected.connect(self.on_device_selected)
        self.devices_page.scanRequested.connect(self.on_scan_requested)
        self.dashboard_page.deviceSelected.connect(self.on_device_selected)
        self.dashboard_page.scanRequested.connect(self.on_scan_requested)
        self.scan_page.scanCompleted.connect(self.on_scan_completed)
        self.scan_page.deviceChanged.connect(self.on_device_selected)
        self.repair_page.deviceChanged.connect(self.on_device_selected)
        self.recovery_page.deviceChanged.connect(self.on_device_selected)
        self.performance_page.deviceChanged.connect(self.on_device_selected)

        self.toast = Toast(self)
        return main

    def _build_shortcuts(self) -> None:
        for index, (key, _icon, _caption) in enumerate(PAGES, start=1):
            shortcut = QShortcut(QKeySequence(f"Ctrl+{index}"), self)
            shortcut.activated.connect(lambda k=key: self.show_page(k))
        refresh = QShortcut(QKeySequence("Ctrl+R"), self)
        refresh.activated.connect(self.refresh_status)
        escape = QShortcut(QKeySequence("Ctrl+Q"), self)
        escape.activated.connect(self.close)

        menu = self.menuBar().addMenu("File")
        refresh_action = QAction("Refresh Devices", self)
        refresh_action.setShortcut("Ctrl+Shift+R")
        refresh_action.triggered.connect(lambda: self.devices_page.refresh())
        menu.addAction(refresh_action)
        reports = QAction("Export Report…", self)
        reports.triggered.connect(lambda: self.show_page("reports"))
        menu.addAction(reports)
        menu.addSeparator()
        quit_action = QAction("Quit", self)
        quit_action.setShortcut("Ctrl+Q")
        quit_action.triggered.connect(self.close)
        menu.addAction(quit_action)

        view = self.menuBar().addMenu("View")
        toggle = QAction("Toggle dark / light", self)
        toggle.setShortcut("Ctrl+T")
        toggle.triggered.connect(self.toggle_theme)
        view.addAction(toggle)

        help_menu = self.menuBar().addMenu("Help")
        about = QAction(f"About {APP_NAME}", self)
        about.triggered.connect(lambda: self.show_page("settings"))
        help_menu.addAction(about)

    # --------------------------------------------------------------- theme
    def apply_theme(self, theme: str) -> None:
        self.theme = theme
        self.setStyleSheet(stylesheet(theme))
        light = theme == "light"
        self.theme_button.setText("☀  Switch to dark" if light else "◐  Switch to light")
        self.service.set_setting("theme", theme)

    def toggle_theme(self) -> None:
        self.apply_theme("light" if self.theme == "dark" else "dark")

    # ---------------------------------------------------------------- pages
    def show_page(self, key: str) -> None:
        if key not in self.pages:
            return
        self.current_page = key
        page = self.pages[key]
        self.stack.setCurrentWidget(page)
        for name, button in self.nav_buttons.items():
            button.setChecked(name == key)
        self.page_title.setText(dict((k, c) for k, _i, c in PAGES)[key])
        self.page_sub.setText(getattr(page, "SUBTITLE", ""))
        if hasattr(page, "on_shown"):
            page.on_shown()

    # --------------------------------------------------------------- events
    def on_device_selected(self, path: str) -> None:
        self.selected_device = path
        for key in ("scan", "repair", "recovery", "performance", "assistant", "reports"):
            page = self.pages[key]
            if hasattr(page, "set_device") and page is not self.sender():
                page.set_device(path)
        self.dashboard_page.set_device(path)
        self.devices_page.select_path(path)

    def on_scan_requested(self, path: str) -> None:
        self.on_device_selected(path)
        self.show_page("scan")
        self.scan_page.start_scan()

    def on_scan_completed(self, result: dict) -> None:
        for key in ("dashboard", "assistant", "performance", "reports", "repair"):
            page = self.pages[key]
            if hasattr(page, "apply_scan"):
                page.apply_scan(result)
        self.history_page.reload()
        self.toast.show_message(
            f"Scan finished: {result['health']['verdict']} — {result['diagnosis']['headline']}"
        )

    def auto_scan_repair(self) -> None:
        path = getattr(self, "selected_device", None)
        if not path:
            devices = self.devices_page.device_paths()
            path = devices[0] if devices else None
        if not path:
            self.toast.show_message("Connect a drive first.")
            return
        self.on_device_selected(path)
        self.show_page("scan")
        self.scan_page.start_scan(auto_repair=True)

    def on_engine_event(self, event: dict) -> None:
        """Fan events out to whichever page is driving a job."""
        for page in self.pages.values():
            if hasattr(page, "on_engine_event"):
                page.on_engine_event(event)
        if event["type"] == "stage":
            message = event["payload"].get("message")
            if message:
                self.status_right.setText(message)

    def refresh_status(self) -> None:
        status = self.service.status()
        if status["is_admin"]:
            self.rights_label.setText("Administrator rights active")
        else:
            self.rights_label.setText(
                "Standard user — raw device access is limited.\nRun as administrator/root for full results."
            )
        self.status_right.setText(f"{status['platform']} · {status['stats']['scans']} scans recorded")
        self.devices_page.refresh()
        self.dashboard_page.reload()

    def notify(self, message: str, tone: str = "info") -> None:
        self.toast.show_message(message)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self.service.set_setting("window_geometry", [
            self.width(), self.height(), self.x(), self.y()
        ])
        super().closeEvent(event)


def create_window(*, state_dir: Optional[Path] = None, theme: str = "dark") -> MainWindow:
    service = DoctorService(state_dir=state_dir)
    saved_theme = service.history.get_setting("theme")
    return MainWindow(service, theme=saved_theme if saved_theme in ("dark", "light") else theme)
