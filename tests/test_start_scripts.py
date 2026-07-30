from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_unix_start_script_checks_all_runtime_dependencies() -> None:
    script = (ROOT / "start_receiver.sh").read_text(encoding="utf-8")

    assert "import fastapi, uvicorn, multipart, qrcode, yaml, playwright" in script
    assert "playwright install firefox" in script
    assert "requirements.txt" in script
    assert '"requirements.txt" -nt "$INSTALL_MARKER"' in script
    assert "lsof -tiTCP:\"$port\" -sTCP:LISTEN" in script
    assert 'kill -TERM "${pids[@]}"' in script
    assert 'kill -KILL "${pids[@]}"' in script


def test_windows_start_script_checks_all_runtime_dependencies() -> None:
    script = (ROOT / "start_receiver.bat").read_text(encoding="utf-8")

    assert "import fastapi, uvicorn, multipart, qrcode, yaml, playwright" in script
    assert "playwright install firefox" in script
    assert "requirements.txt" in script
    assert "marker.stat().st_mtime" in script


def test_macos_sidecar_build_script_installs_desktop_deps_firefox_and_pyinstaller() -> None:
    script = (ROOT / "desktop" / "scripts" / "build_sidecar_macos.sh").read_text(encoding="utf-8")

    assert "pip install -e '.[desktop]'" in script
    assert "PLAYWRIGHT_BROWSERS_PATH" in script
    assert "playwright install firefox" in script
    assert "PyInstaller" in script
    assert "PLAYWRIGHT_BROWSERS_SOURCE" not in script


def test_windows_sidecar_build_script_installs_desktop_deps_firefox_and_pyinstaller() -> None:
    script = (ROOT / "desktop" / "scripts" / "build_sidecar_windows.ps1").read_text(encoding="utf-8")

    assert "pip install -e '.[desktop]'" in script or 'pip install -e ".[desktop]"' in script
    assert "$env:PLAYWRIGHT_BROWSERS_PATH" in script
    assert "playwright install firefox" in script
    assert "PyInstaller" in script
    assert "$env:PLAYWRIGHT_BROWSERS_SOURCE" not in script
