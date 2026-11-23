# Installers

| Path | Builds |
| --- | --- |
| `USBDoctorAI.spec` | Windows PyInstaller build (one-file + single-folder) |
| `windows/build.ps1` | Full Windows pipeline: test → icons → freeze → smoke test → sign → Inno Setup |
| `windows/usbdoctor.iss` | Inno Setup installer (elevated, 64-bit, per-machine) |
| `windows/version_info.txt` | Windows file/version resource |
| `macos/USBDoctorAI.spec` | macOS `.app` bundle (universal-capable) |
| `macos/build.sh` | macOS pipeline: test → icons → freeze → sign → DMG → notarize |
| `macos/entitlements.plist` | Hardened-runtime entitlements (USB + user-selected paths only) |

Full instructions, including signing and notarization: [`../docs/developer/BUILD.md`](../docs/developer/BUILD.md).

No third-party tool is bundled with the installer. `smartctl`, `ddrescue` and `testdisk`
remain the user's own installations, and the app detects them at runtime.
