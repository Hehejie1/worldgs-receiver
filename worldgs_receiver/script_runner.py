import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .script_contract import ACTIVE_SCRIPT_RUN_STATUSES, now_iso
from .script_registry import ScriptRegistry


@dataclass(frozen=True)
class ScriptRunnerStore:
    output_dir: Path


class ScriptRunner:
    def __init__(self, store: ScriptRunnerStore, registry: ScriptRegistry) -> None:
        self.store = store
        self.registry = registry
        self._threads: dict[str, threading.Thread] = {}
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._lock = threading.Lock()

    def start(
        self,
        *,
        script_id: str,
        upload_id: str = "",
        action_id: Optional[str] = None,
        wait: bool = False,
    ) -> dict[str, object]:
        script = self.registry.get_script(script_id)
        if not bool(script.get("enabled", True)):
            raise RuntimeError("script is disabled")
        is_global_action = not upload_id
        if is_global_action and not action_id:
            raise ValueError("global script run requires actionId")
        task_dir, task_name, run_root, results_dir = self._context_for_run(
            script=script,
            upload_id=upload_id,
            action_id=action_id,
        )
        active = (
            self._latest_global_for_script(str(script["scriptId"]), active_only=True)
            if is_global_action
            else self.latest_for_upload(upload_id, active_only=True)
        )
        if active:
            return active
        action_name, command = self._command_for_run(script=script, action_id=action_id)
        run_id = f"run_{uuid.uuid4().hex}"
        run_dir = run_root / run_id
        outputs_dir = run_dir / "outputs"
        outputs_dir.mkdir(parents=True, exist_ok=True)
        summary = {
            "scriptRunId": run_id,
            "scriptId": script["scriptId"],
            "scriptName": script["name"],
            "scriptType": script["scriptType"],
            "actionId": action_id,
            "actionName": action_name,
            "uploadId": upload_id,
            "isGlobalAction": is_global_action,
            "taskName": task_name,
            "status": "queued",
            "message": f"脚本 {action_name} 已排队",
            "taskDir": str(task_dir),
            "runDir": str(run_dir),
            "resultsDir": str(results_dir),
            "stdoutPath": str(run_dir / "stdout.log"),
            "stderrPath": str(run_dir / "stderr.log"),
            "outputJsonPath": str(run_dir / "output.json"),
            "startedAt": now_iso(),
            "endedAt": None,
            "exitCode": None,
            "modelResult": None,
            "previewUrl": None,
        }
        self._write_summary(run_dir, summary)
        if wait:
            self._run(run_id, script, upload_id, task_name, task_dir, results_dir, run_dir, outputs_dir, command)
        else:
            thread = threading.Thread(
                target=self._run,
                args=(run_id, script, upload_id, task_name, task_dir, results_dir, run_dir, outputs_dir, command),
                daemon=True,
                name=f"receiver-script-{run_id[:12]}",
            )
            with self._lock:
                self._threads[run_id] = thread
            thread.start()
        return self.read(run_id)

    def read(self, run_id: str) -> dict[str, object]:
        for summary_path in self._summary_candidates(run_id):
            return json.loads(summary_path.read_text(encoding="utf-8"))
        raise FileNotFoundError(f"script run not found: {run_id}")

    def cancel(self, run_id: str) -> dict[str, object]:
        with self._lock:
            process = self._processes.get(run_id)
        if process and process.poll() is None:
            process.terminate()
        summary = self.read(run_id)
        run_dir = Path(str(summary["runDir"]))
        self._write_summary(
            run_dir,
            {
                **summary,
                "status": "cancelled",
                "message": f"脚本 {summary.get('scriptName') or ''} 已取消".strip(),
                "endedAt": now_iso(),
            },
        )
        return self.read(run_id)

    def latest_for_upload(self, upload_id: str, *, active_only: bool = False) -> Optional[dict[str, object]]:
        latest: Optional[dict[str, object]] = None
        for summary_path in self.store.output_dir.glob("*/*/script_runs/*/summary.json"):
            try:
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if summary.get("uploadId") != upload_id:
                continue
            if active_only and summary.get("status") not in ACTIVE_SCRIPT_RUN_STATUSES:
                continue
            if latest is None or str(summary.get("startedAt") or "") > str(latest.get("startedAt") or ""):
                latest = summary
        return latest

    def _run(
        self,
        run_id: str,
        script: dict[str, object],
        upload_id: str,
        task_name: str,
        task_dir: Path,
        results_dir: Path,
        run_dir: Path,
        outputs_dir: Path,
        command: list[str],
    ) -> None:
        summary = self.read(run_id)
        output_json_path = run_dir / "output.json"
        stdout_path = run_dir / "stdout.log"
        stderr_path = run_dir / "stderr.log"
        env = self._build_env(
            script=script,
            upload_id=upload_id,
            task_name=task_name,
            task_dir=task_dir,
            results_dir=results_dir,
            run_dir=run_dir,
            outputs_dir=outputs_dir,
            output_json_path=output_json_path,
            stdout_path=stdout_path,
            action_id=summary.get("actionId"),
            action_name=summary.get("actionName"),
        )
        self._write_summary(
            run_dir,
            {
                **summary,
                "status": "running",
                "message": f"脚本 {summary.get('actionName') or script['name']} 运行中",
                "command": command,
            },
        )
        try:
            with stdout_path.open("w", encoding="utf-8") as stdout_file, stderr_path.open(
                "w", encoding="utf-8"
            ) as stderr_file:
                process = subprocess.Popen(
                    command,
                    cwd=str(Path(str(script["entryFile"])).parent),
                    env=env,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    text=True,
                )
                with self._lock:
                    self._processes[run_id] = process
                exit_code = process.wait()
            current = self.read(run_id)
            if current.get("status") == "cancelled":
                self._write_summary(run_dir, {**current, "exitCode": exit_code, "endedAt": now_iso()})
                return
            payload = self._read_output_json(output_json_path)
            final_status = str(payload.get("status") or ("succeeded" if exit_code == 0 else "failed")).strip().lower()
            if exit_code != 0 and final_status == "succeeded":
                final_status = "failed"
            model_result = None
            preview_url = payload.get("preview_url") or payload.get("previewUrl")
            if final_status == "succeeded":
                model_path = self._resolve_model_path(
                    payload.get("model_path") or payload.get("modelPath"),
                    run_dir=run_dir,
                    task_dir=task_dir,
                )
                if model_path is not None:
                    model_result = self._publish_model_result(results_dir, run_id, model_path)
            message = str(payload.get("message") or "")
            if not message:
                if exit_code == 0 and final_status == "succeeded":
                    message = f"脚本 {summary.get('actionName') or script['name']} 执行完成"
                elif exit_code != 0:
                    message = f"脚本 {summary.get('actionName') or script['name']} 失败，退出码 {exit_code}"
                else:
                    message = f"脚本 {summary.get('actionName') or script['name']} 已结束"
            self._write_summary(
                run_dir,
                {
                    **current,
                    "status": final_status,
                    "message": message,
                    "endedAt": now_iso(),
                    "exitCode": exit_code,
                    "modelResult": model_result,
                    "previewUrl": preview_url,
                    "output": payload,
                },
            )
        except Exception as exc:
            current = summary
            try:
                current = self.read(run_id)
            except FileNotFoundError:
                pass
            self._write_summary(
                run_dir,
                {
                    **current,
                    "status": "failed",
                    "message": str(exc),
                    "error": str(exc),
                    "endedAt": now_iso(),
                },
            )
        finally:
            with self._lock:
                self._processes.pop(run_id, None)
                self._threads.pop(run_id, None)

    def _build_env(
        self,
        *,
        script: dict[str, object],
        upload_id: str,
        task_name: str,
        task_dir: Path,
        results_dir: Path,
        run_dir: Path,
        outputs_dir: Path,
        output_json_path: Path,
        stdout_path: Path,
        action_id: object,
        action_name: object,
    ) -> dict[str, str]:
        dataset_dir = task_dir / "dataset"
        return {
            **os.environ,
            "WORLDGS_UPLOAD_ID": upload_id,
            "WORLDGS_TASK_NAME": task_name,
            "WORLDGS_TASK_DIR": str(task_dir),
            "WORLDGS_DATASET_DIR": str(dataset_dir),
            "WORLDGS_IMAGES_DIR": str(dataset_dir / "images"),
            "WORLDGS_SCENE_DATASET_DIR": str(dataset_dir / "sceneDataset"),
            "WORLDGS_RESULTS_DIR": str(results_dir),
            "WORLDGS_RUN_DIR": str(run_dir),
            "WORLDGS_RUN_OUTPUT_DIR": str(outputs_dir),
            "WORLDGS_RUN_LOG_PATH": str(stdout_path),
            "WORLDGS_OUTPUT_JSON": str(output_json_path),
            "WORLDGS_SCRIPT_NAME": str(script["name"]),
            "WORLDGS_SCRIPT_TYPE": str(script["scriptType"]),
            "WORLDGS_SCRIPT_ACTION_ID": str(action_id or ""),
            "WORLDGS_SCRIPT_ACTION_NAME": str(action_name or ""),
            "WORLDGS_RECEIVER_VERSION": "0.1.0",
        }

    def _command_for_script(self, entry_path: Path) -> list[str]:
        suffix = entry_path.suffix.lower()
        if suffix == ".py":
            return [sys.executable, str(entry_path)]
        return [str(entry_path)]

    def _command_for_run(self, *, script: dict[str, object], action_id: Optional[str]) -> tuple[str, list[str]]:
        if not action_id:
            return str(script["name"]), self._command_for_script(Path(str(script["entryFile"])))
        action = self._find_action(script, action_id)
        script_dir = self.registry.get_script_dir(str(script["scriptId"]))
        command = self._command_for_action(script_dir, str(action.get("command") or ""))
        action_name = str(action.get("name") or "").strip() or str(script["name"])
        return f"{script['name']} · {action_name}", command

    def _find_action(self, script: dict[str, object], action_id: str) -> dict[str, object]:
        for action in script.get("customActions") or []:
            if str(action.get("actionId") or "") == action_id:
                return action
        raise ValueError(f"script action not found: {action_id}")

    def _command_for_action(self, script_dir: Path, command_text: str) -> list[str]:
        try:
            parts = shlex.split(command_text)
        except ValueError as exc:
            raise ValueError(f"invalid script action command: {exc}") from exc
        if not parts:
            raise ValueError("script action command is required")
        entry_path = (script_dir / parts[0]).resolve()
        if not entry_path.is_file():
            raise FileNotFoundError(f"script action entry not found: {parts[0]}")
        if not _is_relative_to(entry_path, script_dir.resolve()):
            raise ValueError("script action entry must stay inside script directory")
        return [*self._command_for_script(entry_path), *parts[1:]]

    def _read_output_json(self, output_json_path: Path) -> dict[str, object]:
        if not output_json_path.is_file():
            return {}
        try:
            payload = json.loads(output_json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid output json: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("invalid output json: root must be object")
        return payload

    def _resolve_model_path(self, value: object, *, run_dir: Path, task_dir: Path) -> Optional[Path]:
        if not value:
            return None
        raw = str(value).strip()
        path = Path(raw)
        if not path.is_absolute():
            path = (run_dir / raw).resolve()
        else:
            path = path.resolve()
        if not path.is_file():
            raise FileNotFoundError(f"model result not found: {path}")
        if not (_is_relative_to(path, run_dir) or _is_relative_to(path, task_dir)):
            raise ValueError("model path must stay inside task directory or script run directory")
        return path

    def _publish_model_result(self, results_dir: Path, run_id: str, model_path: Path) -> dict[str, object]:
        extension = model_path.suffix.lower().lstrip(".")
        if extension not in {"ply", "sog"}:
            raise ValueError("model result must use .ply or .sog")
        results_dir.mkdir(parents=True, exist_ok=True)
        target = results_dir / f"mobile.{extension}"
        shutil.copyfile(model_path, target)
        payload = {
            "fileName": target.name,
            "fileExt": extension,
            "sizeBytes": target.stat().st_size,
            "path": str(target),
            "uploadedAt": time.time(),
            "source": "user-script",
            "scriptRunId": run_id,
            "generatedAt": now_iso(),
        }
        self._write_json(results_dir / "model_result.json", payload)
        return payload

    def _task_dir_for_upload(self, upload_id: str) -> Path:
        for report_path in self.store.output_dir.glob("*/*/upload_report.json"):
            try:
                payload = json.loads(report_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if payload.get("uploadId") == upload_id:
                return report_path.parent
        raise FileNotFoundError(f"upload not found: {upload_id}")

    def _task_name_for_upload(self, task_dir: Path, upload_id: str) -> str:
        report_path = task_dir / "upload_report.json"
        if report_path.is_file():
            try:
                payload = json.loads(report_path.read_text(encoding="utf-8"))
                task_name = str(payload.get("taskName") or "").strip()
                if task_name:
                    return task_name
            except (OSError, json.JSONDecodeError):
                pass
        return upload_id[:8]

    def _context_for_run(
        self,
        *,
        script: dict[str, object],
        upload_id: str,
        action_id: Optional[str],
    ) -> tuple[Path, str, Path, Path]:
        if upload_id:
            task_dir = self._task_dir_for_upload(upload_id)
            return task_dir, self._task_name_for_upload(task_dir, upload_id), task_dir / "script_runs", task_dir / "results"
        script_dir = self.registry.get_script_dir(str(script["scriptId"]))
        runtime_dir = script_dir / ".runtime"
        task_dir = runtime_dir / "global_task"
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "dataset" / "images").mkdir(parents=True, exist_ok=True)
        (task_dir / "dataset" / "sceneDataset").mkdir(parents=True, exist_ok=True)
        action = self._find_action(script, action_id or "")
        action_name = str(action.get("name") or "").strip() or "自定义动作"
        return task_dir, f"{script['name']} · {action_name}", runtime_dir / "global_runs", runtime_dir / "results"

    def _summary_candidates(self, run_id: str):
        yield from self.store.output_dir.glob(f"*/*/script_runs/{run_id}/summary.json")
        yield from self.store.output_dir.glob(f"scripts/*/.runtime/global_runs/{run_id}/summary.json")

    def _latest_global_for_script(self, script_id: str, *, active_only: bool = False) -> Optional[dict[str, object]]:
        latest: Optional[dict[str, object]] = None
        for summary_path in self.store.output_dir.glob(f"scripts/{script_id}/.runtime/global_runs/*/summary.json"):
            try:
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if active_only and summary.get("status") not in ACTIVE_SCRIPT_RUN_STATUSES:
                continue
            if latest is None or str(summary.get("startedAt") or "") > str(latest.get("startedAt") or ""):
                latest = summary
        return latest

    def _write_summary(self, run_dir: Path, payload: dict[str, object]) -> None:
        self._write_json(run_dir / "summary.json", payload)

    def _write_json(self, path: Path, payload: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False
