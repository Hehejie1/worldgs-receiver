#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
BUILD_ROOT="$PROJECT_ROOT/build"
BROWSERS_ROOT="$BUILD_ROOT/ms-playwright"
PYINSTALLER_CACHE_ROOT="$BUILD_ROOT/pyinstaller-cache"
PYTHON_BIN="${PYTHON_BIN:-}"

is_python_compatible() {
  local candidate="$1"
  local -a candidate_cmd
  read -r -a candidate_cmd <<< "$candidate"
  [[ "${#candidate_cmd[@]}" -gt 0 ]] || return 1
  "${candidate_cmd[@]}" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1
}

resolve_python_bin() {
  local -a candidates=()
  local is_arm64_hardware
  is_arm64_hardware="$(sysctl -in hw.optional.arm64 2>/dev/null || echo 0)"

  if [[ -n "$PYTHON_BIN" ]]; then
    if is_python_compatible "$PYTHON_BIN"; then
      printf '%s\n' "$PYTHON_BIN"
      return 0
    fi
    echo "PYTHON_BIN must point to Python 3.10+ for macOS desktop packaging: $PYTHON_BIN" >&2
    return 1
  fi

  if [[ "$is_arm64_hardware" == "1" ]]; then
    candidates+=("arch -arm64 python3.11" "arch -arm64 python3.10" "arch -arm64 python3")
  fi
  candidates+=("python3.11" "python3.10" "python3")

  local candidate
  for candidate in "${candidates[@]}"; do
    if is_python_compatible "$candidate"; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done

  echo "Failed to find Python 3.10+ for macOS desktop packaging. Set PYTHON_BIN explicitly." >&2
  return 1
}

mkdir -p "$BROWSERS_ROOT"
mkdir -p "$PYINSTALLER_CACHE_ROOT"

cd "$PROJECT_ROOT"

PYTHON_BIN="$(resolve_python_bin)"
read -r -a PYTHON_CMD <<< "$PYTHON_BIN"

"${PYTHON_CMD[@]}" -m pip install --user --upgrade pip setuptools wheel
"${PYTHON_CMD[@]}" -m pip install -e '.[desktop]'
if [[ "$(sysctl -in hw.optional.arm64 2>/dev/null || echo 0)" == "1" && "$PYTHON_BIN" == *"arm64"* ]]; then
  PYDANTIC_CORE_VERSION="$("${PYTHON_CMD[@]}" -c 'from importlib.metadata import version; print(version("pydantic-core"))')"
  RUSTUP_TOOLCHAIN="${RUSTUP_TOOLCHAIN:-stable-aarch64-apple-darwin}" "${PYTHON_CMD[@]}" -m pip install --user --force-reinstall --no-binary pydantic-core "pydantic-core==$PYDANTIC_CORE_VERSION"
fi
PLAYWRIGHT_DRY_RUN_OUTPUT="$(PLAYWRIGHT_BROWSERS_PATH="$BROWSERS_ROOT" "${PYTHON_CMD[@]}" -m playwright install --dry-run firefox)"
EXPECTED_FIREFOX_DIR="$(printf '%s\n' "$PLAYWRIGHT_DRY_RUN_OUTPUT" | awk -F': *' '/Install location:/ {print $2; exit}')"
if [[ -n "${EXPECTED_FIREFOX_DIR:-}" && ! -d "$EXPECTED_FIREFOX_DIR" ]]; then
  EXISTING_FIREFOX_DIR="$(find "$BROWSERS_ROOT" -maxdepth 1 -type d -name 'firefox-*' | sort | tail -1 || true)"
  if [[ -n "${EXISTING_FIREFOX_DIR:-}" ]]; then
    ln -sfn "$(basename "$EXISTING_FIREFOX_DIR")" "$EXPECTED_FIREFOX_DIR"
  fi
fi
PLAYWRIGHT_BROWSERS_PATH="$BROWSERS_ROOT" "${PYTHON_CMD[@]}" -m playwright install firefox
PYINSTALLER_CONFIG_DIR="$PYINSTALLER_CACHE_ROOT" "${PYTHON_CMD[@]}" -m PyInstaller desktop/sidecar/receiver_sidecar.spec --noconfirm --clean
