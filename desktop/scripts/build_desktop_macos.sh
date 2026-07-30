#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
DESKTOP_ROOT="$PROJECT_ROOT/desktop"
BINARIES_ROOT="$DESKTOP_ROOT/src-tauri/binaries"
DIST_BIN="$PROJECT_ROOT/dist/receiver_sidecar"

export PATH="$HOME/.cargo/bin:$PATH"

NATIVE_HOST_TRIPLE="$(rustc --print host-tuple 2>/dev/null || rustc -Vv | awk '/host:/ {print $2}')"
TARGET_TRIPLE="${MACOS_TARGET_TRIPLE:-$NATIVE_HOST_TRIPLE}"

echo "[build] native_host=$NATIVE_HOST_TRIPLE target=$TARGET_TRIPLE"

# Build Python sidecar for native architecture
bash "$SCRIPT_DIR/build_sidecar_macos.sh"

# Copy sidecar to Tauri binaries directory
mkdir -p "$BINARIES_ROOT"
cp "$DIST_BIN" "$BINARIES_ROOT/receiver_sidecar-$TARGET_TRIPLE"
chmod +x "$BINARIES_ROOT/receiver_sidecar-$TARGET_TRIPLE"

echo "[build] Sidecar: $(file "$BINARIES_ROOT/receiver_sidecar-$TARGET_TRIPLE" | head -1)"

# Build Tauri app
cd "$DESKTOP_ROOT"
npm install

if [[ "$TARGET_TRIPLE" == "$NATIVE_HOST_TRIPLE" ]]; then
  npx tauri build --bundles dmg,app
  TARGET_DIR="target/release"
else
  npx tauri build --target "$TARGET_TRIPLE" --bundles dmg,app
  TARGET_DIR="target/$TARGET_TRIPLE/release"
fi

echo "[build] Done."
ls -la "$DESKTOP_ROOT/src-tauri/$TARGET_DIR/bundle/dmg/" 2>&1 || true
ls -la "$DESKTOP_ROOT/src-tauri/$TARGET_DIR/bundle/macos/" 2>&1 || true
