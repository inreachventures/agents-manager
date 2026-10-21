#!/bin/bash
# Builds Workstreams.app, the native macOS dashboard, into macos/build/.
#   macos/scripts/build-app.sh                  for this Mac, running this checkout's wm: build/Workstreams.app
#   macos/scripts/build-app.sh --open           ... and open it
#   macos/scripts/build-app.sh --release ARCH   self-contained, with wm and a Python of its own inside, plus a disk
#                                               image to hand out; ARCH: arm64 (Apple silicon), x86_64 (Intel) or all.
#                                               build/ARCH/Workstreams.app, build/Workstreams-VERSION-CHIP.dmg
# The development build looks for wm in: $WM_BIN, Settings, this checkout's .venv/bin/wm, then PATH; a release build
# uses the wm inside it unless Settings names another.
# Both are signed ad hoc: they run on the Mac that built them. Handing one out without macOS blocking it as from an
# unidentified developer takes a Developer ID signature and notarization.
set -euo pipefail
cd "$(dirname "$0")/.."
REPO=$(cd .. && pwd)
VERSION=$(sed -n 's/^version = "\(.*\)"/\1/p' "$REPO/pyproject.toml")

# app ARCH DIR [WM_PATH]: DIR/Workstreams.app for ARCH ("" = this Mac's), without wm inside
app() {
    local arch=$1 app=$2/Workstreams.app wm_path=${3:-} flags=(-c release)
    [ -n "$arch" ] && flags+=(--arch "$arch")
    swift build "${flags[@]}" --product WM
    local bin; bin=$(swift build "${flags[@]}" --show-bin-path)

    rm -rf "$app"
    mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"
    cp "$bin/WM" "$app/Contents/MacOS/WM"
    [ -d "$bin/SwiftTerm_SwiftTerm.bundle" ] && cp -R "$bin/SwiftTerm_SwiftTerm.bundle" "$app/Contents/Resources/"

    local iconset; iconset=$(mktemp -d)/AppIcon.iconset
    swift scripts/make-icon.swift "$iconset"
    iconutil -c icns "$iconset" -o "$app/Contents/Resources/AppIcon.icns"

    # NSMicrophoneUsageDescription: macOS counts the tmux server the app starts, and so every Claude session in it,
    # as the app. Without a usage string it never asks, and Claude Code's voice dictation gets no microphone.
    cat > "$app/Contents/Info.plist" <<PLIST
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
    <key>CFBundleShortVersionString</key><string>$VERSION</string>
    <key>CFBundleVersion</key><string>1</string>
    <key>LSMinimumSystemVersion</key><string>14.0</string>
    <key>LSApplicationCategoryType</key><string>public.app-category.developer-tools</string>
    <key>NSHighResolutionCapable</key><true/>
    <key>NSPrincipalClass</key><string>NSApplication</string>
    <key>NSMicrophoneUsageDescription</key><string>Claude Code's voice dictation, in your workstreams' sessions, uses the microphone.</string>
$([ -n "$wm_path" ] && echo "    <key>WMPath</key><string>$wm_path</string>")
</dict>
</plist>
PLIST
}

# sign APP: each binary inside first (wm's Python), then the app, which seals the rest
sign() {
    local app=$1
    if [ -d "$app/Contents/Resources/wm" ]; then
        find "$app/Contents/Resources/wm" -type f \( -name '*.so' -o -name '*.dylib' -o -perm -0100 \) -print0 |
            while IFS= read -r -d '' f; do
                case $(file -b "$f") in Mach-O*) codesign --force --sign - "$f" ;; esac
            done
    fi
    codesign --force --sign - "$app"
    codesign --verify --deep --strict "$app"
}

release() {
    local arch=$1 dir=build/$1 chip
    [ "$arch" = arm64 ] && chip=apple-silicon || chip=intel
    rm -rf "$dir" && mkdir -p "$dir"
    app "$arch" "$dir"
    scripts/bundle-wm.sh "$arch" "$dir/Workstreams.app/Contents/Resources/wm"
    sign "$dir/Workstreams.app"

    local dmg=build/Workstreams-$VERSION-$chip.dmg stage; stage=$(mktemp -d)
    ditto "$dir/Workstreams.app" "$stage/Workstreams.app"
    ln -s /Applications "$stage/Applications"  # drag the app onto it
    hdiutil create -quiet -volname Workstreams -srcfolder "$stage" -fs HFS+ -format UDZO -ov "$dmg"
    rm -rf "$stage"
    echo "built $(pwd)/$dir/Workstreams.app and $(pwd)/$dmg ($(du -h "$dmg" | cut -f1))"
}

case ${1:-} in
    --release)
        case ${2:-} in
            arm64 | x86_64) release "$2" ;;
            all) release arm64 && release x86_64 ;;
            *) echo "usage: build-app.sh --release arm64|x86_64|all" >&2; exit 2 ;;
        esac
        ;;
    "" | --open)
        WM_PATH=""
        [ -x "$REPO/.venv/bin/wm" ] && WM_PATH="$REPO/.venv/bin/wm"
        app "" build "$WM_PATH"
        sign build/Workstreams.app
        echo "built $(pwd)/build/Workstreams.app"
        [ "${1:-}" = "--open" ] && open build/Workstreams.app
        ;;
    *) echo "usage: build-app.sh [--open | --release arm64|x86_64|all]" >&2; exit 2 ;;
esac
true
