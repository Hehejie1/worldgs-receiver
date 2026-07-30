#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
DESKTOP_ROOT="$PROJECT_ROOT/desktop"
DIST_BIN="$PROJECT_ROOT/dist/receiver_sidecar"
BROWSERS_ROOT="$PROJECT_ROOT/build/ms-playwright"
PACKAGE_NAME="${PACKAGE_NAME:-worldgs-receiver}"
PACKAGE_VERSION="${PACKAGE_VERSION:-0.1.0}"
ARCH="${DEB_ARCH:-$(dpkg --print-architecture)}"
PACKAGE_ROOT="$PROJECT_ROOT/build/deb/$PACKAGE_NAME"
OUTPUT_DIR="$PROJECT_ROOT/dist"
INCLUDE_PLAYWRIGHT_BROWSERS="${INCLUDE_PLAYWRIGHT_BROWSERS:-0}"

if [[ ! -x "$DIST_BIN" ]]; then
  echo "Missing sidecar binary: $DIST_BIN" >&2
  echo "Run desktop/scripts/build_sidecar_linux.sh first." >&2
  exit 1
fi

rm -rf "$PACKAGE_ROOT"
mkdir -p \
  "$PACKAGE_ROOT/DEBIAN" \
  "$PACKAGE_ROOT/opt/worldgs-receiver/bin" \
  "$PACKAGE_ROOT/opt/worldgs-receiver/playwright-browsers" \
  "$PACKAGE_ROOT/usr/bin" \
  "$PACKAGE_ROOT/usr/share/applications" \
  "$PACKAGE_ROOT/usr/share/icons/hicolor/128x128/apps"

cp "$DIST_BIN" "$PACKAGE_ROOT/opt/worldgs-receiver/bin/receiver_sidecar"
chmod 0755 "$PACKAGE_ROOT/opt/worldgs-receiver/bin/receiver_sidecar"

if [[ "$INCLUDE_PLAYWRIGHT_BROWSERS" == "1" && -d "$BROWSERS_ROOT" ]]; then
  cp -a "$BROWSERS_ROOT"/. "$PACKAGE_ROOT/opt/worldgs-receiver/playwright-browsers/"
fi

cp "$DESKTOP_ROOT/src-tauri/icons/128x128.png" \
  "$PACKAGE_ROOT/usr/share/icons/hicolor/128x128/apps/worldgs-receiver.png"

cat >"$PACKAGE_ROOT/usr/bin/worldgs-receiver" <<'SH'
#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="/opt/worldgs-receiver"
BROWSERS_ROOT="$APP_ROOT/playwright-browsers"
PORT="${WORLDGS_RECEIVER_PORT:-8787}"
OUTPUT_DIR="${WORLDGS_RECEIVER_OUTPUT:-$HOME/WorldGS_Imports}"
LOG_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/worldgs-receiver"
LOG_FILE="$LOG_DIR/receiver.log"
URL="http://127.0.0.1:$PORT/"

mkdir -p "$OUTPUT_DIR" "$LOG_DIR"

RECEIVER_ENV=()
if find "$BROWSERS_ROOT" -maxdepth 1 -type d -name 'firefox-*' | grep -q .; then
  RECEIVER_ENV+=(PLAYWRIGHT_BROWSERS_PATH="$BROWSERS_ROOT")
fi

if command -v lsof >/dev/null 2>&1; then
  if ! lsof -tiTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    env "${RECEIVER_ENV[@]}" \
      "$APP_ROOT/bin/receiver_sidecar" \
        --host 0.0.0.0 \
        --port "$PORT" \
        --output "$OUTPUT_DIR" >>"$LOG_FILE" 2>&1 &
  fi
else
  env "${RECEIVER_ENV[@]}" \
    "$APP_ROOT/bin/receiver_sidecar" \
      --host 0.0.0.0 \
      --port "$PORT" \
      --output "$OUTPUT_DIR" >>"$LOG_FILE" 2>&1 &
fi

for _ in $(seq 1 60); do
  if python3 - "$URL" <<'PY' >/dev/null 2>&1
import sys
from urllib.request import urlopen
urlopen(sys.argv[1] + "api/healthz", timeout=1)
PY
  then
    break
  fi
  sleep 0.5
done

if command -v xdg-open >/dev/null 2>&1; then
  xdg-open "$URL" >/dev/null 2>&1 &
else
  echo "$URL"
fi
SH
chmod 0755 "$PACKAGE_ROOT/usr/bin/worldgs-receiver"

cat >"$PACKAGE_ROOT/usr/share/applications/worldgs-receiver.desktop" <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=WorldGS Receiver
Comment=Receive WorldGS capture packages from mobile devices
Exec=worldgs-receiver
Icon=worldgs-receiver
Terminal=false
Categories=Graphics;Utility;
StartupNotify=true
DESKTOP

cat >"$PACKAGE_ROOT/DEBIAN/control" <<EOF
Package: $PACKAGE_NAME
Version: $PACKAGE_VERSION
Section: graphics
Priority: optional
Architecture: $ARCH
Maintainer: WorldGS <dev@worldgs.local>
Depends: libc6, libstdc++6, python3, xdg-utils, lsof
Description: WorldGS local receiver for Linux arm64
 Local desktop launcher and receiver sidecar for importing WorldGS capture packages.
EOF

mkdir -p "$OUTPUT_DIR"
dpkg-deb --build "$PACKAGE_ROOT" "$OUTPUT_DIR/${PACKAGE_NAME}_${PACKAGE_VERSION}_${ARCH}.deb"
