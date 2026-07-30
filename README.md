# WorldGS Receiver

![Version](https://img.shields.io/badge/version-0.1.0-blue)
![License](https://img.shields.io/badge/license-MIT-green)

WorldGS Receiver 是电脑端本地接收服务。手机端扫描 Receiver 页面二维码后，可以把当前采集任务包上传到电脑指定目录。

这套能力目前有两种使用方式：

- 浏览器版本地服务：直接运行 `start_receiver.sh` / `start_receiver.bat`
- 桌面安装包：见 [desktop/README.md](desktop/README.md)

## 目录说明

```text
worldgs-receiver/
  start_receiver.sh          # macOS / Linux 启动脚本
  start_receiver.bat         # Windows 启动脚本
  worldgs_receiver/          # FastAPI 服务与页面
  SCRIPT_PROTOCOL.md         # 外部训练脚本对接协议
  desktop/                   # Tauri 桌面壳、sidecar 打包脚本
```

## 外部脚本接入

Receiver 支持通过 `SCRIPT_PROTOCOL.md` 定义的协议与外部训练脚本对接。

脚本注册支持两种方式：

1. 单文件脚本：直接上传 `.sh/.bash/.py`
2. 多文件脚本项目：上传 `.zip`，并填写 zip 包内的入口相对路径 `entryFile`

脚本设置还支持：

- 编辑已有脚本名称、类型、描述、入口相对路径和脚本文件
- 为脚本配置多个"自定义动作"

这些自定义动作会直接出现在任务卡"开始训练"菜单里，点击后由 Receiver 按统一脚本协议启动。

### 外部脚本示例项目

以下是外部脚本的参考实现（不在本仓库中）：

- **知天下云行脚本**：通过 Playwright 自动化知天下云行平台的训练流程
- **本地 3DGS 训练脚本**：通过 gsplat 等工具在本地 GPU 上进行 3DGS 训练

这两个脚本项目遵循 `SCRIPT_PROTOCOL.md` 协议，通过 `WORLDGS_*` 环境变量与 Receiver 通信。

## 快速启动

### macOS / Linux

```bash
./start_receiver.sh
```

### Windows

```bat
start_receiver.bat
```

## 启动脚本会做什么

`start_receiver.sh` 和 `start_receiver.bat` 都是自举脚本，首次接手项目的人直接运行即可。脚本会自动：

1. 检查当前 Python 环境是否已具备 Receiver 依赖。
2. 当前环境不可用时创建 `.venv` 虚拟环境。
3. 安装 `requirements.txt` 中的依赖。
4. 检查并安装 Playwright Firefox 浏览器。
5. 启动 `python -m worldgs_receiver.cli`。

其中：

- macOS / Linux 脚本会在启动前检查并清理目标端口占用。
- Windows 脚本会优先复用已可用的 Python 环境；不可用时再落回 `.venv`。

## 默认行为

默认服务地址：

```text
http://localhost:8787
```

默认接收目录：

```text
~/WorldGS_Imports
```

## 常用命令

### 改端口和输出目录

macOS / Linux:

```bash
./start_receiver.sh --port 8788 --output ~/WorldGS_Imports
```

Windows:

```bat
start_receiver.bat --port 8788 --output C:\\WorldGS_Imports
```

### 直接用当前 Python 环境启动

```bash
python -m worldgs_receiver.cli --port 8787 --output ~/WorldGS_Imports
```

适用于你已经手动安装好依赖，想绕过自举脚本排查问题的场景。

### 安装测试依赖并跑测试

```bash
python -m pip install -e ".[test]"
python -m pytest
```

## 桌面端编译 / 打包入口

如果目标不是浏览器版本地服务，而是给别人分发桌面应用，不要只运行 `start_receiver.*`，应改走 `desktop/` 下的构建脚本：

- 只构建 Python sidecar：
  - macOS: `bash desktop/scripts/build_sidecar_macos.sh`
  - Windows: `powershell -ExecutionPolicy Bypass -File .\\desktop\\scripts\\build_sidecar_windows.ps1`
  - Linux: `bash desktop/scripts/build_sidecar_linux.sh`
- 构建完整桌面安装包：
  - macOS: `bash desktop/scripts/build_desktop_macos.sh`
  - Windows: `powershell -ExecutionPolicy Bypass -File .\\desktop\\scripts\\build_desktop_windows.ps1`
  - Linux: `bash desktop/scripts/build_desktop_linux.sh`

完整说明、先决条件和产物目录见 [desktop/README.md](desktop/README.md)。

## 前置依赖

- Python 3.9+（浏览器版）
- Python 3.10+（桌面端 sidecar 打包）
- Node.js 18+（桌面端）
- Rust toolchain（桌面端）
- Playwright Firefox 浏览器（运行时自动安装）

## 许可证

MIT License
