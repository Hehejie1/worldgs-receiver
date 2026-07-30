#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
DESKTOP_ROOT="$PROJECT_ROOT/desktop"
BINARIES_ROOT="$DESKTOP_ROOT/src-tauri/binaries"
DIST_BIN="$PROJECT_ROOT/dist/receiver_sidecar"
TAURI_CONFIG_PATH="$DESKTOP_ROOT/src-tauri/tauri.conf.json"

export PATH="$HOME/.cargo/bin:$PATH"

if [[ -n "${MACOS_TARGET_TRIPLE:-}" ]]; then
  HOST_TRIPLE="$MACOS_TARGET_TRIPLE"
else
  HOST_TRIPLE="$(rustc --print host-tuple 2>/dev/null || rustc -Vv | awk '/host:/ {print $2}')"
fi

if [[ -z "${HOST_TRIPLE:-}" ]]; then
  echo "Failed to determine rust target triple" >&2
  exit 1
fi

PRODUCT_NAME="$(sed -n 's/.*"productName": "\(.*\)".*/\1/p' "$TAURI_CONFIG_PATH" | head -n 1)"
APP_VERSION="$(sed -n 's/.*"version": "\(.*\)".*/\1/p' "$TAURI_CONFIG_PATH" | head -n 1)"

if [[ -z "${PRODUCT_NAME:-}" || -z "${APP_VERSION:-}" ]]; then
  echo "Failed to read product metadata from $TAURI_CONFIG_PATH" >&2
  exit 1
fi

case "$HOST_TRIPLE" in
  x86_64-apple-darwin)
    DMG_ARCH_SUFFIX="x64"
    ;;
  aarch64-apple-darwin)
    DMG_ARCH_SUFFIX="aarch64"
    ;;
  *)
    DMG_ARCH_SUFFIX="${HOST_TRIPLE%%-*}"
    ;;
esac

"$SCRIPT_DIR/build_sidecar_macos.sh"

mkdir -p "$BINARIES_ROOT"
TARGET_BIN="$PROJECT_ROOT/desktop/src-tauri/binaries/receiver_sidecar-$HOST_TRIPLE"
cp "$DIST_BIN" "$TARGET_BIN"
chmod +x "$TARGET_BIN"

cd "$DESKTOP_ROOT"
npm install
npx tauri build --target "$HOST_TRIPLE" --bundles app

APP_BUNDLE_PATH="$DESKTOP_ROOT/src-tauri/target/$HOST_TRIPLE/release/bundle/macos/$PRODUCT_NAME.app"
DMG_OUTPUT_DIR="$DESKTOP_ROOT/src-tauri/target/$HOST_TRIPLE/release/bundle/dmg"
DMG_OUTPUT_PATH="$DMG_OUTPUT_DIR/${PRODUCT_NAME}_${APP_VERSION}_${DMG_ARCH_SUFFIX}.dmg"
DMG_STAGE_DIR="$(mktemp -d "$DESKTOP_ROOT/src-tauri/target/$HOST_TRIPLE/release/bundle/dmg-staging.XXXXXX")"

cleanup() {
  rm -rf "$DMG_STAGE_DIR"
}
trap cleanup EXIT

if [[ ! -d "$APP_BUNDLE_PATH" ]]; then
  echo "Missing app bundle after tauri build: $APP_BUNDLE_PATH" >&2
  exit 1
fi

mkdir -p "$DMG_OUTPUT_DIR"
ditto "$APP_BUNDLE_PATH" "$DMG_STAGE_DIR/$PRODUCT_NAME.app"
ln -s /Applications "$DMG_STAGE_DIR/Applications"
rm -f "$DMG_OUTPUT_PATH"
hdiutil create -volname "$PRODUCT_NAME" -srcfolder "$DMG_STAGE_DIR" -ov -format UDZO "$DMG_OUTPUT_PATH"
echo "DMG created at $DMG_OUTPUT_PATH"
