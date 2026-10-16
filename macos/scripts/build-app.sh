#!/bin/bash
# Builds Workstreams.app, the native macOS dashboard, into macos/build/.
#   macos/scripts/build-app.sh            release build
#   macos/scripts/build-app.sh --open     ... and open it
# The app runs `wm app-server`. It looks for wm in: $WM_BIN, Settings, this checkout's .venv/bin/wm, then PATH.
set -euo pipefail
cd "$(dirname "$0")/.."
REPO=$(cd .. && pwd)
APP=build/Workstreams.app

swift build -c release --product WM
BIN=$(swift build -c release --show-bin-path)

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN/WM" "$APP/Contents/MacOS/WM"
[ -d "$BIN/SwiftTerm_SwiftTerm.bundle" ] && cp -R "$BIN/SwiftTerm_SwiftTerm.bundle" "$APP/Contents/Resources/"

ICONSET=$(mktemp -d)/AppIcon.iconset
swift scripts/make-icon.swift "$ICONSET"
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"

WM_PATH=""
[ -x "$REPO/.venv/bin/wm" ] && WM_PATH="$REPO/.venv/bin/wm"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>Workstreams</string>
    <key>CFBundleDisplayName</key><string>Workstreams</string>
    <key>CFBundleIdentifier</key><string>dev.agentsmanager.workstreams</string>
    <key>CFBundleExecutable</key><string>WM</string>
    <key>CFBundleIconFile</key><string>AppIcon</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>0.1.0</string>
    <key>CFBundleVersion</key><string>1</string>
    <key>LSMinimumSystemVersion</key><string>14.0</string>
    <key>LSApplicationCategoryType</key><string>public.app-category.developer-tools</string>
    <key>NSHighResolutionCapable</key><true/>
    <key>NSPrincipalClass</key><string>NSApplication</string>
    <key>WMPath</key><string>$WM_PATH</string>
</dict>
</plist>
PLIST

codesign --force --deep --sign - "$APP" 2>/dev/null
echo "built $(pwd)/$APP"
[ "${1:-}" = "--open" ] && open "$APP"
true
