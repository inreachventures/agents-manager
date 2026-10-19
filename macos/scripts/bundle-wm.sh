#!/bin/bash
# Puts wm, with a Python of its own, into DEST: what makes Workstreams.app self-contained.
#   macos/scripts/bundle-wm.sh arm64|x86_64 DEST
# DEST/python  CPython from python-build-standalone (relocatable), pinned and checksummed below
# DEST/bin/wm  the command: the app, hooks and tmux key bindings call it by this path, Claude sessions by name
# wm's dependencies are pinned, with hashes, in macos/wm-requirements.txt.
set -euo pipefail
ARCH=${1:?usage: bundle-wm.sh arm64|x86_64 DEST}
DEST=${2:?usage: bundle-wm.sh arm64|x86_64 DEST}
mkdir -p "$(dirname "$DEST")" && DEST=$(cd "$(dirname "$DEST")" && pwd)/$(basename "$DEST")
cd "$(dirname "$0")/../.."
REPO=$(pwd)
CACHE=$REPO/macos/build/cache

PY=3.12 PY_RELEASE=20261001 PY_VERSION=3.12.15
sha256() {  # of the archives below, as listed in the release's SHA256SUMS
    case $1 in
        aarch64-apple-darwin) echo 10cab8f6ed6202fdd81637aa6eda4af8d5b7eaa8fc42f9df3c6bea4923de0d93 ;;
        x86_64-apple-darwin) echo d101ac54bc34afff54741406261325dc896b7b646a36a58fff4845ef0a00b2ce ;;
    esac
}
triple() { [ "$1" = arm64 ] && echo aarch64-apple-darwin || echo x86_64-apple-darwin; }

# CACHE/python-<triple>: an unpacked Python, checksum verified
python_for() {
    local triple=$1 file="cpython-$PY_VERSION+$PY_RELEASE-$1-install_only_stripped.tar.gz"
    local dir=$CACHE/python-$triple
    if [ ! -x "$dir/bin/python3" ]; then
        mkdir -p "$CACHE"
        [ -f "$CACHE/$file" ] || curl -fsSL -o "$CACHE/$file" \
            "https://github.com/astral-sh/python-build-standalone/releases/download/$PY_RELEASE/${file/+/%2B}"
        echo "$(sha256 "$triple")  $CACHE/$file" | shasum -a 256 -c --quiet
        rm -rf "$dir" "$dir.tmp" && mkdir -p "$dir.tmp"
        tar -xzf "$CACHE/$file" -C "$dir.tmp"
        mv "$dir.tmp/python" "$dir" && rmdir "$dir.tmp"
    fi
    echo "$dir"
}

TARGET=$(triple "$ARCH")
case $(uname -m) in arm64) HOST=aarch64-apple-darwin ;; *) HOST=x86_64-apple-darwin ;; esac
TOOL=$(python_for "$HOST")/bin/python3  # runs pip and compileall here, whatever DEST's architecture
PIP=("$TOOL" -m pip --disable-pip-version-check --no-cache-dir --quiet)

rm -rf "$DEST" && mkdir -p "$DEST/bin"
cp -R "$(python_for "$TARGET")" "$DEST/python"
SITE=$DEST/python/lib/python$PY/site-packages

WHEELS=$(mktemp -d)
trap 'rm -rf "$WHEELS"' EXIT
"${PIP[@]}" wheel --no-deps --wheel-dir "$WHEELS" "$REPO"
case $ARCH in arm64) PLATFORM=macosx_14_0_arm64 ;; *) PLATFORM=macosx_14_0_x86_64 ;; esac
TARGET_PIP=(install --no-compile --target "$SITE" --platform "$PLATFORM" --python-version "$PY"
            --implementation cp --abi "cp${PY/./}" --only-binary=:all:)
"${PIP[@]}" "${TARGET_PIP[@]}" --require-hashes -r "$REPO/macos/wm-requirements.txt"
rm -rf "$SITE/bin"  # console scripts for the build machine's Python; bin/wm below replaces them
"${PIP[@]}" "${TARGET_PIP[@]}" --no-deps "$WHEELS"/agentmgr-*.whl
rm -rf "$SITE/bin"

# what wm never uses: Tk, tests, the build headers, pip itself
cd "$DEST/python"
rm -rf include share lib/pkgconfig lib/tcl* lib/tk* lib/itcl* lib/thread* lib/libtcl* lib/libtk* \
    lib/python$PY/{test,idlelib,tkinter,turtledemo,ensurepip,lib2to3,pydoc_data,config-$PY-darwin} \
    lib/python$PY/turtle.py lib/python$PY/lib-dynload/_tkinter* "$SITE"/pip "$SITE"/pip-*.dist-info
find bin -mindepth 1 ! -name python3 ! -name "python$PY" -exec rm -rf {} +
find . -name __pycache__ -type d -prune -exec rm -rf {} +
cd "$REPO"
# compiled now, and never rewritten: wm runs with -B, as writing into the app would break its signature
"$TOOL" -m compileall -q -j 0 --invalidation-mode unchecked-hash "$DEST/python/lib/python$PY" >/dev/null

cat > "$DEST/bin/wm" <<'SH'
#!/bin/sh
# wm, as bundled in Workstreams.app, with its own Python: isolated (-I) from any other Python on this Mac.
self=$0
while [ -L "$self" ]; do  # a `wm` link on PATH points here
    link=$(readlink "$self")
    case $link in /*) self=$link ;; *) self=$(dirname "$self")/$link ;; esac
done
bin=$(cd "$(dirname "$self")" && pwd -P)
export AGENTMGR_WM="$bin/wm"
# Claude sessions run `wm set`/`wm add-repo` by name, and tmux gives a session the PATH of the tmux client that
# started it, so the one of this process
export PATH="$bin:$PATH"
exec "$bin/../python/bin/python3" -I -B -m agentmgr "$@"
SH
chmod 755 "$DEST/bin/wm"
echo "bundled wm ($ARCH, Python $PY_VERSION) in $DEST"
