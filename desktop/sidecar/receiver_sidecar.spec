from pathlib import Path


project_root = Path(SPECPATH).resolve().parents[1]
entrypoint = project_root / "desktop" / "sidecar" / "entrypoint.py"

hiddenimports = [
    "worldgs_receiver.app",
    "worldgs_receiver.cli",
    "playwright.sync_api",
]

datas = [
    (str(project_root / "worldgs_receiver" / "static"), "worldgs_receiver/static"),
    (
        str(project_root / "worldgs_receiver" / "automation_platform_configs"),
        "worldgs_receiver/automation_platform_configs",
    ),
]

a = Analysis(
    [str(entrypoint)],
    pathex=[str(project_root)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="receiver_sidecar",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
)
