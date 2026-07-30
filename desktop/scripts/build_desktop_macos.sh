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

# For x86_64 build on Apple Silicon: use Rosetta for Python sidecar, cross-compile Rust
if [[ "$TARGET_TRIPLE" == "x86_64-apple-darwin" && "$NATIVE_ARCH" == "arm64" ]]; then
  # Find an x86_64 Python (use universal2 system Python under Rosetta)
  # Download x86_64 Python standalone build
  PYTHON_VERSION="3.11.9"
  PYTHON_ARCHIVE="cpython-$PYTHON_VERSION+20240415-x86_64-apple-darwin-install_only.tar.gz"
  PYTHON_URL="https://github.com/indygreg/python-build-standalone/releases/download/20240415/$PYTHON_ARCHIVE"
  PYTHON_DIR="$PROJECT_ROOT/build/python-x86_64"
  mkdir -p "$PYTHON_DIR"
  if [[ ! -x "$PYTHON_DIR/python/bin/python3" ]]; then
    echo "[build] Downloading x86_64 Python..."
    curl -sL "$PYTHON_URL" | tar -xz -C "$PYTHON_DIR" --strip-components=1
  fi
  export PYTHON_BIN="$PYTHON_DIR/python/bin/python3"
  echo "[build] Using x86_64 Python: $($PYTHON_BIN --version) ($($PYTHON_BIN -c "import platform; print(platform.machine())"))"
fi

bash "$SCRIPT_DIR/build_sidecar_macos.sh"

mkdir -p "$BINARIES_ROOT"
cp "$DIST_BIN" "$BINARIES_ROOT/receiver_sidecar-$TARGET_TRIPLE"
chmod +x "$BINARIES_ROOT/receiver_sidecar-$TARGET_TRIPLE"

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
