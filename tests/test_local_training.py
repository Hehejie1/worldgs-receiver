import json
import sys
import time
from pathlib import Path

from fastapi.testclient import TestClient

from worldgs_receiver.app import create_app
from worldgs_receiver.config import ReceiverConfig
from worldgs_receiver.local_training import (
    LocalTrainingConfig,
    LocalTrainingRunner,
    LocalTrainingStore,
)


def make_task(tmp_path: Path) -> tuple[Path, str]:
    upload_id = "a" * 32
    task_dir = tmp_path / "2026-06-29" / f"room_{upload_id[:8]}"
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


def management_headers(client: TestClient) -> dict[str, str]:
    return {"X-WorldGS-Management-Nonce": client.get("/api/dashboard").json()["managementNonce"]}


def test_local_training_rejects_missing_colmap_file(tmp_path: Path) -> None:
    task_dir, upload_id = make_task(tmp_path)
    (task_dir / "dataset" / "sceneDataset" / "points3D.txt").unlink()
    store = LocalTrainingStore(output_dir=tmp_path)
    config = LocalTrainingConfig(enabled=True, command=[sys.executable, "-c", "print('unused')"])

    try:
        LocalTrainingRunner(store, config).start(upload_id)
    except ValueError as exc:
        assert "missing required file: sceneDataset/points3D.txt" in str(exc)
    else:
        raise AssertionError("missing COLMAP file should fail")


def test_local_training_command_receives_dataset_and_output_paths(tmp_path: Path) -> None:
    task_dir, upload_id = make_task(tmp_path)
    script = tmp_path / "fake_train.py"
    script.write_text(
        "import json, pathlib, sys\n"
        "dataset = pathlib.Path(sys.argv[1])\n"
        "out = pathlib.Path(sys.argv[2])\n"
        "out.mkdir(parents=True, exist_ok=True)\n"
        "(out / 'mobile.ply').write_text('ply\\nformat ascii 1.0\\nelement vertex 0\\nend_header\\n')\n"
        "(out / 'seen.json').write_text(json.dumps({'dataset': str(dataset), 'out': str(out)}))\n",
        encoding="utf-8",
    )
    store = LocalTrainingStore(output_dir=tmp_path)
    config = LocalTrainingConfig(
        enabled=True,
        command=[sys.executable, str(script), "{dataset_dir}", "{run_output_dir}"],
    )

    summary = LocalTrainingRunner(store, config).start(upload_id, wait=True)

    assert summary["status"] == "succeeded"
    assert (task_dir / "results" / "mobile.ply").is_file()
    model_result = json.loads((task_dir / "results" / "model_result.json").read_text(encoding="utf-8"))
    assert model_result["source"] == "local-gsplat"
    assert model_result["trainingRunId"] == summary["trainingRunId"]
    seen = json.loads((Path(summary["runDir"]) / "outputs" / "seen.json").read_text(encoding="utf-8"))
    assert seen["dataset"] == str(task_dir / "dataset")


def test_local_training_failed_when_command_produces_no_ply(tmp_path: Path) -> None:
    _, upload_id = make_task(tmp_path)
    store = LocalTrainingStore(output_dir=tmp_path)
    config = LocalTrainingConfig(enabled=True, command=[sys.executable, "-c", "print('no ply')"])

    summary = LocalTrainingRunner(store, config).start(upload_id, wait=True)

    assert summary["status"] == "failed"
    assert "PLY" in summary["message"]


def test_local_training_cancel_keeps_cancelled_state(tmp_path: Path) -> None:
    _, upload_id = make_task(tmp_path)
    store = LocalTrainingStore(output_dir=tmp_path)
    config = LocalTrainingConfig(enabled=True, command=[sys.executable, "-c", "import time; time.sleep(5)"])
    runner = LocalTrainingRunner(store, config)

    summary = runner.start(upload_id)
    run_id = str(summary["trainingRunId"])
    for _ in range(20):
        if runner.read(run_id)["status"] == "running":
            break
        time.sleep(0.05)

    cancelled = runner.cancel(run_id)
    time.sleep(0.3)

    assert cancelled["status"] == "cancelled"
    assert runner.read(run_id)["status"] == "cancelled"


def test_local_training_api_requires_management_nonce(tmp_path: Path) -> None:
    _, upload_id = make_task(tmp_path)
    client = TestClient(create_app(ReceiverConfig(output_dir=tmp_path), automation_enabled=False))

    response = client.post("/api/local-training/runs", json={"uploadId": upload_id})

    assert response.status_code == 403


def test_local_training_api_reports_disabled_when_not_configured(tmp_path: Path) -> None:
    _, upload_id = make_task(tmp_path)
    client = TestClient(create_app(ReceiverConfig(output_dir=tmp_path), automation_enabled=False))

    response = client.post(
        "/api/local-training/runs",
        json={"uploadId": upload_id},
        headers=management_headers(client),
    )

    assert response.status_code == 400
    assert "未配置" in response.json()["detail"]
