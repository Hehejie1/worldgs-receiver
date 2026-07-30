#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
DESKTOP_ROOT="$PROJECT_ROOT/desktop"
BINARIES_ROOT="$DESKTOP_ROOT/src-tauri/binaries"
DIST_BIN="$PROJECT_ROOT/dist/receiver_sidecar"
TAURI_CONFIG_PATH="$DESKTOP_ROOT/src-tauri/tauri.conf.json"

export PATH="$HOME/.cargo/bin:$PATH"

echo "[build_desktop_macos] Starting build at $(date)"
echo "[build_desktop_macos] uname -a: $(uname -a)"
echo "[build_desktop_macos] rustc --version: $(rustc --version 2>&1 || echo 'no rustc')"
echo "[build_desktop_macos] python3 --version: $(python3 --version 2>&1)"
echo "[build_desktop_macos] node --version: $(node --version 2>&1)"
echo "[build_desktop_macos] npm --version: $(npm --version 2>&1)"

NATIVE_HOST_TRIPLE="$(rustc --print host-tuple 2>/dev/null || rustc -Vv | awk '/host:/ {print $2}')"
echo "[build_desktop_macos] NATIVE_HOST_TRIPLE=$NATIVE_HOST_TRIPLE"

if [[ -n "${MACOS_TARGET_TRIPLE:-}" ]]; then
  TARGET_TRIPLE="$MACOS_TARGET_TRIPLE"
else
  TARGET_TRIPLE="$NATIVE_HOST_TRIPLE"
fi
echo "[build_desktop_macos] TARGET_TRIPLE=$TARGET_TRIPLE"

if [[ "$TARGET_TRIPLE" == "$NATIVE_HOST_TRIPLE" ]]; then
  IS_CROSS_COMPILE=0
  TARGET_DIR="target/release"
  TAURI_TARGET_FLAG=""
else
  IS_CROSS_COMPILE=1
  TARGET_DIR="target/$TARGET_TRIPLE/release"
  TAURI_TARGET_FLAG="--target $TARGET_TRIPLE"
fi
echo "[build_desktop_macos] IS_CROSS_COMPILE=$IS_CROSS_COMPILE, TARGET_DIR=$TARGET_DIR"

PRODUCT_NAME="$(sed -n 's/.*"productName": "\(.*\)".*/\1/p' "$TAURI_CONFIG_PATH" | head -n 1)"
APP_VERSION="$(sed -n 's/.*"version": "\(.*\)".*/\1/p' "$TAURI_CONFIG_PATH" | head -n 1)"
echo "[build_desktop_macos] PRODUCT_NAME=$PRODUCT_NAME, APP_VERSION=$APP_VERSION"

case "$TARGET_TRIPLE" in
  x86_64-apple-darwin)
    DMG_ARCH_SUFFIX="x64"
    ;;
  aarch64-apple-darwin)
    DMG_ARCH_SUFFIX="aarch64"
    ;;
  *)
    DMG_ARCH_SUFFIX="${TARGET_TRIPLE%%-*}"
    ;;
esac

echo "[build_desktop_macos] Building sidecar..."
bash "$SCRIPT_DIR/build_sidecar_macos.sh"
echo "[build_desktop_macos] Sidecar build completed at $(date)"

mkdir -p "$BINARIES_ROOT"
TARGET_BIN="$BINARIES_ROOT/receiver_sidecar-$TARGET_TRIPLE"
cp "$DIST_BIN" "$TARGET_BIN"
chmod +x "$TARGET_BIN"
echo "[build_desktop_macos] Sidecar binary placed at $TARGET_BIN"
echo "[build_desktop_macos] Sidecar file type: $(file "$TARGET_BIN" 2>&1)"

cd "$DESKTOP_ROOT"
echo "[build_desktop_macos] Running npm install..."
npm install
echo "[build_desktop_macos] npm install completed at $(date)"

echo "[build_desktop_macos] Running tauri build..."
if [[ -n "$TAURI_TARGET_FLAG" ]]; then
  npx tauri build $TAURI_TARGET_FLAG --bundles app
else
  npx tauri build --bundles app
fi
echo "[build_desktop_macos] Tauri build completed at $(date)"

APP_BUNDLE_PATH="$DESKTOP_ROOT/src-tauri/$TARGET_DIR/bundle/macos/$PRODUCT_NAME.app"
DMG_OUTPUT_DIR="$DESKTOP_ROOT/src-tauri/$TARGET_DIR/bundle/dmg"
DMG_OUTPUT_PATH="$DMG_OUTPUT_DIR/${PRODUCT_NAME}_${APP_VERSION}_${DMG_ARCH_SUFFIX}.dmg"
echo "[build_desktop_macos] APP_BUNDLE_PATH=$APP_BUNDLE_PATH"

if [[ ! -d "$APP_BUNDLE_PATH" ]]; then
  echo "Missing app bundle after tauri build: $APP_BUNDLE_PATH" >&2
  echo "Contents of $DESKTOP_ROOT/src-tauri/$TARGET_DIR/bundle/:" >&2
  ls -la "$DESKTOP_ROOT/src-tauri/$TARGET_DIR/bundle/" 2>&1 || true
  exit 1
fi

echo "[build_desktop_macos] Creating DMG..."
mkdir -p "$DMG_OUTPUT_DIR"

STAGING_DIR="$(mktemp -d)"
echo "[build_desktop_macos] Staging DMG at $STAGING_DIR"
cp -R "$APP_BUNDLE_PATH" "$STAGING_DIR/"
ln -s /Applications "$STAGING_DIR/Applications"
rm -f "$DMG_OUTPUT_PATH"

echo "[build_desktop_macos] Running hdiutil create..."
hdiutil create -volname "$PRODUCT_NAME" -srcfolder "$STAGING_DIR" -ov -format UDZO -noscrub -noatomic "$DMG_OUTPUT_PATH"
rm -rf "$STAGING_DIR"
echo "DMG created at $DMG_OUTPUT_PATH"
echo "[build_desktop_macos] Build completed at $(date)"
