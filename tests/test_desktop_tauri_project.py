import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_tauri_package_manifest_declares_dev_and_build_scripts() -> None:
    package_json = ROOT / "desktop" / "package.json"

    assert package_json.is_file()
    payload = json.loads(package_json.read_text(encoding="utf-8"))
    assert payload["name"] == "worldgs-receiver-desktop"
    assert payload["private"] is True
    assert payload["scripts"]["tauri:dev"] == "tauri dev"
    assert payload["scripts"]["tauri:build"] == "tauri build"
    assert "@tauri-apps/cli" in payload["devDependencies"]


def test_tauri_config_bundles_receiver_sidecar_and_creates_main_window() -> None:
    tauri_config = ROOT / "desktop" / "src-tauri" / "tauri.conf.json"

    assert tauri_config.is_file()
    payload = json.loads(tauri_config.read_text(encoding="utf-8"))
    main_window = payload["app"]["windows"][0]
    assert payload["productName"] == "WorldGS"
    assert payload["identifier"] == "cn.worldgs.desktop"
    assert main_window["title"] == "WorldGS"
    assert main_window["width"] >= 1200
    assert main_window["minWidth"] >= 960
    assert "receiver_sidecar" in payload["bundle"]["externalBin"][0]
    assert "icons/icon.icns" in payload["bundle"]["icon"]
    assert "icons/icon.ico" in payload["bundle"]["icon"]
    assert payload["bundle"]["resources"]["../../build/ms-playwright"] == "playwright-browsers"


def test_tauri_icon_assets_include_macos_icns_and_scaled_pngs() -> None:
    icons_dir = ROOT / "desktop" / "src-tauri" / "icons"

    for name in ["32x32.png", "128x128.png", "128x128@2x.png", "icon.png", "icon.icns", "icon.ico"]:
        assert (icons_dir / name).is_file()


def test_receiver_sidecar_rust_module_serves_lan_uploads_and_uses_loopback_url() -> None:
    source = (ROOT / "desktop" / "src-tauri" / "src" / "receiver_sidecar.rs").read_text(encoding="utf-8")

    assert "/api/healthz" in source
    assert "127.0.0.1" in source
    assert '"0.0.0.0"' in source
    assert "--host" in source
    assert "--port" in source
    assert "--output" in source
    assert "portpicker" in source


def test_tauri_main_wires_sidecar_lifecycle_hooks() -> None:
    source = (ROOT / "desktop" / "src-tauri" / "src" / "main.rs").read_text(encoding="utf-8")

    assert "start_receiver_sidecar" in source
    assert "cleanup_receiver_sidecar" in source
    assert "RunEvent::Exit" in source or "ExitRequested" in source
    assert "WebviewWindowBuilder" not in source


def test_receiver_sidecar_points_playwright_to_bundled_browser_resources() -> None:
    source = (ROOT / "desktop" / "src-tauri" / "src" / "receiver_sidecar.rs").read_text(encoding="utf-8")

    assert "PLAYWRIGHT_BROWSERS_PATH" in source
    assert "playwright-browsers" in source
    assert "resource_dir" in source


def test_receiver_sidecar_rechecks_health_and_retries_failed_startup() -> None:
    source = (ROOT / "desktop" / "src-tauri" / "src" / "receiver_sidecar.rs").read_text(encoding="utf-8")

    assert "healthcheck_ready(port, Duration::from_secs(1))" in source
    assert "cleanup_receiver_sidecar(app);" in source
    assert "for attempt in 1..=2" in source
    assert "kill_listener_on_port(port)" in source
    assert "启动后未通过健康检查" in source


def test_loading_page_centers_panel_contents() -> None:
    source = (ROOT / "desktop" / "loading" / "index.html").read_text(encoding="utf-8")

    assert ".panel" in source
    assert "text-align: center" in source
    assert "margin-left: auto" in source
    assert "margin-right: auto" in source
