import hashlib
from pathlib import Path

from fastapi.testclient import TestClient

from worldgs_receiver.app import create_app
from worldgs_receiver.config import ReceiverConfig


def test_file_sync_uploads_allowed_files_and_skips_completed(tmp_path: Path) -> None:
    client = TestClient(create_app(ReceiverConfig(output_dir=tmp_path), automation_enabled=False))
    token = client.get("/pair").json()["token"]

    created = client.post(
        "/api/sync/sessions",
        json={
            "token": token,
            "jobId": "job-001",
            "taskName": "room",
            "files": [
                _entry("images/frame_000001.jpg", b"image"),
                _entry("sceneDataset/cameras.txt", b"cameras"),
                _entry("sceneDataset/images.txt", b"images"),
                _entry("sceneDataset/points3D.txt", b"points"),
            ],
        },
    )
    assert created.status_code == 200
    session = created.json()

    upload = client.put(
        f"/api/sync/sessions/{session['sessionId']}/files",
        data={
            "sessionToken": session["sessionToken"],
            "relativePath": "images/frame_000001.jpg",
            "sha256": _sha256(b"image"),
            "sizeBytes": "5",
        },
        files={"file": ("frame_000001.jpg", b"image", "image/jpeg")},
    )
    assert upload.status_code == 200
    assert upload.json()["status"] == "completed"

    status = client.get(
        f"/api/sync/sessions/{session['sessionId']}",
        params={"sessionToken": session["sessionToken"]},
    )
    assert status.status_code == 200
    completed = status.json()["files"]["images/frame_000001.jpg"]
    assert completed["status"] == "completed"
    assert completed["sha256"] == _sha256(b"image")
    assert completed["shouldUpload"] is False


def test_file_sync_finalize_reports_image_count_for_dashboard(tmp_path: Path) -> None:
    client = TestClient(create_app(ReceiverConfig(output_dir=tmp_path), automation_enabled=False))
    token = client.get("/pair").json()["token"]
    files = [
        _entry("images/frame_000001.jpg", b"image-1"),
        _entry("images/frame_000002.jpg", b"image-2"),
        _entry("sceneDataset/cameras.txt", b"cameras"),
        _entry("sceneDataset/images.txt", b"images"),
        _entry("sceneDataset/points3D.txt", b"points"),
        _entry(
            "reports/sfm_quality_report.json",
            b'{"schemaVersion":1,"sparsePointCount":2,"visualizations":{"coverageHeatmap":{"path":"quality/coverage_heatmap.png"},"residualPlots":[{"cameraId":1,"path":"quality/camera_residual_1.png","observationCount":4}]}}',
        ),
        _entry("reports/quality/coverage_heatmap.png", b"png"),
        _entry("reports/quality/camera_residual_1.png", b"png"),
    ]
    session = client.post(
        "/api/sync/sessions",
        json={"token": token, "jobId": "job-001", "taskName": "room", "files": files},
    ).json()

    for item in files:
        content = _content_for_entry(item["path"])
        upload = client.put(
            f"/api/sync/sessions/{session['sessionId']}/files",
            data={
                "sessionToken": session["sessionToken"],
                "relativePath": item["path"],
                "sha256": item["sha256"],
                "sizeBytes": str(item["sizeBytes"]),
            },
            files={"file": (Path(str(item["path"])).name, content, "application/octet-stream")},
        )
        assert upload.status_code == 200

    finalized = client.post(
        f"/api/sync/sessions/{session['sessionId']}/finalize",
        json={"sessionToken": session["sessionToken"]},
    )
    assert finalized.status_code == 200
    assert finalized.json()["imageCount"] == 2
    assert finalized.json()["fileCount"] == 8
    task_dir = Path(finalized.json()["openPath"])
    assert (task_dir / "dataset" / "reports" / "sfm_quality_report.json").is_file()
    assert (task_dir / "dataset" / "reports" / "quality" / "coverage_heatmap.png").is_file()
    (task_dir / "dataset" / "images" / "frame_000003.jpg").write_bytes(b"manual-image")
    (task_dir / "dataset" / "images" / ".DS_Store").write_bytes(b"finder")

    dashboard = client.get("/api/dashboard").json()
    task = dashboard["uploads"][0]
    assert task["imageCount"] == 3
    assert task["fileCount"] == 8
    assert task["sfmQualityReport"]["sparsePointCount"] == 2
    assert task["sfmQualityReport"]["visualizations"]["coverageHeatmap"]["url"].endswith("/sfm-quality/coverage_heatmap.png")
    image = client.get(task["sfmQualityReport"]["visualizations"]["coverageHeatmap"]["url"])
    assert image.status_code == 200
    assert task["openPath"].endswith("room_" + session["sessionId"][:8])


def test_file_sync_finalize_rejects_completed_manifest_when_disk_file_missing(tmp_path: Path) -> None:
    client = TestClient(create_app(ReceiverConfig(output_dir=tmp_path), automation_enabled=False))
    token = client.get("/pair").json()["token"]
    files = [
        _entry("images/frame_000001.jpg", b"image-1"),
        _entry("sceneDataset/cameras.txt", b"cameras"),
        _entry("sceneDataset/images.txt", b"images"),
        _entry("sceneDataset/points3D.txt", b"points"),
    ]
    session = client.post(
        "/api/sync/sessions",
        json={"token": token, "jobId": "job-001", "taskName": "room", "files": files},
    ).json()

    for item in files:
        upload = client.put(
            f"/api/sync/sessions/{session['sessionId']}/files",
            data={
                "sessionToken": session["sessionToken"],
                "relativePath": item["path"],
                "sha256": item["sha256"],
                "sizeBytes": str(item["sizeBytes"]),
            },
            files={"file": (Path(str(item["path"])).name, _content_for_entry(str(item["path"])), "application/octet-stream")},
        )
        assert upload.status_code == 200

    task_dir = next(tmp_path.glob(f"*/*_{session['sessionId'][:8]}"))
    (task_dir / "dataset" / "sceneDataset" / "images.txt").unlink()

    finalized = client.post(
        f"/api/sync/sessions/{session['sessionId']}/finalize",
        json={"sessionToken": session["sessionToken"]},
    )

    assert finalized.status_code == 400
    assert finalized.json()["detail"] == "missing synced file on disk: sceneDataset/images.txt"


def test_dashboard_lists_receiving_file_sync_session_with_progress(tmp_path: Path) -> None:
    client = TestClient(create_app(ReceiverConfig(output_dir=tmp_path), automation_enabled=False))
    token = client.get("/pair").json()["token"]
    files = [
        _entry("images/frame_000001.jpg", b"image-1"),
        _entry("images/frame_000002.jpg", b"image-2"),
        _entry("sceneDataset/cameras.txt", b"cameras"),
    ]
    session = client.post(
        "/api/sync/sessions",
        json={"token": token, "jobId": "job-001", "taskName": "room", "files": files},
    ).json()

    upload = client.put(
        f"/api/sync/sessions/{session['sessionId']}/files",
        data={
            "sessionToken": session["sessionToken"],
            "relativePath": "images/frame_000001.jpg",
            "sha256": files[0]["sha256"],
            "sizeBytes": str(files[0]["sizeBytes"]),
        },
        files={"file": ("frame_000001.jpg", b"image-1", "image/jpeg")},
    )
    assert upload.status_code == 200

    dashboard = client.get("/api/dashboard").json()
    task = dashboard["uploads"][0]

    assert task["uploadId"] == session["sessionId"]
    assert task["status"] == "receiving"
    assert task["taskName"] == "room"
    assert task["datasetPath"]
    assert task["openPath"]
    assert task["syncProgress"] == {
        "completedFiles": 1,
        "totalFiles": 3,
        "completedBytes": len(b"image-1"),
        "totalBytes": len(b"image-1") + len(b"image-2") + len(b"cameras"),
    }


def test_file_sync_returns_device_credential_and_accepts_it_for_next_session(tmp_path: Path) -> None:
    client = TestClient(create_app(ReceiverConfig(output_dir=tmp_path), automation_enabled=False))
    token = client.get("/pair").json()["token"]

    first = client.post(
        "/api/sync/sessions",
        json={
            "token": token,
            "jobId": "job-001",
            "taskName": "room",
            "files": [_entry("images/frame_000001.jpg", b"image")],
        },
    )

    assert first.status_code == 200
    first_payload = first.json()
    assert first_payload["deviceId"]
    assert first_payload["deviceToken"]

    reused_token = client.post(
        "/api/sync/sessions",
        json={"token": token, "jobId": "job-002", "taskName": "room", "files": []},
    )
    assert reused_token.status_code == 403

    second = client.post(
        "/api/sync/sessions",
        json={
            "deviceToken": first_payload["deviceToken"],
            "jobId": "job-002",
            "taskName": "room",
            "files": [_entry("images/frame_000002.jpg", b"image-2")],
        },
    )

    assert second.status_code == 200
    assert second.json()["deviceId"] == first_payload["deviceId"]
    assert second.json()["deviceToken"] == first_payload["deviceToken"]


def test_file_sync_create_session_reports_invalid_path(tmp_path: Path) -> None:
    client = TestClient(create_app(ReceiverConfig(output_dir=tmp_path), automation_enabled=False))
    token = client.get("/pair").json()["token"]

    response = client.post(
        "/api/sync/sessions",
        json={
            "token": token,
            "jobId": "job-001",
            "taskName": "room",
            "files": [_entry("reports/quality/nested/coverage_heatmap.png", b"png")],
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "invalid sync file path: reports/quality/nested/coverage_heatmap.png"


def test_file_sync_rejects_path_traversal(tmp_path: Path) -> None:
    client = TestClient(create_app(ReceiverConfig(output_dir=tmp_path), automation_enabled=False))
    token = client.get("/pair").json()["token"]
    session = client.post(
        "/api/sync/sessions",
        json={"token": token, "jobId": "job-001", "taskName": "room", "files": []},
    ).json()

    response = client.put(
        f"/api/sync/sessions/{session['sessionId']}/files",
        data={
            "sessionToken": session["sessionToken"],
            "relativePath": "../escape.txt",
            "sha256": _sha256(b"bad"),
            "sizeBytes": "3",
        },
        files={"file": ("escape.txt", b"bad", "text/plain")},
    )

    assert response.status_code == 400
    assert "path" in response.json()["detail"]


def test_file_sync_finalize_requires_colmap_files(tmp_path: Path) -> None:
    client = TestClient(create_app(ReceiverConfig(output_dir=tmp_path), automation_enabled=False))
    token = client.get("/pair").json()["token"]
    session = client.post(
        "/api/sync/sessions",
        json={"token": token, "jobId": "job-001", "taskName": "room", "files": []},
    ).json()

    response = client.post(
        f"/api/sync/sessions/{session['sessionId']}/finalize",
        json={"sessionToken": session["sessionToken"]},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "no files completed"


def test_management_endpoints_require_nonce(tmp_path: Path) -> None:
    client = TestClient(create_app(ReceiverConfig(output_dir=tmp_path), automation_enabled=False))

    unauthorized = client.post("/api/open-path", json={"path": str(tmp_path)})
    assert unauthorized.status_code == 403

    nonce = client.get("/api/dashboard").json()["managementNonce"]
    authorized = client.post(
        "/api/open-path",
        json={"path": str(tmp_path)},
        headers={"X-WorldGS-Management-Nonce": nonce},
    )
    assert authorized.status_code == 200


def _entry(path: str, content: bytes) -> dict[str, object]:
    return {"path": path, "sizeBytes": len(content), "sha256": _sha256(content)}


def _content_for_entry(path: str) -> bytes:
    values = {
        "images/frame_000001.jpg": b"image-1",
        "images/frame_000002.jpg": b"image-2",
        "sceneDataset/cameras.txt": b"cameras",
        "sceneDataset/images.txt": b"images",
        "sceneDataset/points3D.txt": b"points",
        "reports/sfm_quality_report.json": b'{"schemaVersion":1,"sparsePointCount":2,"visualizations":{"coverageHeatmap":{"path":"quality/coverage_heatmap.png"},"residualPlots":[{"cameraId":1,"path":"quality/camera_residual_1.png","observationCount":4}]}}',
        "reports/quality/coverage_heatmap.png": b"png",
        "reports/quality/camera_residual_1.png": b"png",
    }
    return values[path]


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
