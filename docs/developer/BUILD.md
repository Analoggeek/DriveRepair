# Building, signing and releasing

## Prerequisites

| Target | Needs |
| --- | --- |
| Engine / CLI / web (all platforms) | Python 3.9+, no third-party packages |
| Desktop app | `PySide6-Essentials>=6.5` |
| Windows build | Python 3.9+ 64-bit, Inno Setup 6, optional Windows SDK (`signtool`) |
| macOS build | Xcode command line tools, Python 3.9+ (universal2), optional `create-dmg` |

```bash
python -m venv .venv
. .venv/bin/activate           # Windows: .venv\Scripts\activate
pip install -e ".[desktop,dev,package]"
```

---

## Windows

```powershell
powershell -ExecutionPolicy Bypass -File installers\windows\build.ps1
```

The script, in order:

1. Verifies the Python toolchain.
2. Creates `.venv-build` and installs desktop + packaging extras.
3. **Runs the test suite** — a failing suite aborts the build.
4. Generates the icons (`tools/make_icons.py`).
5. Runs PyInstaller with `installers/USBDoctorAI.spec` → `dist\USBDoctorAI\` and
   `dist\USBDoctorAI.exe`.
6. **Smoke-tests the frozen binary** by running `USBDoctorAI.exe status --json`.
7. Optionally signs (see below).
8. Runs Inno Setup → `dist\USBDoctorAI-Setup-1.0.0.exe`.

Useful flags: `-SkipInstaller` (portable builds only), `-Sign` (Authenticode).

### Signing (recommended for distribution)

```powershell
$env:CODESIGN_CERT = "THUMBPRINT_OF_CERT"
powershell -ExecutionPolicy Bypass -File installers\windows\build.ps1 -Sign
```

Signing order matters: sign the inner executable, then the installer. The installer script
uses `/tr` timestamping and `/td sha256`, and verifies each signature with `signtool verify /pa`.

If you sign the installer after building it separately:

```powershell
signtool sign /tr http://timestamp.digicert.com /td sha256 /fd sha256 /sha1 $CERT dist\USBDoctorAI-Setup-1.0.0.exe
signtool verify /pa dist\USBDoctorAI-Setup-1.0.0.exe
```

An EV certificate avoids SmartScreen reputation warm-up entirely (it is a subscription, not a
one-off file).

---

## macOS

```bash
./installers/macos/build.sh                 # build + DMG
SIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)" ./installers/macos/build.sh --sign
```

The script:

1. Checks it is running on Darwin with Python 3.9+.
2. Installs desktop + packaging extras into `.venv-build`.
3. **Runs the test suite.**
4. Generates `.icns` and PNGs.
5. Runs PyInstaller with `installers/macos/USBDoctorAI.spec` → `dist/USBDoctorAI.app`.
6. Prints the bundle architecture from `lipo -archs`.
7. Smoke-tests the frozen binary.
8. With `--sign`: signs inside-out (nested Mach-O first, then the bundle) with hardened
   runtime and `installers/macos/entitlements.plist`, then verifies.
9. Builds the DMG (`create-dmg` if present, else `hdiutil`).
10. With `NOTARY_PROFILE` set: `notarytool submit --wait`, `stapler staple`.

### Universal binary

Build once on each architecture, then merge:

```bash
# On an Apple Silicon machine
./installers/macos/build.sh                       # → dist/USBDoctorAI.app (arm64)
mv dist/USBDoctorAI.app dist/USBDoctorAI-arm64.app

# On an Intel machine
./installers/macos/build.sh                       # → dist/USBDoctorAI.app (x86_64)
```

Merge the two Mach-O trees with `lipo` (the Python framework and any bundled dylibs must both
be universal), then confirm:

```bash
lipo -archs dist/USBDoctorAI.app/Contents/MacOS/USBDoctorAI
# → x86_64 arm64
```

`PySide6` wheels from PyPI are universal2, so the merge works when both builds used the same
wheel version and the interpreter is universal2.

### Notarization, step by step

```bash
# one-time credential storage
xcrun notarytool store-credentials "usbdoctor-notary" \
  --apple-id "you@example.com" --team-id "TEAMID" --password "app-specific-password"

# build and notarize
SIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)" \
NOTARY_PROFILE="usbdoctor-notary" \
./installers/macos/build.sh --sign

# verify
spctl --assess --type open --context context:primary-signature -v dist/USBDoctorAI-1.0.0.dmg
xcrun stapler validate dist/USBDoctorAI-1.0.0.dmg
```

Notarization requires a **Developer ID** certificate (not Apple Development) and a hardened
runtime. The entitlements file grants only: USB device access, user-selected file read/write,
network client (for allow-listed research) and a narrow `/dev/` exception for raw device paths.

### Gatekeeper expectations after notarization

| Scenario | Result |
| --- | --- |
| Signed + notarized + stapled | Opens with no prompt |
| Signed, not notarized | "Apple cannot check it for malicious software" |
| Unsigned | Blocked by default; right-click → Open once |

---

## Linux (reference build)

The specs build on Linux too, which is useful for verifying that the frozen bundle is
self-contained. Note that a *system* Python without `libpython3.x.so` cannot be frozen — use a
pyenv/conda interpreter that has the shared library, or install the matching `-dev` package.

```bash
pyinstaller installers/USBDoctorAI.spec --noconfirm --clean
./dist/USBDoctorAI/USBDoctorAI status --json
```

---

## Release checklist

- [ ] `python -m pytest tests -q` is green (177 tests).
- [ ] `python tools/make_icons.py` regenerated after any logo change.
- [ ] Version bumped in `usbdoctor/version.py` **and** in the installer scripts
      (`installers/windows/usbdoctor.iss`, both `.spec` bundles).
- [ ] Windows: installer signed, verified with `signtool verify /pa`.
- [ ] macOS: `codesign --verify --deep --strict`, `spctl --assess`, `stapler validate`.
- [ ] Frozen binary smoke test passed on both platforms (`status --json`).
- [ ] Install, launch, scan a **known-good** drive, confirm the verdict is sensible.
- [ ] Install on a clean VM with no Python present.
- [ ] Uninstall leaves no services, drivers or scheduled tasks behind.
- [ ] `docs/user/TROUBLESHOOTING.md` updated with anything new that bit you.

---

## What the package contains

| Path | Purpose |
| --- | --- |
| `usbdoctor/` | The application |
| `launcher.py` | Frozen entry point (GUI + CLI dispatch) |
| `installers/` | PyInstaller specs, Inno Setup script, build scripts, entitlements |
| `tools/make_icons.py` | Icon generation |
| `docs/` | User and developer documentation |
| `tests/` | Test suite and image fixtures |
| `pyproject.toml` | Packaging metadata and entry points |

No third-party tool is bundled. `smartctl`, `ddrescue` and `testdisk` are detected at runtime
and remain the user's own installations from their official sources.

---

## Reproducibility notes

- Pin `PySide6-Essentials` and `pyinstaller` versions for release builds.
- Build on the **oldest** OS you intend to support (Windows 10 builds run on 11; the reverse is
  not guaranteed).
- The macOS `LSMinimumSystemVersion` is 11.0; raising it must be reflected in the bundle
  metadata.
- Icons are generated from code, so a rebuild reproduces them byte-for-byte as long as Qt's
  rasteriser version matches. If that matters for signing manifests, commit the generated
  assets instead of regenerating.
