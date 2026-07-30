import hashlib
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from worldgs_receiver.app import create_app
from worldgs_receiver.config import ReceiverConfig


def test_record_start_and_stop_api(tmp_path: Path) -> None:
    client = TestClient(create_app(ReceiverConfig(output_dir=tmp_path), automation_enabled=False))
    headers = _management_headers(client)

    started = client.post("/api/automation/pointcosm/record/start", headers=headers)
    assert started.status_code == 200
    payload = started.json()
    assert payload["status"] == "recording"
    assert payload["url"] == "https://www.pointcosm.cn/"

    stopped = client.post(
        "/api/automation/pointcosm/record/stop",
        json={"recordSessionId": payload["recordSessionId"]},
        headers=headers,
    )
    assert stopped.status_code == 200
    assert stopped.json()["status"] == "completed"
    assert Path(stopped.json()["recordDir"]).is_dir()


def test_record_stop_writes_expected_artifact_dirs(tmp_path: Path) -> None:
    client = TestClient(create_app(ReceiverConfig(output_dir=tmp_path), automation_enabled=False))
    headers = _management_headers(client)

    started = client.post("/api/automation/pointcosm/record/start", headers=headers).json()
    stopped = client.post(
        "/api/automation/pointcosm/record/stop",
        json={"recordSessionId": started["recordSessionId"]},
        headers=headers,
    ).json()

    record_dir = Path(stopped["recordDir"])
    assert (record_dir / "record_session.json").is_file()
    assert (record_dir / "network_events.jsonl").is_file()
    assert (record_dir / "screenshots").is_dir()
    assert (record_dir / "dom_snapshots").is_dir()


def test_run_api_requires_existing_flow_file(tmp_path: Path) -> None:
    client = TestClient(create_app(ReceiverConfig(output_dir=tmp_path), automation_enabled=False))
    upload_id = _upload_package(client, tmp_path)

    response = client.post(
        "/api/automation/pointcosm/runs",
        json={"uploadId": upload_id},
        headers=_management_headers(client),
    )

    assert response.status_code == 400
    assert "pointcosm_flow.yaml" in response.json()["detail"]


def test_continue_and_cancel_unknown_run_return_404(tmp_path: Path) -> None:
    client = TestClient(create_app(ReceiverConfig(output_dir=tmp_path), automation_enabled=False))
    headers = _management_headers(client)

    assert client.post("/api/automation/pointcosm/runs/missing/continue", headers=headers).status_code == 404
    assert client.post("/api/automation/pointcosm/runs/missing/cancel", headers=headers).status_code == 404


def test_run_status_continue_and_cancel(tmp_path: Path) -> None:
    client = TestClient(create_app(ReceiverConfig(output_dir=tmp_path), automation_enabled=False))
    upload_id = _upload_package(client, tmp_path)
    flow_dir = tmp_path / "automations" / "pointcosm"
    flow_dir.mkdir(parents=True)
    (flow_dir / "pointcosm_flow.yaml").write_text(
        """
platform: pointcosm
baseUrl: https://www.pointcosm.cn/
steps:
  - id: wait_user
    action:
      type: wait_for_user
      prompt: 请人工确认页面
    observe:
      successAny:
        - urlContains: pointcosm.cn
      timeoutSeconds: 1
      onUnknown: pause_for_user
""",
        encoding="utf-8",
    )

    headers = _management_headers(client)
    started = client.post("/api/automation/pointcosm/runs", json={"uploadId": upload_id}, headers=headers)
    assert started.status_code == 200
    run_id = started.json()["automationRunId"]

    status = client.get(f"/api/automation/pointcosm/runs/{run_id}")
    assert status.status_code == 200
    assert status.json()["status"] in {"queued", "running", "paused"}

    continued = client.post(f"/api/automation/pointcosm/runs/{run_id}/continue", headers=headers)
    assert continued.status_code == 200
    assert continued.json()["ok"] is True

    cancelled = client.post(f"/api/automation/pointcosm/runs/{run_id}/cancel", headers=headers)
    assert cancelled.status_code == 200
    assert cancelled.json()["ok"] is True


def _upload_package(client: TestClient, tmp_path: Path) -> str:
    pair = client.get("/pair").json()
    package = tmp_path / "input.zip"
    with zipfile.ZipFile(package, "w") as zip_file:
        zip_file.writestr("worldgs_task/manifest.json", '{"jobId":"job-001","jobName":"room"}')
    content = package.read_bytes()
    response = client.post(
        "/upload",
        data={"token": pair["token"], "sha256": hashlib.sha256(content).hexdigest()},
        files={"file": ("worldgs_job-001.zip", content, "application/zip")},
    )
    assert response.status_code == 200
    return response.json()["uploadId"]


def _management_headers(client: TestClient) -> dict[str, str]:
    nonce = client.get("/api/dashboard").json()["managementNonce"]
    return {"X-WorldGS-Management-Nonce": nonce}
