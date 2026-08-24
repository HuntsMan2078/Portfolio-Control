#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
APP_VERSION="3.7.0"
VENV=".build-venv-mac"
RELEASE="release-macos"
mkdir -p vendor "$RELEASE"

echo "============================================================"
echo "Portfolio Control v${APP_VERSION} macOS builder"
echo "Architecture: $(uname -m)"
echo "============================================================"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is required on the BUILD Mac."
  if command -v brew >/dev/null 2>&1; then brew install python; else echo "Install Homebrew/Python, then rerun."; exit 1; fi
fi
python3 -c 'import sys; assert sys.version_info >= (3,10), "Python 3.10+ required"; print("Python",sys.version.split()[0])'

if [ ! -d "$VENV" ]; then python3 -m venv "$VENV"; fi
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
    if command -v longbridge >/dev/null 2>&1; then cp "$(command -v longbridge)" vendor/longbridge; else echo "Longbridge was not found after installation."; exit 1; fi
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
APP="dist/Portfolio Control.app"
[ -d "$APP" ] || { echo "macOS .app build failed"; exit 1; }

if [ -n "${APPLE_SIGN_IDENTITY:-}" ]; then
  echo "Signing with: $APPLE_SIGN_IDENTITY"
  codesign --force --deep --options runtime --timestamp --sign "$APPLE_SIGN_IDENTITY" "$APP"
fi

DMG="$RELEASE/PortfolioControl_v${APP_VERSION}_$(uname -m).dmg"
rm -f "$DMG"
STAGE="$(mktemp -d)"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
hdiutil create -volname "Portfolio Control" -srcfolder "$STAGE" -ov -format UDZO "$DMG"
rm -rf "$STAGE"
shasum -a 256 "$DMG" | tee "$DMG.sha256.txt"
echo "Built: $DMG"
echo "Architecture: $(uname -m). Intel and Apple Silicon DMGs should be built on matching Macs."
