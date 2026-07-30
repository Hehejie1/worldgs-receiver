# Receiver 脚本协议

这份文档给外部训练脚本项目使用。

Receiver 的职责只有三件事：

1. 传入任务上下文
2. 启动脚本
3. 收集日志并回填结果

Receiver 不负责：

- 平台登录态
- 浏览器 profile
- Playwright 安装
- 本地 3DGS 训练环境安装

## 1. 启动方式

支持的上传形式：

- `.sh`
- `.bash`
- `.py`
- `.zip`

其中：

- 单文件脚本可直接上传 `.sh/.bash/.py`
- 多文件脚本项目必须上传 `.zip`，并额外提供 zip 包内的 `entryFile` 相对路径

Receiver 会把脚本文件或脚本包托管到自己的目录，再按入口文件类型启动：

- `.sh/.bash`：直接执行
- `.py`：使用当前 Python 解释器执行

如果脚本在设置里配置了"自定义动作"，Receiver 不会把任意命令直接透传给 shell，而是：

1. 从脚本注册表里按 `actionId` 找到对应动作
2. 解析动作命令的第一个 token 为"相对脚本目录的入口文件"
3. 校验入口文件仍在当前脚本目录内
4. 再把剩余参数拼到最终启动命令后面

如果脚本依赖 `src/main.py`、`requirements.txt`、模板文件、资源文件等伴随目录，不能只上传入口 shell；必须把完整项目打成 zip 包后再注册。

## 2. 输入环境变量

每次脚本运行时，Receiver 会注入这些环境变量：

- `WORLDGS_UPLOAD_ID`
- `WORLDGS_TASK_NAME`
- `WORLDGS_TASK_DIR`
- `WORLDGS_DATASET_DIR`
- `WORLDGS_IMAGES_DIR`
- `WORLDGS_SCENE_DATASET_DIR`
- `WORLDGS_RESULTS_DIR`
- `WORLDGS_RUN_DIR`
- `WORLDGS_RUN_OUTPUT_DIR`
- `WORLDGS_RUN_LOG_PATH`
- `WORLDGS_OUTPUT_JSON`
- `WORLDGS_SCRIPT_NAME`
- `WORLDGS_SCRIPT_TYPE`
- `WORLDGS_SCRIPT_ACTION_ID`
- `WORLDGS_SCRIPT_ACTION_NAME`
- `WORLDGS_RECEIVER_VERSION`

## 3. 退出码

- `0`：脚本成功
- 非 `0`：脚本失败

注意：

- 就算脚本写了 `"status": "succeeded"`，但退出码非 `0`，Receiver 仍会按失败处理。

## 4. 输出文件

脚本应把结构化结果写到 `WORLDGS_OUTPUT_JSON`。

成功最小示例：

```json
{
  "status": "succeeded",
  "model_path": "/absolute/path/to/mobile.ply",
  "preview_url": "https://example.com/model/123",
  "message": "训练完成"
}
```

失败最小示例：

```json
{
  "status": "failed",
  "message": "Playwright 登录超时"
}
```

字段说明：

- `status`：`succeeded | failed`
- `message`：给 Receiver UI 展示的摘要
- `model_path`：可选。最终模型路径，支持 `.ply` 和 `.sog`
- `preview_url`：可选。第三方平台结果页地址

## 5. 路径约束

`model_path` 必须满足：

- 文件真实存在
- 位于当前任务目录 `WORLDGS_TASK_DIR` 内，或位于当前运行目录 `WORLDGS_RUN_DIR` 内
- 扩展名为 `.ply` 或 `.sog`

Receiver 会把合法结果复制到：

- `results/mobile.ply`
- 或 `results/mobile.sog`

并统一写：

- `results/model_result.json`

## 6. 日志约束

脚本可以自由写 stdout/stderr。

Receiver 会统一保存为：

- `<runDir>/stdout.log`
- `<runDir>/stderr.log`

不要让脚本直接写这些 Receiver 私有文件：

- `summary.json`
- `model_result.json`

## 7. 运行目录

每次脚本执行，Receiver 会创建：

```text
<taskDir>/script_runs/<runId>/
  summary.json
  stdout.log
  stderr.log
  output.json
  outputs/
```

建议脚本把自己的产物优先写到：

```text
$WORLDGS_RUN_OUTPUT_DIR
```

## 8. 最小 shell 示例

```bash
#!/usr/bin/env bash
set -euo pipefail

printf 'ply\nformat ascii 1.0\nelement vertex 0\nend_header\n' > "$WORLDGS_RUN_OUTPUT_DIR/mobile.ply"

cat > "$WORLDGS_OUTPUT_JSON" <<JSON
{
  "status": "succeeded",
  "model_path": "$WORLDGS_RUN_OUTPUT_DIR/mobile.ply",
  "message": "脚本执行完成"
}
JSON
```

## 9. 外部脚本示例项目

以下是遵循本协议的参考实现（不在本仓库中）：

- **知天下云行脚本** (`explorerglobal-script`)：通过 Playwright 自动化知天下云行平台的训练流程
- **本地 3DGS 训练脚本** (`local-training-script`)：通过 gsplat 等工具在本地 GPU 上进行 3DGS 训练

这些项目不放入本仓库，可作为外部脚本接入的参考。
