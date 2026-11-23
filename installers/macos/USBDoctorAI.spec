# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — macOS universal build (Intel x86_64 + Apple Silicon arm64).

Build on macOS with:

    arch -arm64  pyinstaller installers/macos/USBDoctorAI.spec -y    # Apple Silicon
    arch -x86_64 pyinstaller installers/macos/USBDoctorAI.spec -y    # Intel

The produced .app is single-architecture: PyInstaller bundles the interpreter and
the PySide6 Qt libraries it finds, so a universal binary requires universal2
wheels for BOTH. `installers/macos/build.sh` builds the native architecture and
reports it with `lipo -archs`; the CI matrix builds each architecture on its own
runner and ships two DMGs rather than pretending one binary covers both.
"""

from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent.parent.parent   # noqa: F821 - provided by PyInstaller
ASSETS = ROOT / "usbdoctor" / "frontend" / "web"

icon = str(ASSETS / "assets" / "app.icns")
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
    "usbdoctor.devices.macos",
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
    ],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="USBDoctorAI",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,             # UPX breaks macOS code signing
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,      # set by the build architecture
    codesign_identity=None,
    entitlements_file=None,
    icon=icon,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="USBDoctorAI",
)

app = BUNDLE(  # noqa: F821
    coll,
    name="USBDoctorAI.app",
    icon=icon,
    bundle_identifier="ai.usbdoctor.app",
    version="1.0.0",
    info_plist={
        "CFBundleName": "USB Doctor AI",
        "CFBundleDisplayName": "USB Doctor AI",
        "CFBundleShortVersionString": "1.0.0",
        "CFBundleVersion": "1.0.0",
        "NSHighResolutionCapable": True,
        "NSRequiresAquaSystemAppearance": False,
        "LSMinimumSystemVersion": "11.0",
        # Raw device access on macOS requires user consent for removable media.
        "NSRemovableVolumesUsageDescription":
            "USB Doctor AI needs to read and repair external drives you select.",
        "NSDesktopFolderUsageDescription":
            "USB Doctor AI saves reports and recovered files where you choose.",
        "NSDocumentsFolderUsageDescription":
            "USB Doctor AI saves reports and recovered files where you choose.",
        "LSApplicationCategoryType": "public.app-category.utilities",
        "LSArchitecturePriority": ["arm64", "x86_64"],
        "NSHumanReadableCopyright": "Copyright © 2026 USB Doctor AI",
    },
)
