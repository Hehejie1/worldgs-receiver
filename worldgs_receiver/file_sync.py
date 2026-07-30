import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import UploadFile


REQUIRED_COLMAP_FILES = {
    "sceneDataset/cameras.txt",
    "sceneDataset/images.txt",
    "sceneDataset/points3D.txt",
}

OPTIONAL_REPORT_FILES = {
    "reports/sfm_quality_report.json",
    "reports/training_quality_report.json",
}


@dataclass(frozen=True)
class FileSyncLimits:
    max_file_bytes: int
    max_total_bytes: int
    max_files: int


@dataclass(frozen=True)
class FileSyncSession:
    session_id: str
    session_token: str
    task_dir: Path
    dataset_dir: Path
    manifest_path: Path
    report_path: Path


class FileSyncStore:
    def __init__(self, output_dir: Path, limits: FileSyncLimits) -> None:
        self.output_dir = output_dir
        self.limits = limits

    def create_session(
        self,
        job_id: str,
        task_name: str,
        files: list[dict[str, Any]],
    ) -> FileSyncSession:
        if len(files) > self.limits.max_files:
            raise ValueError("too many files")
        total_size = 0
        normalized_files: dict[str, dict[str, Any]] = {}
        for item in files:
            relative_path = validate_relative_path(str(item.get("path", "")))
            size_bytes = int(item.get("sizeBytes") or 0)
            if size_bytes < 0:
                raise ValueError("invalid file size")
            if size_bytes > self.limits.max_file_bytes:
                raise ValueError("file too large")
            total_size += size_bytes
            normalized_files[relative_path] = {
                "path": relative_path,
                "sizeBytes": size_bytes,
                "sha256": str(item.get("sha256") or "").lower(),
                "status": "pending",
                "shouldUpload": True,
            }
        if total_size > self.limits.max_total_bytes:
            raise ValueError("sync payload too large")

        session_id = uuid.uuid4().hex
        session_token = uuid.uuid4().hex
        safe_name = _safe_name(task_name or job_id or "worldgs_dataset")
        task_dir = self.output_dir / datetime.now().strftime("%Y-%m-%d") / f"{safe_name}_{session_id[:8]}"
        dataset_dir = task_dir / "dataset"
        manifest_path = task_dir / "sync_manifest.json"
        report_path = task_dir / "upload_report.json"
        (dataset_dir / "images").mkdir(parents=True, exist_ok=True)
        (dataset_dir / "sceneDataset").mkdir(parents=True, exist_ok=True)
        (task_dir / ".partial").mkdir(parents=True, exist_ok=True)

        payload = {
            "schemaVersion": 2,
            "sessionId": session_id,
            "sessionToken": session_token,
            "jobId": job_id,
            "taskName": task_name,
            "createdAt": datetime.utcnow().isoformat() + "Z",
            "datasetPath": str(dataset_dir),
            "files": normalized_files,
            "status": "receiving",
        }
        _write_json(manifest_path, payload)
        return FileSyncSession(
            session_id=session_id,
            session_token=session_token,
            task_dir=task_dir,
            dataset_dir=dataset_dir,
            manifest_path=manifest_path,
            report_path=report_path,
        )

    def read_status(self, session_id: str, session_token: str) -> dict[str, Any]:
        manifest_path = self._manifest_path(session_id)
        payload = _read_json(manifest_path)
        _verify_session_token(payload, session_token)
        return _public_status(payload)

    async def save_file(
        self,
        session_id: str,
        session_token: str,
        relative_path: str,
        expected_sha256: str,
        expected_size_bytes: int,
        file: UploadFile,
    ) -> dict[str, Any]:
        relative_path = validate_relative_path(relative_path)
        if expected_size_bytes > self.limits.max_file_bytes:
            raise ValueError("file too large")

        manifest_path = self._manifest_path(session_id)
        payload = _read_json(manifest_path)
        _verify_session_token(payload, session_token)

        task_dir = manifest_path.parent
        dataset_dir = Path(str(payload["datasetPath"]))
        target = (dataset_dir / relative_path).resolve()
        if not _is_relative_to(target, dataset_dir.resolve()):
            raise ValueError("path outside dataset")
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = task_dir / ".partial" / f"{relative_path.replace('/', '__')}.{uuid.uuid4().hex}.part"

        digest = hashlib.sha256()
        written = 0
        with partial.open("wb") as output:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > expected_size_bytes or written > self.limits.max_file_bytes:
                    output.close()
                    partial.unlink(missing_ok=True)
                    raise ValueError("file too large")
                digest.update(chunk)
                output.write(chunk)

        actual_sha256 = digest.hexdigest()
        if written != expected_size_bytes:
            partial.unlink(missing_ok=True)
            raise ValueError("file size mismatch")
        if expected_sha256 and actual_sha256 != expected_sha256.lower():
            partial.unlink(missing_ok=True)
            raise ValueError("sha256 mismatch")

        partial.replace(target)
        files = dict(payload.get("files") or {})
        files[relative_path] = {
            "path": relative_path,
            "sizeBytes": written,
            "sha256": actual_sha256,
            "status": "completed",
            "shouldUpload": False,
        }
        payload["files"] = files
        _write_json(manifest_path, payload)
        return files[relative_path]

    def finalize(self, session_id: str, session_token: str) -> dict[str, Any]:
        manifest_path = self._manifest_path(session_id)
        payload = _read_json(manifest_path)
        _verify_session_token(payload, session_token)
        files = dict(payload.get("files") or {})
        completed_files = {
            path: item for path, item in files.items() if item.get("status") == "completed"
        }
        if not completed_files:
            raise ValueError("no files completed")

        task_dir = manifest_path.parent
        dataset_dir = Path(str(payload["datasetPath"]))
        for path in completed_files:
            if not (dataset_dir / path).is_file():
                raise ValueError(f"missing synced file on disk: {path}")

        payload["status"] = "completed"
        payload["completedAt"] = datetime.utcnow().isoformat() + "Z"
        _write_json(manifest_path, payload)

        total_size = sum(int(item.get("sizeBytes") or 0) for item in completed_files.values())
        image_count = _count_dataset_images(dataset_dir)
        report = {
            "ok": True,
            "schemaVersion": 2,
            "uploadId": session_id,
            "sessionId": session_id,
            "taskName": str(payload.get("taskName") or task_dir.name),
            "sha256": "",
            "sizeBytes": total_size,
            "fileCount": len(completed_files),
            "imageCount": image_count,
            "packagePath": str(dataset_dir),
            "extractedPath": str(dataset_dir),
            "datasetPath": str(dataset_dir),
            "openPath": str(task_dir),
            "deviceName": "android",
            "error": None,
        }
        _write_json(task_dir / "upload_report.json", report)
        return report

    def _manifest_path(self, session_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{32}", session_id):
            raise FileNotFoundError("sync session not found")
        matches = list(self.output_dir.glob(f"*/*_{session_id[:8]}/sync_manifest.json"))
        for path in matches:
            try:
                payload = _read_json(path)
            except (OSError, json.JSONDecodeError):
                continue
            if payload.get("sessionId") == session_id:
                return path
        raise FileNotFoundError("sync session not found")


def validate_relative_path(relative_path: str) -> str:
    value = relative_path.strip().replace("\\", "/")
    if not value or value.startswith("/") or ".." in value.split("/"):
        raise ValueError(_invalid_path_message(value))
    if value in REQUIRED_COLMAP_FILES:
        return value
    if value in OPTIONAL_REPORT_FILES:
        return value
    if value == "output/result.ply":
        return value
    if value.startswith("reports/quality/") and value.endswith(".png"):
        filename = value.removeprefix("reports/quality/")
        if "/" in filename or not re.fullmatch(r"[A-Za-z0-9_.-]+", filename):
            raise ValueError(_invalid_path_message(value))
        return value
    if value.startswith("images/"):
        filename = value.removeprefix("images/")
        if "/" in filename or not re.fullmatch(r"[A-Za-z0-9_.-]+", filename):
            raise ValueError(_invalid_path_message(value))
        return value
    raise ValueError(_invalid_path_message(value))


def _invalid_path_message(relative_path: str) -> str:
    if not relative_path:
        return "invalid sync file path"
    return f"invalid sync file path: {relative_path}"


def _public_status(payload: dict[str, Any]) -> dict[str, Any]:
    files = {}
    for path, item in dict(payload.get("files") or {}).items():
        files[path] = {
            "path": path,
            "sizeBytes": int(item.get("sizeBytes") or 0),
            "sha256": str(item.get("sha256") or ""),
            "status": str(item.get("status") or "pending"),
            "shouldUpload": bool(item.get("shouldUpload", True)),
        }
    return {
        "sessionId": str(payload.get("sessionId") or ""),
        "status": str(payload.get("status") or "receiving"),
        "datasetPath": str(payload.get("datasetPath") or ""),
        "files": files,
    }


def _verify_session_token(payload: dict[str, Any], session_token: str) -> None:
    if str(payload.get("sessionToken") or "") != session_token:
        raise PermissionError("invalid sync session token")


def _count_dataset_images(dataset_dir: Path) -> int:
    images_dir = dataset_dir / "images"
    if not images_dir.is_dir():
        return 0
    return sum(1 for path in images_dir.iterdir() if path.is_file() and _is_image_file(path))


def _is_image_file(path: Path) -> bool:
    return not path.name.startswith(".") and path.suffix.lower() in {
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".heic",
        ".heif",
    }


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return cleaned or "worldgs_dataset"


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False
