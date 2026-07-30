import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .automation_paths import (
    ensure_inside_output,
    pointcosm_record_dir,
    pointcosm_run_dir,
)


@dataclass(frozen=True)
class AutomationStore:
    output_dir: Path


@dataclass(frozen=True)
class RecordSession:
    record_session_id: str
    record_dir: Path


@dataclass(frozen=True)
class RunSummary:
    automation_run_id: str
    run_dir: Path
    status: str


@dataclass(frozen=True)
class UploadForAutomation:
    upload_id: str
    task_name: str
    package_path: Path
    extracted_path: Path
    open_path: Path
    size_bytes: int


def create_record_session(store: AutomationStore, base_url: str) -> RecordSession:
    record_session_id = uuid.uuid4().hex
    record_dir = pointcosm_record_dir(store.output_dir, record_session_id)
    (record_dir / "screenshots").mkdir(parents=True, exist_ok=True)
    (record_dir / "dom_snapshots").mkdir(parents=True, exist_ok=True)
    (record_dir / "network_events.jsonl").write_text("", encoding="utf-8")
    payload = {
        "recordSessionId": record_session_id,
        "platform": "pointcosm",
        "baseUrl": base_url,
        "startedAt": _now_iso(),
        "endedAt": None,
        "profileDir": str(store.output_dir / "automations" / "pointcosm" / "profile"),
        "events": [],
    }
    _write_json(record_dir / "record_session.json", payload)
    return RecordSession(record_session_id=record_session_id, record_dir=record_dir)


def stop_record_session(store: AutomationStore, record_session_id: str) -> RecordSession:
    record_dir = pointcosm_record_dir(store.output_dir, record_session_id)
    record_file = record_dir / "record_session.json"
    if not record_file.is_file():
        raise FileNotFoundError(f"record session not found: {record_session_id}")
    payload = json.loads(record_file.read_text(encoding="utf-8"))
    payload["endedAt"] = _now_iso()
    _write_json(record_file, payload)
    return RecordSession(record_session_id=record_session_id, record_dir=record_dir)


def create_run_summary(
    store: AutomationStore,
    upload_id: str,
    task_name: str,
    package_path: Path,
    extracted_path: Path,
) -> RunSummary:
    automation_run_id = uuid.uuid4().hex
    run_dir = pointcosm_run_dir(store.output_dir, automation_run_id)
    (run_dir / "screenshots").mkdir(parents=True, exist_ok=True)
    (run_dir / "dom_snapshots").mkdir(parents=True, exist_ok=True)
    summary = {
        "automationRunId": automation_run_id,
        "uploadId": upload_id,
        "taskName": task_name,
        "status": "running",
        "currentStepId": None,
        "packagePath": str(package_path),
        "extractedPath": str(extracted_path),
        "pointcosmUrl": None,
        "startedAt": _now_iso(),
        "endedAt": None,
        "error": None,
        "message": None,
        "latestScreenshot": None,
    }
    _write_json(run_dir / "summary.json", summary)
    (run_dir / "run_log.jsonl").write_text("", encoding="utf-8")
    return RunSummary(automation_run_id=automation_run_id, run_dir=run_dir, status="running")


def create_platform_run_summary(
    store: AutomationStore,
    context: Any,
) -> RunSummary:
    automation_run_id = uuid.uuid4().hex
    run_dir = pointcosm_run_dir(store.output_dir, automation_run_id)
    (run_dir / "screenshots").mkdir(parents=True, exist_ok=True)
    (run_dir / "dom_snapshots").mkdir(parents=True, exist_ok=True)
    summary = {
        "automationRunId": automation_run_id,
        "uploadId": context.upload_id,
        "taskName": context.task_name,
        "status": "running",
        "currentStepId": None,
        "platformId": context.platform_id,
        "platformName": context.platform_name,
        "entryUrl": context.entry_url,
        "datasetPath": str(context.dataset_path),
        "imagesDir": str(context.images_dir),
        "imageCount": context.image_count,
        "pointcosmUrl": None,
        "startedAt": _now_iso(),
        "endedAt": None,
        "error": None,
        "message": None,
        "latestScreenshot": None,
    }
    _write_json(run_dir / "summary.json", summary)
    (run_dir / "run_log.jsonl").write_text("", encoding="utf-8")
    return RunSummary(automation_run_id=automation_run_id, run_dir=run_dir, status="running")


def update_run_summary(store: AutomationStore, automation_run_id: str, **fields: object) -> None:
    run_dir = pointcosm_run_dir(store.output_dir, automation_run_id)
    summary_file = run_dir / "summary.json"
    if not summary_file.is_file():
        raise FileNotFoundError(f"automation run not found: {automation_run_id}")
    payload = json.loads(summary_file.read_text(encoding="utf-8"))
    payload.update(fields)
    if fields.get("status") in {"succeeded", "failed", "cancelled"} and not payload.get("endedAt"):
        payload["endedAt"] = _now_iso()
    _write_json(summary_file, payload)


def append_run_log(
    store: AutomationStore,
    automation_run_id: str,
    event: str,
    payload: dict[str, object],
) -> None:
    run_dir = pointcosm_run_dir(store.output_dir, automation_run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    line = {
        "occurredAt": _now_iso(),
        "event": event,
        "payload": payload,
    }
    with (run_dir / "run_log.jsonl").open("a", encoding="utf-8") as file:
        file.write(json.dumps(line, ensure_ascii=False) + "\n")


def find_upload_by_id(
    output_dir: Path,
    uploads: dict[str, dict[str, object]],
    upload_id: str,
) -> UploadForAutomation:
    if not upload_id:
        raise FileNotFoundError("upload not found: empty uploadId")

    memory_upload = uploads.get(upload_id)
    if memory_upload:
        return _upload_from_payload(output_dir, memory_upload)

    for report_path in output_dir.rglob("upload_report.json"):
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if payload.get("uploadId") == upload_id:
            return _upload_from_payload(output_dir, payload, fallback_open_path=report_path.parent)

    raise FileNotFoundError(f"upload not found: {upload_id}")


def read_run_summary(store: AutomationStore, automation_run_id: str) -> dict[str, object]:
    summary_file = pointcosm_run_dir(store.output_dir, automation_run_id) / "summary.json"
    if not summary_file.is_file():
        raise FileNotFoundError(f"automation run not found: {automation_run_id}")
    return json.loads(summary_file.read_text(encoding="utf-8"))


def _upload_from_payload(
    output_dir: Path,
    payload: dict[str, object],
    fallback_open_path: Optional[Path] = None,
) -> UploadForAutomation:
    upload_id = str(payload.get("uploadId") or "")
    package_path = ensure_inside_output(output_dir, Path(str(payload.get("savePath") or payload.get("packagePath"))))
    extracted_path = ensure_inside_output(output_dir, Path(str(payload.get("extractedPath"))))
    open_path_value = payload.get("openPath")
    open_path = ensure_inside_output(
        output_dir,
        Path(str(open_path_value)) if open_path_value else (fallback_open_path or package_path.parent),
    )
    return UploadForAutomation(
        upload_id=upload_id,
        task_name=str(payload.get("taskName") or open_path.name),
        package_path=package_path,
        extracted_path=extracted_path,
        open_path=open_path,
        size_bytes=int(payload.get("sizeBytes") or 0),
    )


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
