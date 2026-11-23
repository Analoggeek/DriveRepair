# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — Windows build.

Produces a single-folder application (the form the installer ships):

    pyinstaller installers/USBDoctorAI.spec --noconfirm --clean

Output:
    dist/USBDoctorAI/USBDoctorAI.exe     (single-folder, fast start, what Inno Setup packages)

A one-file build is deliberately NOT produced: PyInstaller cannot emit both a
COLLECT directory and a differently-named one-file EXE from a single spec, and
a one-file bundle would unpack to %TEMP% on every launch — unacceptable for a
tool that must be run as administrator against raw disk devices.
"""

import sys
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent          # noqa: F821 - provided by PyInstaller
ASSETS = ROOT / "usbdoctor" / "frontend" / "web"

icon = str(ASSETS / "assets" / "app.ico")
if not Path(icon).exists():
    icon = None

datas = [
    (str(ASSETS / "index.html"), "usbdoctor/frontend/web"),
    (str(ASSETS / "app.css"), "usbdoctor/frontend/web"),
    (str(ASSETS / "app.js"), "usbdoctor/frontend/web"),
    (str(ASSETS / "assets"), "usbdoctor/frontend/web/assets"),
]

hiddenimports = [
    "usbdoctor.app.main",
    "usbdoctor.app.main_window",
    "usbdoctor.app.pages.dashboard",
    "usbdoctor.app.pages.devices",
    "usbdoctor.app.pages.scan",
    "usbdoctor.app.pages.repair",
    "usbdoctor.app.pages.recovery",
    "usbdoctor.app.pages.performance",
    "usbdoctor.app.pages.assistant",
    "usbdoctor.app.pages.history",
    "usbdoctor.app.pages.reports",
    "usbdoctor.app.pages.settings",
    "usbdoctor.webapi.server",
    "usbdoctor.devices.windows",
    # The main window imports its pages lazily, so PyInstaller's static
    # analysis cannot see them; without these the frozen GUI fails at start-up.
    "usbdoctor.app.theme",
    "usbdoctor.app.widgets.common",
    "usbdoctor.report",
    "usbdoctor.knowledge.database",
    "usbdoctor.knowledge.online",
    "usbdoctor.ai.assistant",
]

a = Analysis(  # noqa: F821
    [str(ROOT / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter", "test", "unittest", "pydoc_data",
        "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.Qt3DCore",
        "PySide6.QtMultimedia", "PySide6.QtQuick", "PySide6.QtQml", "PySide6.QtSql",
        "PySide6.QtTest", "PySide6.QtCharts", "PySide6.QtDataVisualization",
    ],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="USBDoctorAI",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # GUI app: no console window on Windows
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon,
    version=str(ROOT / "installers" / "windows" / "version_info.txt"),
)

# Ship the single-folder build: this is what the installer packages.
coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="USBDoctorAI",
)
