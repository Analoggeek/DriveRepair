#!/usr/bin/env bash
# Build USB Doctor AI for macOS: universal .app, DMG, and (optionally) sign + notarize.
#
#   ./installers/macos/build.sh                 # build + DMG
#   SIGN_IDENTITY="Developer ID Application: …" ./installers/macos/build.sh --sign
#
# Requirements
#   Xcode command line tools, Python 3.9+ (universal2 build recommended), and
#   `create-dmg` (brew install create-dmg) for the styled disk image.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

DO_SIGN=0
[[ "${1:-}" == "--sign" ]] && DO_SIGN=1

step() { printf '\n\033[36m==> %s\033[0m\n' "$1"; }
fail() { printf '\033[31mERROR:\033[0m %s\n' "$1" >&2; exit 1; }

step "Checking the toolchain"
command -v python3 >/dev/null || fail "python3 not found. Install Python 3.9+ (universal2)."
[[ "$(uname -s)" == "Darwin" ]] || fail "This build must run on macOS."
python3 - <<'PY'
import platform, sys
print(f"Python {platform.python_version()} on {platform.machine()}")
if sys.version_info < (3, 9):
    raise SystemExit("Python 3.9 or newer is required.")
PY

step "Creating a build environment"
[[ -d .venv-build ]] || python3 -m venv .venv-build
PY_BIN=".venv-build/bin/python"
"$PY_BIN" -m pip install --upgrade pip --quiet
"$PY_BIN" -m pip install --quiet "PySide6-Essentials>=6.5" "pyinstaller>=6.0" "Pillow>=10.0" pytest

step "Running the test suite"
"$PY_BIN" -m pytest tests -q || fail "Tests failed — fix them before packaging."

step "Generating icons"
QT_QPA_PLATFORM=offscreen "$PY_BIN" tools/make_icons.py

step "Building the .app bundle"
rm -rf build dist
"$PY_BIN" -m PyInstaller installers/macos/USBDoctorAI.spec --noconfirm --clean
[[ -d "dist/USBDoctorAI.app" ]] || fail "The .app bundle was not produced."

ARCHS="$(lipo -archs dist/USBDoctorAI.app/Contents/MacOS/USBDoctorAI 2>/dev/null || echo unknown)"
printf 'Bundle architecture: %s\n' "$ARCHS"

step "Smoke-testing the bundle"
if ./dist/USBDoctorAI.app/Contents/MacOS/USBDoctorAI status --json >/tmp/usbdoctor-smoke.json 2>/dev/null; then
  echo "Frozen binary responded correctly."
else
  echo "WARNING: the frozen binary did not return a status; verify manually." >&2
fi

if [[ $DO_SIGN -eq 1 ]]; then
  step "Signing"
  [[ -n "${SIGN_IDENTITY:-}" ]] || fail "Set SIGN_IDENTITY to your 'Developer ID Application: …' identity."
  APP="dist/USBDoctorAI.app"
  # Inside-out signing: nested binaries first, then the bundle.
  find "$APP/Contents" -type f -perm +111 -print0 2>/dev/null | while IFS= read -r -d '' bin; do
    case "$bin" in
      *.dylib|*.so|*/MacOS/*|*/Frameworks/*)
        codesign --force --timestamp --options runtime --sign "$SIGN_IDENTITY" "$bin" || true ;;
    esac
  done
  codesign --force --deep --timestamp --options runtime \
    --entitlements installers/macos/entitlements.plist \
    --sign "$SIGN_IDENTITY" "$APP"
  codesign --verify --deep --strict --verbose=2 "$APP" || fail "Signature verification failed."
  spctl --assess --type execute --verbose=2 "$APP" || echo "Gatekeeper assessment pending notarization."
fi

step "Building the DMG"
rm -f dist/USBDoctorAI-1.0.0.dmg
if command -v create-dmg >/dev/null; then
  create-dmg \
    --volname "USB Doctor AI" \
    --volicon usbdoctor/frontend/web/assets/app.icns \
    --window-pos 200 120 --window-size 640 400 \
    --icon-size 96 --icon "USBDoctorAI.app" 160 180 \
    --app-drop-link 470 180 \
    --hide-extension "USBDoctorAI.app" \
    "dist/USBDoctorAI-1.0.0.dmg" "dist/USBDoctorAI.app" || \
  hdiutil create -volname "USB Doctor AI" -srcfolder dist/USBDoctorAI.app \
    -ov -format UDZO "dist/USBDoctorAI-1.0.0.dmg"
else
  echo "create-dmg not installed (brew install create-dmg); using hdiutil."
  hdiutil create -volname "USB Doctor AI" -srcfolder dist/USBDoctorAI.app \
    -ov -format UDZO "dist/USBDoctorAI-1.0.0.dmg"
fi
codesign --force --timestamp --sign "${SIGN_IDENTITY:-adhoc-placeholder}" dist/USBDoctorAI-1.0.0.dmg 2>/dev/null || \
  echo "DMG left unsigned (set SIGN_IDENTITY to sign it)."

if [[ $DO_SIGN -eq 1 ]]; then
  step "Notarizing"
  if [[ -n "${NOTARY_PROFILE:-}" ]]; then
    xcrun notarytool submit "dist/USBDoctorAI-1.0.0.dmg" --keychain-profile "$NOTARY_PROFILE" --wait
    xcrun stapler staple "dist/USBDoctorAI-1.0.0.dmg"
    spctl --assess --type open --context context:primary-signature --verbose=2 "dist/USBDoctorAI-1.0.0.dmg" || true
  else
    echo "Set NOTARY_PROFILE to a 'xcrun notarytool store-credentials' profile to notarize."
  fi
fi

step "Done"
ls -lh dist/USBDoctorAI-1.0.0.dmg
echo "App  : dist/USBDoctorAI.app"
echo "CLI  : dist/USBDoctorAI.app/Contents/MacOS/USBDoctorAI status"
