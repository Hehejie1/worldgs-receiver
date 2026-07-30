import json
from pathlib import Path

from worldgs_receiver.automation_context import AutomationTaskContext
from worldgs_receiver.automation_store import (
    AutomationStore,
    append_run_log,
    create_record_session,
    create_platform_run_summary,
    create_run_summary,
    find_upload_by_id,
    read_run_summary,
    stop_record_session,
)


def test_create_and_stop_record_session(tmp_path: Path) -> None:
    store = AutomationStore(output_dir=tmp_path)

    record = create_record_session(store, base_url="https://www.pointcosm.cn/")
    stopped = stop_record_session(store, record.record_session_id)

    record_file = stopped.record_dir / "record_session.json"
    payload = json.loads(record_file.read_text(encoding="utf-8"))
    assert payload["recordSessionId"] == record.record_session_id
    assert payload["platform"] == "pointcosm"
    assert payload["endedAt"]
    assert (stopped.record_dir / "screenshots").is_dir()
    assert (stopped.record_dir / "dom_snapshots").is_dir()


def test_create_run_summary_and_append_log(tmp_path: Path) -> None:
    store = AutomationStore(output_dir=tmp_path)
    summary = create_run_summary(
        store,
        upload_id="upload-1",
        task_name="room",
        package_path=tmp_path / "2026-06-23" / "room" / "package.zip",
        extracted_path=tmp_path / "2026-06-23" / "room" / "extracted",
    )

    append_run_log(store, summary.automation_run_id, "step_started", {"stepId": "open_home"})

    summary_file = summary.run_dir / "summary.json"
    log_file = summary.run_dir / "run_log.jsonl"
    assert json.loads(summary_file.read_text(encoding="utf-8"))["status"] == "running"
    assert '"step_started"' in log_file.read_text(encoding="utf-8")


def test_find_upload_by_id_from_history_report(tmp_path: Path) -> None:
    task_dir = tmp_path / "2026-06-23" / "room_abcd1234"
    extracted = task_dir / "extracted"
    extracted.mkdir(parents=True)
    package = task_dir / "package.zip"
    package.write_bytes(b"zip")
    report = task_dir / "upload_report.json"
    report.write_text(
        json.dumps(
            {
                "ok": True,
                "uploadId": "upload-1",
                "packagePath": str(package),
                "extractedPath": str(extracted),
                "sizeBytes": 3,
            }
        ),
        encoding="utf-8",
    )

    upload = find_upload_by_id(tmp_path, {}, "upload-1")

    assert upload.upload_id == "upload-1"
    assert upload.package_path == package
    assert upload.extracted_path == extracted
    assert upload.open_path == task_dir


def test_create_platform_run_summary_persists_platform_and_dataset_fields(tmp_path: Path) -> None:
    store = AutomationStore(output_dir=tmp_path)
    context = AutomationTaskContext(
        upload_id="upload-1",
        task_name="job-1782265913849",
        task_dir=tmp_path / "2026-06-24" / "job",
        dataset_path=tmp_path / "2026-06-24" / "job" / "dataset",
        images_dir=tmp_path / "2026-06-24" / "job" / "dataset" / "images",
        image_count=120,
        platform_id="explorerglobal",
        platform_name="知天下",
        entry_url="https://3d.explorerglobal.cn/compute",
    )

    summary = create_platform_run_summary(store, context)
    payload = read_run_summary(store, summary.automation_run_id)

    assert payload["platformId"] == "explorerglobal"
    assert payload["platformName"] == "知天下"
    assert payload["entryUrl"] == "https://3d.explorerglobal.cn/compute"
    assert payload["taskName"] == "job-1782265913849"
    assert payload["datasetPath"].endswith("/dataset")
    assert payload["imagesDir"].endswith("/dataset/images")
    assert payload["imageCount"] == 120
