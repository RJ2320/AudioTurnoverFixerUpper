#!/usr/bin/env bash
# Build AudioTurnoverFixerupper.app (and optional .dmg) on macOS.
# Requires: Python 3.11+ from python.org (Apple Silicon or Intel), Xcode CLT.
# Run from this script's directory.
#
# Usage:
#   ./build_mac.sh           # builds the .app under dist/
#   ./build_mac.sh dmg       # also wraps it in a drag-to-install .dmg

set -euo pipefail
cd "$(dirname "$0")"

APP_NAME="AudioTurnoverFixerupper"
ENTRY="edl_sound_roll_gui.py"
PY=python3

# 1. Isolated venv so we don't touch the system Python.
echo "==> Creating virtualenv (.venv-mac)..."
$PY -m venv .venv-mac
# shellcheck disable=SC1091
source .venv-mac/bin/activate
pip install --upgrade pip --quiet
pip install PySide6 wavinfo pyinstaller --quiet

# 2. Build a Cocoa-style .app bundle. --windowed gives no console;
#    --onedir is faster to launch than --onefile and is the standard
#    PyInstaller layout for .app bundles on macOS.
echo "==> Running PyInstaller..."
pyinstaller \
    --noconfirm \
    --windowed \
    --name "$APP_NAME" \
    --collect-submodules wavinfo \
    --collect-data wavinfo \
    "$ENTRY"

APP_PATH="dist/$APP_NAME.app"
if [ ! -d "$APP_PATH" ]; then
    echo "Build did not produce $APP_PATH" >&2
    exit 1
fi
echo "==> Built: $APP_PATH"

# 3. Optional .dmg packaging.
if [ "${1:-}" = "dmg" ]; then
    DMG="dist/$APP_NAME.dmg"
    rm -f "$DMG"
    STAGING="$(mktemp -d)"
    cp -R "$APP_PATH" "$STAGING/"
    ln -s /Applications "$STAGING/Applications"
    echo "==> Creating $DMG ..."
    hdiutil create -volname "$APP_NAME" -srcfolder "$STAGING" -ov -format UDZO "$DMG"
    rm -rf "$STAGING"
    echo "==> Built: $DMG"
fi

echo
echo "Done. Drag $APP_PATH (or the .dmg contents) into /Applications."
echo
echo "First-launch note: macOS will block an unsigned app with"
echo '  "AudioTurnoverFixerupper cannot be opened because the developer cannot be verified."'
echo 'Right-click the .app → Open → Open. After that it launches normally.'
echo "To ship publicly without that dialog you would need an Apple Developer ID"
echo "and codesign + notarytool. For in-house use this is fine."
