#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
DESKTOP_ROOT="$PROJECT_ROOT/desktop"
BINARIES_ROOT="$DESKTOP_ROOT/src-tauri/binaries"
DIST_BIN="$PROJECT_ROOT/dist/receiver_sidecar"

export PATH="$HOME/.cargo/bin:$PATH"

NATIVE_ARCH="$(uname -m)"
NATIVE_HOST_TRIPLE="$(rustc --print host-tuple 2>/dev/null || rustc -Vv | awk '/host:/ {print $2}')"
TARGET_TRIPLE="${MACOS_TARGET_TRIPLE:-$NATIVE_HOST_TRIPLE}"

echo "[build] native_arch=$NATIVE_ARCH native_host=$NATIVE_HOST_TRIPLE target=$TARGET_TRIPLE"

if [[ "$TARGET_TRIPLE" == "x86_64-apple-darwin" && "$NATIVE_ARCH" == "arm64" ]]; then
  VENV_DIR="$PROJECT_ROOT/build/venv-x86_64"
  if [[ ! -d "$VENV_DIR" ]]; then
    echo "[build] Creating x86_64 Python venv under Rosetta 2..."
    mkdir -p "$PROJECT_ROOT/build"
    arch -x86_64 /usr/bin/python3 -m venv "$VENV_DIR"
  fi
  export PATH="$VENV_DIR/bin:$PATH"
  export PYTHON_BIN="$VENV_DIR/bin/python3"
  echo "[build] Python: $($PYTHON_BIN --version) arch=$($PYTHON_BIN -c "import platform; print(platform.machine())")"
  bash "$SCRIPT_DIR/build_sidecar_macos.sh"
else
  bash "$SCRIPT_DIR/build_sidecar_macos.sh"
fi

mkdir -p "$BINARIES_ROOT"
cp "$DIST_BIN" "$BINARIES_ROOT/receiver_sidecar-$TARGET_TRIPLE"
chmod +x "$BINARIES_ROOT/receiver_sidecar-$TARGET_TRIPLE"

echo "[build] Sidecar: $(file "$BINARIES_ROOT/receiver_sidecar-$TARGET_TRIPLE" | head -1)"

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
