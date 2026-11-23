"""Desktop UI smoke tests.

PySide6 is an optional dependency, so these skip cleanly when it is absent. They
construct the real window and drive real scans through it — a UI that cannot
render or cannot run a scan is a release blocker.
"""

from __future__ import annotations

import os

import pytest

from tests.support import wait_for

PySide6 = pytest.importorskip("PySide6", reason="desktop UI not installed")

from PySide6.QtWidgets import QApplication  # noqa: E402

from tests.fixtures import images as F  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def window(qapp, tmp_path, monkeypatch):
    from usbdoctor.core.model import DeviceRef
    from usbdoctor.service import DoctorService
    from usbdoctor.app.main_window import MainWindow

    # A real damaged image, presented as an attached drive.
    path = tmp_path / "damaged.img"
    geo = F.build_fat32(path, 8 * 1024 * 1024)
    F.add_fat32_file(path, "DATA.TXT", b"recover me", geo)
    data = bytearray(path.read_bytes())
    data[0:512] = b"\x00" * 512
    path.write_bytes(data)

    device = DeviceRef(
        path=str(path), kind="image", platform="linux", size_bytes=path.stat().st_size,
        bus="usb", usb_speed="USB 2.0 High Speed (480 Mbps)", vendor="Test", model="Drive",
        filesystem="fat32",
    )

    import usbdoctor.service.engine as engine

    class _Listing:
        devices = [device]
        warnings: list[str] = []
        source = "test"

    monkeypatch.setattr(engine, "enumerate_devices", lambda include_internal=True: _Listing())

    service = DoctorService(state_dir=tmp_path / "state")
    win = MainWindow(service, theme="dark")
    win.resize(1280, 900)
    yield win
    win.close()


class TestDesktopShell:
    def test_all_ten_pages_construct_and_render(self, window, qapp):
        assert len(window.pages) == 10
        for key, page in window.pages.items():
            window.show_page(key)
            qapp.processEvents()
            assert window.stack.currentWidget() is page
            assert not page.isHidden()

    def test_theme_toggle_switches_palette(self, window):
        assert window.theme == "dark"
        window.toggle_theme()
        assert window.theme == "light"
        assert window.service.history.get_setting("theme") == "light"
        window.toggle_theme()
        assert window.theme == "dark"

    def test_window_can_render_to_a_pixmap(self, window):
        pixmap = window.grab()
        assert not pixmap.isNull()
        assert pixmap.width() > 800 and pixmap.height() > 600

    def test_device_list_is_populated_from_real_enumeration(self, window, qapp):
        window.devices_page.refresh()
        qapp.processEvents()
        assert window.devices_page.device_paths()
        assert window.devices_page.table.rowCount() == 1

    def test_scan_runs_through_the_ui_and_renders_a_verdict(self, window, qapp):
        import time

        window.devices_page.refresh()
        window.on_device_selected(window.devices_page.device_paths()[0])
        window.show_page("scan")
        window.scan_page.start_scan()
        def finished():
            qapp.processEvents()
            return window.scan_page.job_id is None

        wait_for(finished, timeout=120, message="the scan never completed")
        assert window.scan_page.verdict_badge.text() in ("WARNING", "CRITICAL")
        assert "repairable" in window.scan_page.headline.text().lower()

    def test_repair_page_loads_a_real_plan(self, window, qapp, tmp_path):
        window.devices_page.refresh()
        window.on_device_selected(window.devices_page.device_paths()[0])
        window.show_page("repair")
        window.repair_page.load_plans()
        qapp.processEvents()
        operations = [plan["operation_id"] for plan in window.repair_page.plans]
        assert operations == ["fat-boot"]
        assert window.repair_page.repair_button.isEnabled()

    def test_assistant_page_requires_a_scan_first(self, window, qapp):
        window.show_page("assistant")
        qapp.processEvents()
        assert "Run a health scan" in window.assistant_page.diagnosis_view.toPlainText()
