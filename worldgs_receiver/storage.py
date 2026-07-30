import hashlib
import json
import re
import shutil
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional


MAX_ZIP_MEMBERS = 5000
MAX_ZIP_MEMBER_BYTES = 512 * 1024 * 1024
MAX_ZIP_UNCOMPRESSED_BYTES = 20 * 1024 * 1024 * 1024


@dataclass(frozen=True)
class SavePackageResult:
    upload_id: str
    package_path: Path
    extracted_dir: Path
    report_path: Path
    sha256: str
    size_bytes: int


def save_package(
    output_dir: Path,
    filename: str,
    content: bytes,
    expected_sha256: Optional[str],
    device_name: str,
) -> SavePackageResult:
    sha256 = hashlib.sha256(content).hexdigest()
    if expected_sha256 and expected_sha256.lower() != sha256:
        raise ValueError("sha256 mismatch")
    temp_dir = output_dir / ".tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_path = temp_dir / f"{uuid.uuid4().hex}.zip"
    try:
        temp_path.write_bytes(content)
        return save_package_file(
            output_dir=output_dir,
            filename=filename,
            source_path=temp_path,
            sha256=sha256,
            size_bytes=len(content),
            device_name=device_name,
        )
    finally:
        temp_path.unlink(missing_ok=True)


def save_package_file(
    output_dir: Path,
    filename: str,
    source_path: Path,
    sha256: str,
    size_bytes: int,
    device_name: str,
) -> SavePackageResult:
    upload_id = uuid.uuid4().hex
    safe_name = _safe_name(Path(filename).stem or "worldgs_package")
    day_dir = output_dir / datetime.now().strftime("%Y-%m-%d")
    task_dir = day_dir / f"{safe_name}_{upload_id[:8]}"
    extracted_dir = task_dir / "extracted"
    package_path = task_dir / "package.zip"
    report_path = task_dir / "upload_report.json"

    extracted_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source_path, package_path)

    ok = True
    error: Optional[str] = None
    try:
        with zipfile.ZipFile(package_path) as zip_file:
            _validate_and_extract_zip(zip_file, extracted_dir)
    except Exception as exc:
        ok = False
        error = str(exc)
        _write_report(
            report_path=report_path,
            ok=ok,
            upload_id=upload_id,
            sha256=sha256,
            size_bytes=size_bytes,
            package_path=package_path,
            extracted_dir=extracted_dir,
            device_name=device_name,
            error=error,
        )
        raise

    _write_report(
        report_path=report_path,
        ok=ok,
        upload_id=upload_id,
        sha256=sha256,
        size_bytes=size_bytes,
        package_path=package_path,
        extracted_dir=extracted_dir,
        device_name=device_name,
        error=error,
    )
    return SavePackageResult(
        upload_id=upload_id,
        package_path=package_path,
        extracted_dir=extracted_dir,
        report_path=report_path,
        sha256=sha256,
        size_bytes=size_bytes,
    )


def _validate_and_extract_zip(zip_file: zipfile.ZipFile, extracted_dir: Path) -> None:
    infos = zip_file.infolist()
    if len(infos) > MAX_ZIP_MEMBERS:
        raise ValueError("too many zip members")
    names = {info.filename for info in infos}
    if "worldgs_task/manifest.json" not in names:
        raise ValueError("missing worldgs_task/manifest.json")
    total_uncompressed = 0
    extracted_root = extracted_dir.resolve()
    for info in infos:
        _validate_zip_member(info, extracted_root, extracted_dir)
        if info.is_dir():
            continue
        if info.file_size > MAX_ZIP_MEMBER_BYTES:
            raise ValueError("zip member too large")
        total_uncompressed += info.file_size
        if total_uncompressed > MAX_ZIP_UNCOMPRESSED_BYTES:
            raise ValueError("zip uncompressed size too large")

    for info in infos:
        target = (extracted_dir / info.filename).resolve()
        if info.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with zip_file.open(info) as source, target.open("wb") as output:
            shutil.copyfileobj(source, output, length=1024 * 1024)


def _validate_zip_member(info: zipfile.ZipInfo, extracted_root: Path, extracted_dir: Path) -> None:
    if "\\" in info.filename:
        raise ValueError("invalid zip member path")
    name = info.filename
    parts = Path(name).parts
    if not name or name.startswith("/") or ".." in parts:
        raise ValueError("invalid zip member path")
    target = (extracted_dir / name).resolve()
    try:
        target.relative_to(extracted_root)
    except ValueError as exc:
        raise ValueError("invalid zip member path") from exc


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return cleaned or "worldgs_package"


def _write_report(
    report_path: Path,
    ok: bool,
    upload_id: str,
    sha256: str,
    size_bytes: int,
    package_path: Path,
    extracted_dir: Path,
    device_name: str,
    error: Optional[str],
) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "ok": ok,
        "uploadId": upload_id,
        "sha256": sha256,
        "sizeBytes": size_bytes,
        "packagePath": str(package_path),
        "extractedPath": str(extracted_dir),
        "deviceName": device_name,
        "error": error,
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
