import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


REQUIRED_DATASET_FILES = (
    "sceneDataset/cameras.txt",
    "sceneDataset/images.txt",
    "sceneDataset/points3D.txt",
)


@dataclass(frozen=True)
class LocalTrainingConfig:
    enabled: bool = False
    command: list[str] = field(default_factory=list)
    cwd: Optional[Path] = None
    env: dict[str, str] = field(default_factory=dict)
    latest_ply_name: str = "mobile.ply"


@dataclass(frozen=True)
class LocalTrainingStore:
    output_dir: Path


class LocalTrainingRunner:
    def __init__(self, store: LocalTrainingStore, config: LocalTrainingConfig) -> None:
        self.store = store
        self.config = config
        self._threads: dict[str, threading.Thread] = {}
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._lock = threading.Lock()

    def start(self, upload_id: str, preset: str = "fast", wait: bool = False) -> dict[str, object]:
        if not self.config.enabled:
            raise RuntimeError("本地高斯训练未配置")
        if not self.config.command:
            raise RuntimeError("本地高斯训练命令为空")
        task_dir = self._task_dir_for_upload(upload_id)
        dataset_dir = task_dir / "dataset"
        self._validate_dataset(dataset_dir)
        active = self.latest_for_upload(upload_id, active_only=True)
        if active:
            return active
        run_id = uuid.uuid4().hex
        run_dir = task_dir / "local_training" / run_id
        outputs_dir = run_dir / "outputs"
        outputs_dir.mkdir(parents=True, exist_ok=True)
        command = self._render_command(
            upload_id=upload_id,
            preset=preset,
            task_dir=task_dir,
            dataset_dir=dataset_dir,
            run_dir=run_dir,
            outputs_dir=outputs_dir,
        )
        summary = {
            "trainingRunId": run_id,
            "uploadId": upload_id,
            "preset": preset,
            "status": "queued",
            "progressPercent": 0,
            "currentStep": "queued",
            "message": "本地高斯训练已排队",
            "taskDir": str(task_dir),
            "datasetDir": str(dataset_dir),
            "runDir": str(run_dir),
            "outputDir": str(outputs_dir),
            "command": command,
            "startedAt": _now_iso(),
            "endedAt": None,
            "modelResult": None,
        }
        self._write_summary(run_dir, summary)
        self._write_json(run_dir / "command.json", {"command": command, "cwd": str(self.config.cwd or "")})
        if wait:
            self._run(run_id, run_dir, task_dir, outputs_dir, command)
        else:
            thread = threading.Thread(
                target=self._run,
                args=(run_id, run_dir, task_dir, outputs_dir, command),
                daemon=True,
                name=f"local-gsplat-{run_id[:8]}",
            )
            with self._lock:
                self._threads[run_id] = thread
            thread.start()
        return self.read(run_id)

    def read(self, run_id: str) -> dict[str, object]:
        for summary_path in self.store.output_dir.glob(f"*/*/local_training/{run_id}/summary.json"):
            return json.loads(summary_path.read_text(encoding="utf-8"))
        raise FileNotFoundError(f"local training run not found: {run_id}")

    def cancel(self, run_id: str) -> dict[str, object]:
        with self._lock:
            process = self._processes.get(run_id)
        if process and process.poll() is None:
            process.terminate()
        summary = self.read(run_id)
        run_dir = Path(str(summary["runDir"]))
        self._write_summary(
            run_dir,
            {**summary, "status": "cancelled", "message": "用户取消本地高斯训练", "endedAt": _now_iso()},
        )
        return self.read(run_id)

    def latest_for_upload(self, upload_id: str, active_only: bool = False) -> Optional[dict[str, object]]:
        latest: Optional[dict[str, object]] = None
        for summary_path in self.store.output_dir.glob("*/*/local_training/*/summary.json"):
            try:
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if summary.get("uploadId") != upload_id:
                continue
            if active_only and summary.get("status") not in {"queued", "running"}:
                continue
            if latest is None or str(summary.get("startedAt") or "") > str(latest.get("startedAt") or ""):
                latest = summary
        return latest

    def _run(self, run_id: str, run_dir: Path, task_dir: Path, outputs_dir: Path, command: list[str]) -> None:
        log_path = run_dir / "run_log.txt"
        summary = self.read(run_id)
        self._write_summary(
            run_dir,
            {
                **summary,
                "status": "running",
                "progressPercent": 1,
                "currentStep": "training",
                "message": "本地高斯训练运行中",
            },
        )
        self._write_heartbeat(run_dir, "running")
        env = os.environ.copy()
        env.update(self.config.env)
        try:
            with log_path.open("w", encoding="utf-8") as log_file:
                process = subprocess.Popen(
                    command,
                    cwd=str(self.config.cwd) if self.config.cwd else None,
                    env=env,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                with self._lock:
                    self._processes[run_id] = process
                return_code = process.wait()
            if self.read(run_id).get("status") == "cancelled":
                self._write_heartbeat(run_dir, "cancelled")
                return
            if return_code != 0:
                self._write_summary(
                    run_dir,
                    {
                        **self.read(run_id),
                        "status": "failed",
                        "message": f"本地高斯训练失败，退出码 {return_code}",
                        "endedAt": _now_iso(),
                    },
                )
                self._write_heartbeat(run_dir, "failed")
                return
            ply_path = outputs_dir / self.config.latest_ply_name
            if not ply_path.is_file():
                candidates = sorted(outputs_dir.rglob("*.ply"), key=lambda item: item.stat().st_mtime, reverse=True)
                ply_path = candidates[0] if candidates else ply_path
            if not ply_path.is_file():
                self._write_summary(
                    run_dir,
                    {
                        **self.read(run_id),
                        "status": "failed",
                        "message": "本地高斯训练完成但没有找到 PLY 产物",
                        "endedAt": _now_iso(),
                    },
                )
                self._write_heartbeat(run_dir, "failed")
                return
            model_result = self._publish_model_result(task_dir, run_id, ply_path)
            self._write_summary(
                run_dir,
                {
                    **self.read(run_id),
                    "status": "succeeded",
                    "progressPercent": 100,
                    "currentStep": "completed",
                    "message": "本地高斯训练完成",
                    "endedAt": _now_iso(),
                    "modelResult": model_result,
                },
            )
            self._write_heartbeat(run_dir, "succeeded")
        except Exception as exc:
            current = summary
            try:
                current = self.read(run_id)
            except FileNotFoundError:
                pass
            self._write_summary(
                run_dir,
                {**current, "status": "failed", "message": str(exc), "error": str(exc), "endedAt": _now_iso()},
            )
            self._write_heartbeat(run_dir, "failed")
        finally:
            with self._lock:
                self._processes.pop(run_id, None)
                self._threads.pop(run_id, None)

    def _publish_model_result(self, task_dir: Path, run_id: str, ply_path: Path) -> dict[str, object]:
        results_dir = task_dir / "results"
        results_dir.mkdir(parents=True, exist_ok=True)
        target = results_dir / "mobile.ply"
        shutil.copyfile(ply_path, target)
        payload = {
            "fileName": target.name,
            "fileExt": "ply",
            "sizeBytes": target.stat().st_size,
            "path": str(target),
            "uploadedAt": time.time(),
            "source": "local-gsplat",
            "trainingRunId": run_id,
            "generatedAt": _now_iso(),
        }
        self._write_json(results_dir / "model_result.json", payload)
        return payload

    def _render_command(
        self,
        upload_id: str,
        preset: str,
        task_dir: Path,
        dataset_dir: Path,
        run_dir: Path,
        outputs_dir: Path,
    ) -> list[str]:
        values = {
            "upload_id": upload_id,
            "preset": preset,
            "task_dir": str(task_dir),
            "dataset_dir": str(dataset_dir),
            "run_dir": str(run_dir),
            "run_output_dir": str(outputs_dir),
        }
        return [part.format(**values) for part in self.config.command]

    def _validate_dataset(self, dataset_dir: Path) -> None:
        if not any((dataset_dir / "images").glob("*")):
            raise ValueError("missing required file: images/*")
        for relative in REQUIRED_DATASET_FILES:
            if not (dataset_dir / relative).is_file():
                raise ValueError(f"missing required file: {relative}")

    def _task_dir_for_upload(self, upload_id: str) -> Path:
        for report_path in self.store.output_dir.glob("*/*/upload_report.json"):
            try:
                payload = json.loads(report_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if payload.get("uploadId") == upload_id:
                return report_path.parent
        raise FileNotFoundError(f"upload not found: {upload_id}")

    def _write_summary(self, run_dir: Path, payload: dict[str, object]) -> None:
        self._write_json(run_dir / "summary.json", payload)

    def _write_heartbeat(self, run_dir: Path, status: str) -> None:
        self._write_json(run_dir / "heartbeat.json", {"status": status, "updatedAt": _now_iso()})

    def _write_json(self, path: Path, payload: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
