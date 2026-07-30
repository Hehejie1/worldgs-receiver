#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR=".venv"
VENV_PYTHON="$VENV_DIR/bin/python"
INSTALL_MARKER="$VENV_DIR/.worldgs_receiver_installed"
NEEDS_INSTALL=0

receiver_port() {
  local port="8787"
  local expect_value=0
  for arg in "$@"; do
    if [ "$expect_value" -eq 1 ]; then
      port="$arg"
      expect_value=0
      continue
    fi
    case "$arg" in
      --port)
        expect_value=1
        ;;
      --port=*)
        port="${arg#--port=}"
        ;;
    esac
  done
  printf '%s\n' "$port"
}

ensure_port_available() {
  local port="$1"
  local -a pids=()
  local deadline=0

  if ! command -v lsof >/dev/null 2>&1; then
    return
  fi

  mapfile -t pids < <(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)
  if [ "${#pids[@]}" -eq 0 ]; then
    return
  fi

  echo "[WorldGS Receiver] Port $port is already in use. Stopping existing listener..."
  kill -TERM "${pids[@]}" 2>/dev/null || true

  deadline=20
  while [ "$deadline" -gt 0 ]; do
    mapfile -t pids < <(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)
    if [ "${#pids[@]}" -eq 0 ]; then
      return
    fi
    sleep 0.2
    deadline=$((deadline - 1))
  done

  echo "[WorldGS Receiver] Force killing remaining listener on port $port..."
  kill -KILL "${pids[@]}" 2>/dev/null || true
}

deps_ready() {
  "$1" -c "import fastapi, uvicorn, multipart, qrcode, yaml, playwright" >/dev/null 2>&1
}

firefox_ready() {
  "$1" - <<'PY' >/dev/null 2>&1
from pathlib import Path
from playwright.sync_api import sync_playwright

playwright = sync_playwright().start()
try:
    path = Path(playwright.firefox.executable_path)
    raise SystemExit(0 if path.exists() else 1)
finally:
    playwright.stop()
PY
}

ensure_firefox() {
  if firefox_ready "$1"; then
    return
  fi
  echo "[WorldGS Receiver] Installing Playwright Firefox browser..."
  "$1" -m playwright install firefox
}

if deps_ready "$PYTHON_BIN"; then
  ensure_firefox "$PYTHON_BIN"
  ensure_port_available "$(receiver_port "$@")"
  echo "[WorldGS Receiver] Using current Python environment."
  exec "$PYTHON_BIN" -m worldgs_receiver.cli "$@"
fi

if [ ! -x "$VENV_PYTHON" ]; then
  echo "[WorldGS Receiver] Creating Python virtual environment..."
  "$PYTHON_BIN" -m venv "$VENV_DIR"
  NEEDS_INSTALL=1
fi

if [ ! -f "$INSTALL_MARKER" ] || [ "pyproject.toml" -nt "$INSTALL_MARKER" ] || [ "requirements.txt" -nt "$INSTALL_MARKER" ]; then
  NEEDS_INSTALL=1
fi

if [ -x "$VENV_PYTHON" ] && ! deps_ready "$VENV_PYTHON"; then
  NEEDS_INSTALL=1
fi

if [ "$NEEDS_INSTALL" -eq 1 ]; then
  echo "[WorldGS Receiver] Installing receiver dependencies..."
  "$VENV_PYTHON" -m pip install -r requirements.txt
  touch "$INSTALL_MARKER"
else
  echo "[WorldGS Receiver] Dependencies are ready."
fi

ensure_firefox "$VENV_PYTHON"
ensure_port_available "$(receiver_port "$@")"

echo "[WorldGS Receiver] Starting local receiver..."
exec "$VENV_PYTHON" -m worldgs_receiver.cli "$@"