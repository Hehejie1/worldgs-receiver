#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
DESKTOP_ROOT="$PROJECT_ROOT/desktop"
BINARIES_ROOT="$DESKTOP_ROOT/src-tauri/binaries"
DIST_BIN="$PROJECT_ROOT/dist/receiver_sidecar"

HOST_TRIPLE="${LINUX_TARGET_TRIPLE:-$(rustc --print host-tuple 2>/dev/null || rustc -Vv | awk '/host:/ {print $2}')}"
if [[ -z "${HOST_TRIPLE:-}" ]]; then
  echo "Failed to determine rust target triple" >&2
  exit 1
fi

"$SCRIPT_DIR/build_sidecar_linux.sh"

mkdir -p "$BINARIES_ROOT"
TARGET_BIN="$BINARIES_ROOT/receiver_sidecar-$HOST_TRIPLE"
cp "$DIST_BIN" "$TARGET_BIN"
chmod +x "$TARGET_BIN"

cd "$DESKTOP_ROOT"
npm install
npx tauri build --target "$HOST_TRIPLE" --bundles deb
