import json
import io
import sys
import time
from pathlib import Path
from zipfile import ZipFile

import pytest

from worldgs_receiver.script_registry import ScriptRegistry
from worldgs_receiver.script_runner import ScriptRunner, ScriptRunnerStore


def make_script_zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with ZipFile(buffer, "w") as archive:
        for relative_path, content in files.items():
            archive.writestr(relative_path, content)
    return buffer.getvalue()


def make_task(tmp_path: Path) -> tuple[Path, str]:
    upload_id = "b" * 32
    task_dir = tmp_path / "2026-07-04" / f"room_{upload_id[:8]}"
    dataset = task_dir / "dataset"
    (dataset / "images").mkdir(parents=True)
    (dataset / "images" / "frame_000001.jpg").write_bytes(b"jpg")
    (dataset / "sceneDataset").mkdir()
    for name in ("cameras.txt", "images.txt", "points3D.txt"):
        (dataset / "sceneDataset" / name).write_text(name, encoding="utf-8")
    (task_dir / "upload_report.json").write_text(
        json.dumps(
            {
                "ok": True,
                "uploadId": upload_id,
                "taskName": "room",
                "datasetPath": str(dataset),
                "openPath": str(task_dir),
                "packagePath": str(dataset),
                "extractedPath": str(dataset),
            }
        ),
        encoding="utf-8",
    )
    return task_dir, upload_id


def test_script_runner_passes_environment_and_registers_model_result(tmp_path: Path) -> None:
    task_dir, upload_id = make_task(tmp_path)
    registry = ScriptRegistry(tmp_path)
    runner_script = (
        "#!/usr/bin/env bash\n"
        "set -e\n"
        "printf '%s' \"$WORLDGS_TASK_DIR\" > \"$WORLDGS_RUN_OUTPUT_DIR/task_dir.txt\"\n"
        "cat > \"$WORLDGS_OUTPUT_JSON\" <<JSON\n"
        "{\n"
        "  \"status\": \"succeeded\",\n"
        "  \"model_path\": \"$WORLDGS_RUN_OUTPUT_DIR/mobile.ply\",\n"
        "  \"message\": \"脚本成功\"\n"
        "}\n"
        "JSON\n"
        "printf 'ply\\nformat ascii 1.0\\nelement vertex 0\\nend_header\\n' > \"$WORLDGS_RUN_OUTPUT_DIR/mobile.ply\"\n"
    ).encode("utf-8")
    script = registry.create_script(
        name="示例脚本",
        description="测试脚本",
        script_type="platform",
        filename="run.sh",
        content=runner_script,
    )

    summary = ScriptRunner(ScriptRunnerStore(tmp_path), registry).start(script_id=script["scriptId"], upload_id=upload_id, wait=True)

    assert summary["status"] == "succeeded"
    assert summary["message"] == "脚本成功"
    assert (task_dir / "results" / "mobile.ply").is_file()
    assert json.loads((task_dir / "results" / "model_result.json").read_text(encoding="utf-8"))["source"] == "user-script"
    output_dir = Path(str(summary["runDir"])) / "outputs"
    assert (output_dir / "task_dir.txt").read_text(encoding="utf-8") == str(task_dir)


def test_script_runner_allows_success_without_model_result(tmp_path: Path) -> None:
    _, upload_id = make_task(tmp_path)
    registry = ScriptRegistry(tmp_path)
    script = registry.create_script(
        name="仅提交脚本",
        description="只返回 preview url",
        script_type="platform",
        filename="run.py",
        content=(
            "import json, os\n"
            "with open(os.environ['WORLDGS_OUTPUT_JSON'], 'w', encoding='utf-8') as handle:\n"
            "    json.dump({'status': 'succeeded', 'preview_url': 'https://example.com/model/1'}, handle)\n"
        ).encode("utf-8"),
    )

    summary = ScriptRunner(ScriptRunnerStore(tmp_path), registry).start(script_id=script["scriptId"], upload_id=upload_id, wait=True)

    assert summary["status"] == "succeeded"
    assert summary["previewUrl"] == "https://example.com/model/1"
    assert summary["modelResult"] is None


def test_script_runner_executes_zip_bundle_script_project(tmp_path: Path) -> None:
    _, upload_id = make_task(tmp_path)
    registry = ScriptRegistry(tmp_path)
    script = registry.create_script(
        name="知天下项目脚本",
        description="zip 项目",
        script_type="platform",
        filename="explorerglobal.zip",
        content=make_script_zip(
            {
                "run_explorerglobal.sh": (
                    "#!/usr/bin/env bash\n"
                    "set -e\n"
                    "SCRIPT_DIR=\"$(cd \"$(dirname \"$0\")\" && pwd)\"\n"
                    "python3 \"$SCRIPT_DIR/src/main.py\"\n"
                ).encode("utf-8"),
                "src/main.py": (
                    "import json, os\n"
                    "with open(os.environ['WORLDGS_OUTPUT_JSON'], 'w', encoding='utf-8') as handle:\n"
                    "    json.dump({'status': 'succeeded', 'message': 'zip 脚本成功'}, handle)\n"
                ).encode("utf-8"),
            }
        ),
        entry_file="run_explorerglobal.sh",
    )

    summary = ScriptRunner(ScriptRunnerStore(tmp_path), registry).start(script_id=script["scriptId"], upload_id=upload_id, wait=True)

    assert summary["status"] == "succeeded"
    assert summary["message"] == "zip 脚本成功"


def test_script_runner_executes_custom_action_command(tmp_path: Path) -> None:
    _, upload_id = make_task(tmp_path)
    registry = ScriptRegistry(tmp_path)
    script = registry.create_script(
        name="知天下项目脚本",
        description="zip 项目",
        script_type="platform",
        filename="explorerglobal.zip",
        content=make_script_zip(
            {
                "run_explorerglobal.sh": (
                    "#!/usr/bin/env bash\n"
                    "set -e\n"
                    "SCRIPT_DIR=\"$(cd \"$(dirname \"$0\")\" && pwd)\"\n"
                    "python3 \"$SCRIPT_DIR/src/main.py\" \"$@\"\n"
                ).encode("utf-8"),
                "src/main.py": (
                    "import json, os, sys\n"
                    "with open(os.environ['WORLDGS_OUTPUT_JSON'], 'w', encoding='utf-8') as handle:\n"
                    "    json.dump({'status': 'succeeded', 'message': ' '.join(sys.argv[1:])}, handle)\n"
                ).encode("utf-8"),
            }
        ),
        entry_file="run_explorerglobal.sh",
        custom_actions=[{"name": "登录", "command": "run_explorerglobal.sh --login"}],
    )
    action_id = script["customActions"][0]["actionId"]

    summary = ScriptRunner(ScriptRunnerStore(tmp_path), registry).start(
        script_id=script["scriptId"],
        upload_id=upload_id,
        action_id=action_id,
        wait=True,
    )

    assert summary["status"] == "succeeded"
    assert summary["actionId"] == action_id
    assert summary["actionName"] == "知天下项目脚本 · 登录"
    assert summary["message"] == "--login"


def test_script_runner_executes_global_custom_action_without_upload(tmp_path: Path) -> None:
    registry = ScriptRegistry(tmp_path)
    script = registry.create_script(
        name="知天下项目脚本",
        description="zip 项目",
        script_type="platform",
        filename="explorerglobal.zip",
        content=make_script_zip(
            {
                "run_explorerglobal.sh": (
                    "#!/usr/bin/env bash\n"
                    "set -e\n"
                    "SCRIPT_DIR=\"$(cd \"$(dirname \"$0\")\" && pwd)\"\n"
                    "python3 \"$SCRIPT_DIR/src/main.py\" \"$@\"\n"
                ).encode("utf-8"),
                "src/main.py": (
                    "import json, os, sys\n"
                    "with open(os.environ['WORLDGS_OUTPUT_JSON'], 'w', encoding='utf-8') as handle:\n"
                    "    json.dump({'status': 'succeeded', 'message': ' '.join(sys.argv[1:]), 'results_dir': os.environ['WORLDGS_RESULTS_DIR']}, handle)\n"
                ).encode("utf-8"),
            }
        ),
        entry_file="run_explorerglobal.sh",
        custom_actions=[{"name": "登录", "command": "run_explorerglobal.sh --login"}],
    )
    runner = ScriptRunner(ScriptRunnerStore(tmp_path), registry)
    action_id = script["customActions"][0]["actionId"]

    summary = runner.start(script_id=script["scriptId"], action_id=action_id, wait=True)

    assert summary["status"] == "succeeded"
    assert summary["uploadId"] == ""
    assert summary["isGlobalAction"] is True
    assert summary["actionName"] == "知天下项目脚本 · 登录"
    assert summary["message"] == "--login"
    assert str(summary["resultsDir"]).endswith("/scripts/" + script["scriptId"] + "/.runtime/results")
    assert Path(str(summary["runDir"])).is_relative_to(tmp_path / "scripts" / script["scriptId"] / ".runtime" / "global_runs")


def test_script_runner_rejects_global_default_run_without_action(tmp_path: Path) -> None:
    registry = ScriptRegistry(tmp_path)
    script = registry.create_script(
        name="默认脚本",
        description="缺少 action",
        script_type="generic",
        filename="run.sh",
        content=b"#!/usr/bin/env bash\nexit 0\n",
    )

    with pytest.raises(ValueError, match="global script run requires actionId"):
        ScriptRunner(ScriptRunnerStore(tmp_path), registry).start(script_id=script["scriptId"], wait=True)


def test_script_runner_fails_on_out_of_scope_model_path(tmp_path: Path) -> None:
    _, upload_id = make_task(tmp_path)
    registry = ScriptRegistry(tmp_path)
    outside_file = tmp_path / "outside.ply"
    outside_file.write_text("ply\nformat ascii 1.0\nelement vertex 0\nend_header\n", encoding="utf-8")
    script = registry.create_script(
        name="越界脚本",
        description="返回外部路径",
        script_type="generic",
        filename="run.py",
        content=(
            "import json, os\n"
            f"with open(os.environ['WORLDGS_OUTPUT_JSON'], 'w', encoding='utf-8') as handle:\n"
            f"    json.dump({{'status': 'succeeded', 'model_path': {outside_file.as_posix()!r}}}, handle)\n"
        ).encode("utf-8"),
    )

    summary = ScriptRunner(ScriptRunnerStore(tmp_path), registry).start(script_id=script["scriptId"], upload_id=upload_id, wait=True)

    assert summary["status"] == "failed"
    assert "task directory" in str(summary["message"])


def test_script_runner_cancel_keeps_cancelled_state(tmp_path: Path) -> None:
    _, upload_id = make_task(tmp_path)
    registry = ScriptRegistry(tmp_path)
    script = registry.create_script(
        name="可取消脚本",
        description="sleep",
        script_type="generic",
        filename="run.py",
        content=b"import time\ntime.sleep(5)\n",
    )
    runner = ScriptRunner(ScriptRunnerStore(tmp_path), registry)

    summary = runner.start(script_id=script["scriptId"], upload_id=upload_id)
    run_id = str(summary["scriptRunId"])
    for _ in range(20):
        if runner.read(run_id)["status"] == "running":
            break
        time.sleep(0.05)

    cancelled = runner.cancel(run_id)
    time.sleep(0.2)

    assert cancelled["status"] == "cancelled"
    assert runner.read(run_id)["status"] == "cancelled"
