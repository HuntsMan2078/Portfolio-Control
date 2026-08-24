#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

APP_VERSION="3.7.1"
VENV=".build-venv-mac"
RELEASE="release-macos"
mkdir -p vendor "$RELEASE"

ARCH="$(uname -m)"
case "$ARCH" in
  arm64)   PLATFORM_LABEL="Apple-Silicon" ;;
  x86_64)  PLATFORM_LABEL="Intel" ;;
  *)       PLATFORM_LABEL="$ARCH" ;;
esac

echo "============================================================"
echo "Portfolio Control v${APP_VERSION} macOS builder"
echo "Architecture: ${ARCH}"
echo "============================================================"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is required on the BUILD Mac."
  if command -v brew >/dev/null 2>&1; then
    brew install python
  else
    echo "Install Homebrew/Python, then rerun."
    exit 1
  fi
fi

python3 -c 'import sys; assert sys.version_info >= (3,10), "Python 3.10+ required"; print("Python", sys.version.split()[0])'

if [ ! -d "$VENV" ]; then
  python3 -m venv "$VENV"
fi
PYBIN="$VENV/bin/python"
"$PYBIN" -m pip install --upgrade pip
"$PYBIN" -m pip install 'pyinstaller==6.22.0' 'pywebview==6.2.1' tzdata 'cryptography>=45,<47'

# Bundle an architecture-matching Longbridge CLI using the official installer.
if [ ! -x vendor/longbridge ]; then
  if command -v longbridge >/dev/null 2>&1; then
    cp "$(command -v longbridge)" vendor/longbridge
  else
    echo "Installing official Longbridge CLI on the BUILD Mac..."
    curl -sSL https://open.longbridge.com/longbridge/longbridge-terminal/install | sh
    export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
    if command -v longbridge >/dev/null 2>&1; then
      cp "$(command -v longbridge)" vendor/longbridge
    else
      echo "Longbridge was not found after installation."
      exit 1
    fi
  fi
fi
chmod +x vendor/longbridge
./vendor/longbridge --version

# Build a native macOS .icns from the 1024px PNG when possible.
ICONSET="assets/PortfolioControl.iconset"
ICNS="assets/portfolio_control.icns"
if command -v sips >/dev/null 2>&1 && command -v iconutil >/dev/null 2>&1; then
  rm -rf "$ICONSET"
  mkdir -p "$ICONSET"
  sips -z 16 16     assets/portfolio_control.png --out "$ICONSET/icon_16x16.png" >/dev/null
  sips -z 32 32     assets/portfolio_control.png --out "$ICONSET/icon_16x16@2x.png" >/dev/null
  sips -z 32 32     assets/portfolio_control.png --out "$ICONSET/icon_32x32.png" >/dev/null
  sips -z 64 64     assets/portfolio_control.png --out "$ICONSET/icon_32x32@2x.png" >/dev/null
  sips -z 128 128   assets/portfolio_control.png --out "$ICONSET/icon_128x128.png" >/dev/null
  sips -z 256 256   assets/portfolio_control.png --out "$ICONSET/icon_128x128@2x.png" >/dev/null
  sips -z 256 256   assets/portfolio_control.png --out "$ICONSET/icon_256x256.png" >/dev/null
  sips -z 512 512   assets/portfolio_control.png --out "$ICONSET/icon_256x256@2x.png" >/dev/null
  sips -z 512 512   assets/portfolio_control.png --out "$ICONSET/icon_512x512.png" >/dev/null
  cp assets/portfolio_control.png "$ICONSET/icon_512x512@2x.png"
  iconutil -c icns "$ICONSET" -o "$ICNS"
  rm -rf "$ICONSET"
fi

rm -rf build dist
"$PYBIN" -m PyInstaller --noconfirm --clean PortfolioControl_mac.spec
SOURCE_APP="dist/Portfolio Control.app"
[ -d "$SOURCE_APP" ] || { echo "macOS .app build failed"; exit 1; }

# IMPORTANT: never sign/package the Finder-visible dist bundle directly.
# Re-copy without resource forks / FinderInfo / extended attributes first.
STAGE="$(mktemp -d /tmp/portfolio-control-release.XXXXXX)"
trap 'rm -rf "$STAGE"' EXIT
CLEAN_APP="$STAGE/Portfolio Control.app"

ditto --norsrc --noextattr "$SOURCE_APP" "$CLEAN_APP"

# Fail early if forbidden Finder metadata survived the clean copy.
if xattr -lr "$CLEAN_APP" 2>/dev/null | grep -Eq 'com\.apple\.(ResourceFork|FinderInfo)'; then
  echo "ERROR: FinderInfo/ResourceFork metadata still exists in clean app bundle."
  exit 1
fi

if [ -n "${APPLE_SIGN_IDENTITY:-}" ]; then
  echo "Signing with Developer ID identity: $APPLE_SIGN_IDENTITY"
  codesign --force --deep --options runtime --timestamp --sign "$APPLE_SIGN_IDENTITY" "$CLEAN_APP"
else
  echo "No APPLE_SIGN_IDENTITY set; applying ad-hoc signature."
  codesign --force --deep --sign - --timestamp=none "$CLEAN_APP"
fi

# Verify the exact app that will enter the DMG.
codesign --verify --deep --strict --verbose=2 "$CLEAN_APP"

ln -s /Applications "$STAGE/Applications"

DMG="$RELEASE/PortfolioControl-v${APP_VERSION}-macOS-${PLATFORM_LABEL}.dmg"
rm -f "$DMG" "$DMG.sha256.txt"
hdiutil create \
  -volname "Portfolio Control" \
  -srcfolder "$STAGE" \
  -ov \
  -format UDZO \
  "$DMG"

# Verify the disk image structure.
hdiutil verify "$DMG"

# Verify that the app signature also survived packaging into the DMG.
MOUNT_POINT="$(mktemp -d /tmp/portfolio-control-mount.XXXXXX)"
cleanup_mount() {
  if mount | grep -Fq "on $MOUNT_POINT "; then
    hdiutil detach "$MOUNT_POINT" -quiet || true
  fi
  rm -rf "$MOUNT_POINT"
}
trap 'cleanup_mount; rm -rf "$STAGE"' EXIT

hdiutil attach "$DMG" -nobrowse -readonly -mountpoint "$MOUNT_POINT" -quiet
codesign --verify --deep --strict --verbose=2 "$MOUNT_POINT/Portfolio Control.app"
hdiutil detach "$MOUNT_POINT" -quiet
rm -rf "$MOUNT_POINT"

shasum -a 256 "$DMG" | tee "$DMG.sha256.txt"

echo "============================================================"
echo "Built and verified: $DMG"
echo "SHA256 file: $DMG.sha256.txt"
echo "Architecture: $ARCH"
if [ -z "${APPLE_SIGN_IDENTITY:-}" ]; then
  echo "Signing: ad-hoc (GitHub distribution may require users to approve the app in macOS Security settings)."
else
  echo "Signing: $APPLE_SIGN_IDENTITY"
fi
echo "============================================================"
