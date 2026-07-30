#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
BUILD_ROOT="$PROJECT_ROOT/build"
BROWSERS_ROOT="$BUILD_ROOT/ms-playwright"
PYINSTALLER_CACHE_ROOT="$BUILD_ROOT/pyinstaller-cache"
PYTHON_BIN="${PYTHON_BIN:-python3}"

mkdir -p "$BROWSERS_ROOT"
mkdir -p "$PYINSTALLER_CACHE_ROOT"

cd "$PROJECT_ROOT"

read -r -a PYTHON_CMD <<< "$PYTHON_BIN"

"${PYTHON_CMD[@]}" -m pip install --upgrade pip setuptools wheel
"${PYTHON_CMD[@]}" -m pip install -e '.[desktop]'
PLAYWRIGHT_BROWSERS_PATH="$BROWSERS_ROOT" "${PYTHON_CMD[@]}" -m playwright install firefox
PYINSTALLER_CONFIG_DIR="$PYINSTALLER_CACHE_ROOT" "${PYTHON_CMD[@]}" -m PyInstaller desktop/sidecar/receiver_sidecar.spec --noconfirm --clean
