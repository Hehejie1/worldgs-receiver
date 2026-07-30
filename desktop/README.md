# WorldGS 桌面端

这个目录承载 `WorldGS Receiver` 的 Tauri 壳、Python sidecar 打包配置和桌面构建脚本。

## 目录结构

- `package.json`：Tauri CLI 入口。
- `loading/index.html`：桌面端启动中的本地加载页。
- `src-tauri/`：Rust 桌面壳、Tauri 配置和 sidecar 生命周期逻辑。
- `sidecar/`：PyInstaller 入口和 spec。
- `scripts/`：sidecar 与桌面端打包脚本。

## 这几个脚本分别干什么

- `../start_receiver.sh` / `../start_receiver.bat`
  - 启动浏览器版 Receiver
  - 适合本地开发、调试接口、验证上传链路
- `scripts/build_sidecar_macos.sh` / `scripts/build_sidecar_windows.ps1` / `scripts/build_sidecar_linux.sh`
  - 只构建 Python Receiver sidecar 可执行文件
  - 适合排查 PyInstaller、Playwright 浏览器资源、sidecar 运行问题
- `scripts/build_desktop_macos.sh` / `scripts/build_desktop_windows.ps1` / `scripts/build_desktop_linux.sh`
  - 构建完整桌面安装包
  - 这是给别人分发 Windows / macOS / Linux 安装包时要走的入口

## 先决条件

- Python 3.10+
- Node.js 18+
- Rust toolchain
- macOS 或 Windows 对应平台的 Tauri 构建依赖
- Linux / Jetson 打 `.deb` 时需要 WebKitGTK、GTK、AppIndicator、librsvg 等 Tauri Linux 构建依赖

额外说明：

- Windows 打包机还需要可用的 MSVC / Visual Studio C++ Build Tools。
- 这套脚本默认在目标平台本机构建，不承诺跨平台产出安装包。

## 只构建 Receiver sidecar

macOS:

```bash
bash desktop/scripts/build_sidecar_macos.sh
```

Windows:

```powershell
powershell -ExecutionPolicy Bypass -File .\desktop\scripts\build_sidecar_windows.ps1
```

Linux:

```bash
bash desktop/scripts/build_sidecar_linux.sh
```

sidecar 输出位置：

- `dist/receiver_sidecar`
- `dist/receiver_sidecar.exe`

Windows sidecar 脚本会执行：

1. `python -m pip install -e ".[desktop]"`
2. `python -m playwright install firefox`
3. `python -m PyInstaller desktop/sidecar/receiver_sidecar.spec --noconfirm --clean`

macOS sidecar 脚本会自动查找 Python 3.10+：

1. 优先使用显式传入的 `PYTHON_BIN`
2. 否则尝试 `python3.11`、`python3.10`、`python3`
3. 如果最终只能命中 Python 3.9 或更低版本，会直接失败
4. PyInstaller 缓存会落到项目内 `build/pyinstaller-cache/`，避免命中系统缓存目录的权限残留问题

## 构建桌面安装包

macOS:

```bash
bash desktop/scripts/build_desktop_macos.sh
```

Windows:

```powershell
powershell -ExecutionPolicy Bypass -File .\desktop\scripts\build_desktop_windows.ps1
```

Linux:

```bash
bash desktop/scripts/build_desktop_linux.sh
```

构建脚本会做三件事：

1. 调用 `build_sidecar_*` 生成 sidecar。
2. 根据 `rustc --print host-tuple` 把 sidecar 复制到 `desktop/src-tauri/binaries/receiver_sidecar-<target-triple>[.exe]`。
3. 在 `desktop/` 下执行 `npm install` 和 Tauri bundling。

macOS 当前的完整打包收尾策略是：

1. 执行 `npx tauri build --target <host-triple> --bundles app` 只生成 `.app`
2. 再基于 `WorldGS.app` 用 `hdiutil create` 手工生成 `dmg`

这样可以绕开 Tauri 自带 `bundle_dmg.sh` 在当前环境里依赖 `osascript` / Finder 美化时的挂起问题，同时仍然保留标准拖拽安装结构（`WorldGS.app` + `Applications`）。

macOS Apple Silicon 机器会优先使用 `aarch64-apple-darwin` native 包。

## 产物位置

Tauri bundle 产物默认在：

- `desktop/src-tauri/target/release/bundle/dmg`
- `desktop/src-tauri/target/release/bundle/macos`
- `desktop/src-tauri/target/release/bundle/deb`
- `desktop/src-tauri/target/release/bundle/msi`
- `desktop/src-tauri/target/release/bundle/nsis`

Windows 发布时优先关注：

- `bundle/msi`
- `bundle/nsis`

Linux / Jetson 发布时优先关注：

- `bundle/deb`

## 首次安装说明（无代码签名版本）

由于当前版本未进行代码签名，用户首次安装时需要：

### macOS

1. 打开 `WorldGS.dmg`，将 `WorldGS.app` 拖入 `Applications` 文件夹
2. 首次打开时，如果系统提示"无法打开，因为无法验证开发者"，请：
   - 打开「系统设置」→「隐私与安全性」，点击「仍要打开」
   - 或在终端执行：`xattr -cr /Applications/WorldGS.app`

### Windows

1. 双击安装包
2. Windows SmartScreen 可能会提示"未识别的应用"，点击「更多信息」→「仍要运行」

### Linux

直接双击 `.deb` 包或使用 `dpkg -i` 安装即可。

## 推荐阅读顺序

第一次接手这个模块，建议按这个顺序看：

1. [../README.md](../README.md)：先会启动浏览器版 Receiver。
2. 本文：明确 sidecar 和桌面安装包各自的构建入口。
3. `scripts/build_sidecar_*`：定位 sidecar 打包问题。
4. `scripts/build_desktop_*`：定位 Tauri 安装包问题。

## 当前约束

- sidecar 仍是 Python Receiver，可继续复用现有 FastAPI、Playwright 和本地输出目录语义。
- 如果本机 Rust / LLVM 环境异常，`tauri build` 或 `cargo test` 会先失败，需要先修复宿主机工具链。
