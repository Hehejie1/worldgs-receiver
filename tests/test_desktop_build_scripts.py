from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_macos_desktop_build_script_chains_sidecar_target_triple_and_tauri_build() -> None:
    script = (ROOT / "desktop" / "scripts" / "build_desktop_macos.sh").read_text(encoding="utf-8")

    assert "build_sidecar_macos.sh" in script
    assert 'WORLDGS_RUST_BIN_DIR' in script
    assert 'export PATH="$HOME/.cargo/bin:$PATH"' in script
    assert "sysctl -in hw.optional.arm64" in script
    assert "stable-aarch64-apple-darwin" in script
    assert "stable-x86_64-apple-darwin" in script
    assert "rustc --print host-tuple" in script or "rustc -Vv" in script
    assert 'TAURI_CONFIG_PATH="$DESKTOP_ROOT/src-tauri/tauri.conf.json"' in script
    assert 'PRODUCT_NAME="$(sed -n ' in script
    assert 'APP_VERSION="$(sed -n ' in script
    assert "src-tauri/binaries/receiver_sidecar-" in script
    assert 'npm_config_registry="${NPM_CONFIG_REGISTRY:-https://registry.npmjs.org}"' in script
    assert "npm install" in script
    assert 'tauri build --target "$HOST_TRIPLE" --bundles app' in script
    assert 'DMG_OUTPUT_PATH="$DMG_OUTPUT_DIR/${PRODUCT_NAME}_${APP_VERSION}_${DMG_ARCH_SUFFIX}.dmg"' in script
    assert 'ditto "$APP_BUNDLE_PATH" "$DMG_STAGE_DIR/$PRODUCT_NAME.app"' in script
    assert 'ln -s /Applications "$DMG_STAGE_DIR/Applications"' in script
    assert 'hdiutil create -volname "$PRODUCT_NAME" -srcfolder "$DMG_STAGE_DIR" -ov -format UDZO "$DMG_OUTPUT_PATH"' in script


def test_windows_desktop_build_script_chains_sidecar_target_triple_and_tauri_build() -> None:
    script = (ROOT / "desktop" / "scripts" / "build_desktop_windows.ps1").read_text(encoding="utf-8")

    assert "build_sidecar_windows.ps1" in script
    assert "rustc --print host-tuple" in script or "rustc -Vv" in script
    assert "src-tauri\\binaries\\receiver_sidecar-" in script
    assert "npm" in script
    assert "tauri build --target $hostTriple" in script


def test_desktop_readme_documents_build_steps_and_bundle_outputs() -> None:
    readme = (ROOT / "desktop" / "README.md").read_text(encoding="utf-8")

    assert "build_sidecar_macos.sh" in readme
    assert "build_sidecar_windows.ps1" in readme
    assert "build_desktop_macos.sh" in readme
    assert "build_desktop_windows.ps1" in readme
    assert "src-tauri/target/release/bundle" in readme


def test_macos_sidecar_build_script_accepts_python_command_with_arch_prefix() -> None:
    script = (ROOT / "desktop" / "scripts" / "build_sidecar_macos.sh").read_text(encoding="utf-8")

    assert 'PYTHON_BIN="${PYTHON_BIN:-}"' in script
    assert "resolve_python_bin()" in script
    assert "sys.version_info >= (3, 10)" in script
    assert '$HOME/.pyenv/versions/3.10.13/bin/python' in script
    assert "python3.11" in script
    assert "python3.10" in script
    assert 'PYINSTALLER_CACHE_ROOT="$BUILD_ROOT/pyinstaller-cache"' in script
    assert 'mkdir -p "$PYINSTALLER_CACHE_ROOT"' in script
    assert 'read -r -a PYTHON_CMD <<< "$PYTHON_BIN"' in script
    assert "sysctl -in hw.optional.arm64" in script
    assert 'PYTHON_BIN="$(resolve_python_bin)"' in script
    assert '"${PYTHON_CMD[@]}" -m pip install --user --upgrade pip setuptools wheel' in script
    assert '"${PYTHON_CMD[@]}" -m pip install' in script
    assert 'playwright install --dry-run firefox' in script
    assert "EXPECTED_FIREFOX_DIR" in script
    assert 'ln -sfn "$(basename "$EXISTING_FIREFOX_DIR")" "$EXPECTED_FIREFOX_DIR"' in script
    assert "--no-binary pydantic-core" in script
    assert 'PYINSTALLER_CONFIG_DIR="$PYINSTALLER_CACHE_ROOT" "${PYTHON_CMD[@]}" -m PyInstaller' in script
    assert "--target-arch" not in script
