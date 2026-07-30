#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
BUILD_ROOT="$PROJECT_ROOT/build"
BROWSERS_ROOT="$BUILD_ROOT/ms-playwright"
PYTHON_BIN="${PYTHON_BIN:-python3}"
PIP_INDEX_URL="${PIP_INDEX_URL:-}"

mkdir -p "$BROWSERS_ROOT"

cd "$PROJECT_ROOT"

read -r -a PYTHON_CMD <<< "$PYTHON_BIN"

PIP_ARGS=()
if [[ -n "$PIP_INDEX_URL" ]]; then
  PIP_ARGS+=(--index-url "$PIP_INDEX_URL")
fi

"${PYTHON_CMD[@]}" -m pip install "${PIP_ARGS[@]}" --upgrade pip setuptools wheel
"${PYTHON_CMD[@]}" -m pip install "${PIP_ARGS[@]}" -e '.[desktop]'
if ! find "$BROWSERS_ROOT" -maxdepth 1 -type d -name 'firefox-*' | grep -q .; then
  PLAYWRIGHT_BROWSERS_PATH="$BROWSERS_ROOT" "${PYTHON_CMD[@]}" -m playwright install firefox
fi
"${PYTHON_CMD[@]}" -m PyInstaller desktop/sidecar/receiver_sidecar.spec --noconfirm --clean
