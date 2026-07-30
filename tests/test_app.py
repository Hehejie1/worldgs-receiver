import hashlib
import json
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

import worldgs_receiver.app as app_module
from worldgs_receiver.app import create_app
from worldgs_receiver.config import ReceiverConfig


def _recent_iso(days_ago: int = 0, hour: int = 10) -> str:
    return (
        datetime.now(timezone.utc).replace(hour=hour, minute=0, second=0, microsecond=0) - timedelta(days=days_ago)
    ).isoformat()


def _recent_day(days_ago: int = 0) -> str:
    return (datetime.now(timezone.utc).date() - timedelta(days=days_ago)).isoformat()


def test_pair_returns_upload_contract(tmp_path: Path) -> None:
    client = TestClient(create_app(ReceiverConfig(output_dir=tmp_path)))

    response = client.get("/pair")

    assert response.status_code == 200
    payload = response.json()
    assert payload["computerName"]
    assert payload["uploadUrl"] == "/upload"
    assert payload["token"]
    assert payload["expiresInSeconds"] == 1800


def test_dashboard_scan_url_uses_shared_lan_address(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "local_lan_addresses", lambda: ["192.168.1.8"])
    client = TestClient(create_app(ReceiverConfig(output_dir=tmp_path)))

    response = client.get("/api/dashboard", headers={"host": "localhost:8787"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["scanUrl"].startswith("http://192.168.1.8:8787/upload?token=")
    assert payload["lanUrls"] == ["http://192.168.1.8:8787"]


def test_dashboard_reuses_pairing_token_between_auto_refreshes(tmp_path: Path) -> None:
    client = TestClient(create_app(ReceiverConfig(output_dir=tmp_path)))

    first = client.get("/api/dashboard").json()
    second = client.get("/api/dashboard").json()

    assert second["token"] == first["token"]
    assert second["scanUrl"] == first["scanUrl"]
    assert 1700 <= second["expiresInSeconds"] <= 1800


def test_dashboard_marks_upload_with_active_automation_run_as_training(tmp_path: Path) -> None:
    upload_id = "upload-1"
    task_dir = tmp_path / "2026-06-25" / "job-001_abcd1234"
    extracted = task_dir / "extracted"
    extracted.mkdir(parents=True)
    package = task_dir / "package.zip"
    package.write_bytes(b"zip")
    (task_dir / "upload_report.json").write_text(
        json.dumps(
            {
                "ok": True,
                "uploadId": upload_id,
                "taskName": "job-001",
                "packagePath": str(package),
                "extractedPath": str(extracted),
                "openPath": str(task_dir),
                "sizeBytes": 3,
            }
        ),
        encoding="utf-8",
    )
    run_dir = tmp_path / "automations" / "pointcosm" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "summary.json").write_text(
        json.dumps(
            {
                "automationRunId": "run-1",
                "uploadId": upload_id,
                "taskName": "job-001",
                "status": "running",
                "platformId": "explorerglobal",
                "platformName": "知天下",
                "message": "正在上传照片到知天下，请不要关闭 Firefox。",
                "startedAt": "2026-06-25T10:00:00+00:00",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    client = TestClient(create_app(ReceiverConfig(output_dir=tmp_path), automation_enabled=False))

    payload = client.get("/api/dashboard").json()

    upload = payload["uploads"][0]
    assert upload["uploadId"] == upload_id
    assert upload["automationTraining"] == {
        "automationRunId": "run-1",
        "status": "running",
        "platformId": "explorerglobal",
        "platformName": "知天下",
        "message": "正在上传照片到知天下，请不要关闭 Firefox。",
    }


def test_dashboard_marks_upload_with_active_local_training(tmp_path: Path) -> None:
    upload_id = "upload-local-1"
    task_dir = tmp_path / "2026-06-29" / "room_abcd1234"
    dataset = task_dir / "dataset"
    dataset.mkdir(parents=True)
    (task_dir / "upload_report.json").write_text(
        json.dumps(
            {
                "ok": True,
                "uploadId": upload_id,
                "taskName": "room",
                "packagePath": str(dataset),
                "extractedPath": str(dataset),
                "datasetPath": str(dataset),
                "openPath": str(task_dir),
                "sizeBytes": 3,
            }
        ),
        encoding="utf-8",
    )
    run_dir = task_dir / "local_training" / "run-local-1"
    run_dir.mkdir(parents=True)
    (run_dir / "summary.json").write_text(
        json.dumps(
            {
                "trainingRunId": "run-local-1",
                "uploadId": upload_id,
                "status": "running",
                "progressPercent": 42,
                "currentStep": "training",
                "message": "本地高斯训练运行中",
                "startedAt": "2026-06-29T10:00:00+00:00",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    client = TestClient(create_app(ReceiverConfig(output_dir=tmp_path), automation_enabled=False))

    upload = client.get("/api/dashboard").json()["uploads"][0]

    assert upload["localTraining"] == {
        "trainingRunId": "run-local-1",
        "status": "running",
        "progressPercent": 42,
        "currentStep": "training",
        "message": "本地高斯训练运行中",
    }


def test_upload_accepts_token_once_and_reports_status(tmp_path: Path) -> None:
    client = TestClient(create_app(ReceiverConfig(output_dir=tmp_path)))
    pair = client.get("/pair").json()
    package = _make_package(tmp_path)
    content = package.read_bytes()
    checksum = hashlib.sha256(content).hexdigest()

    response = client.post(
        "/upload",
        data={
            "token": pair["token"],
            "sha256": checksum,
            "deviceName": "android-test",
        },
        files={"file": ("worldgs_job-001.zip", content, "application/zip")},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["uploadId"]
    assert payload["sha256"] == checksum
    assert Path(payload["savePath"]).is_file()

    status = client.get(f"/uploads/{payload['uploadId']}")
    assert status.status_code == 200
    assert status.json()["ok"] is True
    dashboard_upload = client.get("/api/dashboard").json()["uploads"][0]
    assert dashboard_upload["sfmQualityReport"]["sparsePointCount"] == 2
    coverage_url = dashboard_upload["sfmQualityReport"]["visualizations"]["coverageHeatmap"]["url"]
    assert coverage_url.endswith("/sfm-quality/coverage_heatmap.png")
    assert client.get(coverage_url).status_code == 200

    reused = client.post(
        "/upload",
        data={"token": pair["token"], "deviceName": "android-test"},
        files={"file": ("worldgs_job-001.zip", content, "application/zip")},
    )
    assert reused.status_code == 403


def test_track_records_event_and_summary_requires_auth(tmp_path: Path) -> None:
    client = TestClient(
        create_app(
            ReceiverConfig(
                output_dir=tmp_path,
                dashboard_username="admin",
                dashboard_password="secret",
            )
        )
    )

    response = client.post(
        "/api/track",
        json={
            "event": "page_view",
            "client_id": "visitor-1",
            "source": "worldgs.website",
            "path": "/",
            "occurred_at": "2026-06-23T10:00:00Z",
        },
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert (tmp_path / "analytics" / "events.jsonl").is_file()

    unauthorized = client.get("/api/analytics/summary")
    assert unauthorized.status_code == 401

    summary = client.get("/api/analytics/summary", auth=("admin", "secret"))
    assert summary.status_code == 200
    payload = summary.json()
    assert payload["totals"]["page_views"] == 1
    assert payload["top_events"][0] == {"event": "page_view", "count": 1}


def test_android_dau_counts_unique_clients(tmp_path: Path) -> None:
    client = TestClient(
        create_app(
            ReceiverConfig(
                output_dir=tmp_path,
                dashboard_username="admin",
                dashboard_password="secret",
            )
        )
    )

    for client_id in ["device-a", "device-a", "device-b"]:
        response = client.post(
            "/api/track",
            json={
                "event": "android_dau",
                "client_id": client_id,
                "source": "worldgs.android",
                "occurred_at": _recent_iso(),
            },
        )
        assert response.status_code == 200

    summary = client.get("/api/analytics/summary", auth=("admin", "secret")).json()
    day = next(item for item in summary["last_7_days"] if item["date"] == _recent_day())
    assert day["android_dau"] == 2


def test_security_summary_tracks_dashboard_auth_failures_and_alerts(tmp_path: Path) -> None:
    client = TestClient(
        create_app(
            ReceiverConfig(
                output_dir=tmp_path,
                dashboard_username="admin",
                dashboard_password="secret",
            )
        )
    )

    for _ in range(5):
        response = client.get("/api/analytics/summary")
        assert response.status_code == 401

    summary = client.get("/api/security/summary?days=7", auth=("admin", "secret"))
    assert summary.status_code == 200
    payload = summary.json()
    assert payload["totals"]["events"] == 5
    assert payload["totals"]["alerts"] == 1
    assert payload["top_signatures"][0]["attack_label"] == "dashboard_auth_probe"
    assert payload["recent_alerts"][0]["attack_label"] == "dashboard_auth_probe"
    assert payload["recent_alerts"][0]["count_in_window"] >= 5


def test_security_summary_tracks_path_probe_signatures(tmp_path: Path) -> None:
    client = TestClient(
        create_app(
            ReceiverConfig(
                output_dir=tmp_path,
                dashboard_username="admin",
                dashboard_password="secret",
            )
        )
    )

    response = client.get("/wp-login.php")
    assert response.status_code == 404

    summary = client.get("/api/security/summary?days=7", auth=("admin", "secret"))
    assert summary.status_code == 200
    payload = summary.json()
    assert payload["totals"]["events"] == 1
    assert payload["top_signatures"][0]["attack_label"] == "wordpress_probe"
    assert payload["recent_events"][0]["path"] == "/wp-login.php"
    assert payload["recent_events"][0]["event_type"] == "path_probe"


def test_website_showcase_config_returns_defaults(tmp_path: Path) -> None:
    client = TestClient(create_app(ReceiverConfig(output_dir=tmp_path)))

    response = client.get("/api/website/showcase-config")

    assert response.status_code == 200
    payload = response.json()
    assert payload["sectionTitle"] == "精选案例"
    assert [item["title"] for item in payload["showcases"]] == ["苏东坡", "石狮"]


def test_website_showcase_config_put_requires_auth_and_persists(tmp_path: Path) -> None:
    client = TestClient(
        create_app(
            ReceiverConfig(
                output_dir=tmp_path,
                dashboard_username="admin",
                dashboard_password="secret",
            )
        )
    )

    payload = {
        "sectionEyebrow": "Featured Cases",
        "sectionTitle": "首页案例",
        "sectionDescription": "用于官网首页展示的精选模型。",
        "showcases": [
            {
                "id": "case-1",
                "title": "苏东坡",
                "subtitle": "图一",
                "description": "苏堤南端的苏东坡石像。",
                "shareUrl": "https://worldgs.notemeld.wiki/share/sh_vDXmrFEKSvsIBGrXvSzWFzPR",
                "imageUrl": "",
                "sortOrder": 30,
                "enabled": True,
            }
        ],
    }

    unauthorized = client.put("/api/website/showcase-config", json=payload)
    assert unauthorized.status_code == 401

    response = client.put("/api/website/showcase-config", json=payload, auth=("admin", "secret"))
    assert response.status_code == 200
    saved = response.json()
    assert saved["sectionTitle"] == "首页案例"
    assert saved["showcases"][0]["sortOrder"] == 30

    config_path = tmp_path / "website" / "showcase-config.json"
    assert config_path.is_file()
    persisted = json.loads(config_path.read_text(encoding="utf-8"))
    assert persisted["showcases"][0]["title"] == "苏东坡"


def test_model_share_upload_and_fetch_round_trip_with_sog(tmp_path: Path) -> None:
    client = TestClient(create_app(ReceiverConfig(output_dir=tmp_path)))

    create_response = client.post(
        "/api/model-shares",
        data={
            "title": "苏东坡",
            "description": "测试分享",
            "format": "sog",
            "source_format": "sog",
            "device_model": "Xiaomi 14",
        },
        files={"model": ("model.sog", b"SOGDATA", "application/octet-stream")},
        headers={"host": "worldgs.notemeld.wiki", "x-forwarded-proto": "https"},
    )

    assert create_response.status_code == 200
    created = create_response.json()
    assert created["id"].startswith("sh_")
    assert created["url"] == f"https://worldgs.notemeld.wiki/share/{created['id']}"
    assert created["status"] == "ready"

    get_response = client.get(f"/api/model-shares/{created['id']}")

    assert get_response.status_code == 200
    payload = get_response.json()
    assert payload["id"] == created["id"]
    assert payload["title"] == "苏东坡"
    assert payload["description"] == "测试分享"
    assert payload["status"] == "ready"
    assert payload["format"] == "sog"
    assert payload["device_model"] == "Xiaomi 14"
    assert payload["asset_url"] == f"https://worldgs.notemeld.wiki/uploads/model-shares/{created['id']}/model.sog"


def test_model_share_fetch_missing_record_returns_404(tmp_path: Path) -> None:
    client = TestClient(create_app(ReceiverConfig(output_dir=tmp_path)))

    response = client.get("/api/model-shares/sh_missing")

    assert response.status_code == 404
    assert response.json()["detail"] == "model share not found"


def test_diag_event_records_and_summary_requires_auth(tmp_path: Path) -> None:
    client = TestClient(
        create_app(
            ReceiverConfig(
                output_dir=tmp_path,
                dashboard_username="admin",
                dashboard_password="secret",
            )
        )
    )

    response = client.post(
        "/api/diag/v1/events",
        json={
            "t": "scene_run_v1",
            "sid": "device-a",
            "rid": "job-hash",
            "ts": _recent_iso(),
            "app": {"v": "0.0.1", "p": "android"},
            "dev": {"m": "Xiaomi 24115RA8EC", "os": "14"},
            "hw": {"c": "SM7635", "g": "Adreno", "api": 34, "abi": "arm64-v8a", "mt": 12_000, "ma": 1_800, "low": False},
            "mode": "quick",
            "phase": "done",
            "cfg": {"fps": 0.5, "res": 512, "steps": 600, "cap": 120_000, "view": 1},
            "dur": {"prep": 10, "pose": 20, "fit": 30, "pack": 40, "total": 100},
            "in": {"items": 1, "frames": 80, "w": 512, "h": 288, "blur": 0.12, "exposure": 0.03},
            "solve": {"ok": 72, "ratio": 0.9, "risk": "low", "miss": 8, "missRun": 2, "missZone": "mid"},
            "out": {"points": 85_000, "bytes": 1_234},
            "err": None,
        },
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert (tmp_path / "diag" / "events.jsonl").is_file()

    unauthorized = client.get("/api/diag/v1/summary")
    assert unauthorized.status_code == 401

    summary = client.get("/api/diag/v1/summary?days=7", auth=("admin", "secret")).json()
    assert summary["today"]["runs"] >= 0
    assert summary["duration"]["fit"]["max"] == 30
    assert summary["quality"]["registered_ratio_avg"] == 0.9
    assert summary["risk_distribution"] == [{"risk": "low", "count": 1}]


def test_diag_summary_groups_mode_frames_failures_and_versions(tmp_path: Path) -> None:
    client = TestClient(
        create_app(
            ReceiverConfig(
                output_dir=tmp_path,
                dashboard_username="admin",
                dashboard_password="secret",
            )
        )
    )

    events = [
        {
            "t": "scene_run_v1",
            "sid": "device-a",
            "rid": "quick-10",
            "ts": _recent_iso(hour=10),
            "app": {"v": "0.0.1", "p": "android"},
            "mode": "quick",
            "phase": "done",
            "dur": {"prep": 1_000, "pose": 2_000, "fit": 7_000, "pack": 0, "total": 10_000},
            "in": {"frames": 10},
            "solve": {"risk": "low"},
            "err": None,
        },
        {
            "t": "scene_run_v1",
            "sid": "device-a",
            "rid": "quick-100",
            "ts": _recent_iso(hour=11),
            "app": {"v": "0.0.1", "p": "android"},
            "mode": "quick",
            "phase": "done",
            "dur": {"prep": 3_000, "pose": 20_000, "fit": 80_000, "pack": 2_000, "total": 100_000},
            "in": {"frames": 100},
            "solve": {"risk": "low"},
            "err": None,
        },
        {
            "t": "scene_run_v1",
            "sid": "device-b",
            "rid": "full-100",
            "ts": _recent_iso(hour=12),
            "app": {"v": "0.0.2", "p": "android"},
            "mode": "full",
            "phase": "done",
            "dur": {"prep": 5_000, "pose": 30_000, "fit": 240_000, "pack": 5_000, "total": 300_000},
            "in": {"frames": 100},
            "solve": {"risk": "high"},
            "err": None,
        },
        {
            "t": "scene_run_v1",
            "sid": "device-c",
            "rid": "quick-failed",
            "ts": _recent_iso(hour=13),
            "app": {"v": "0.0.2", "p": "android"},
            "mode": "quick",
            "phase": "failed",
            "dur": {"prep": 500, "pose": 0, "total": 600},
            "in": {"frames": 100},
            "err": {"code": "PosePipelineCommandFailed"},
        },
    ]
    for event in events:
        assert client.post("/api/diag/v1/events", json=event).status_code == 200

    summary = client.get("/api/diag/v1/summary?days=7", auth=("admin", "secret")).json()

    quick = next(item for item in summary["mode_stats"] if item["mode"] == "quick")
    assert quick["runs"] == 3
    assert quick["success"] == 2
    assert quick["failures"] == 1
    assert quick["success_rate"] == 0.6667
    assert quick["avg_total_ms"] == 55_000
    assert quick["avg_total_per_frame_ms"] == 1_000
    assert quick["avg_fit_per_frame_ms"] == 750

    bucket_10 = next(item for item in summary["frame_buckets"] if item["bucket"] == "1-10")
    assert bucket_10["runs"] == 1
    assert bucket_10["avg_frames"] == 10
    assert bucket_10["avg_total_per_frame_ms"] == 1_000

    bucket_100 = next(
        item for item in summary["frame_buckets"]
        if item["mode"] == "quick" and item["bucket"] == "51-100"
    )
    assert bucket_100["runs"] == 1
    assert bucket_100["avg_frames"] == 100
    assert bucket_100["avg_total_per_frame_ms"] == 1_000

    full_bucket_100 = next(
        item for item in summary["frame_buckets"]
        if item["mode"] == "full" and item["bucket"] == "51-100"
    )
    assert full_bucket_100["runs"] == 1
    assert full_bucket_100["avg_total_per_frame_ms"] == 3_000

    version_002 = next(item for item in summary["version_stats"] if item["version"] == "0.0.2")
    assert version_002["runs"] == 2
    assert version_002["success_rate"] == 0.5
    assert version_002["top_error"] == "PosePipelineCommandFailed"

    assert summary["failure_reasons"] == [
        {
            "code": "PosePipelineCommandFailed",
            "count": 1,
            "phase": "failed",
            "mode": "quick",
        }
    ]


def test_diag_issue_upload_records_index_without_unpacking_user_assets(tmp_path: Path) -> None:
    client = TestClient(
        create_app(
            ReceiverConfig(
                output_dir=tmp_path,
                dashboard_username="admin",
                dashboard_password="secret",
            )
        )
    )
    issue_zip = tmp_path / "issue.zip"
    with zipfile.ZipFile(issue_zip, "w") as archive:
        archive.writestr("manifest.json", "{}")
        archive.writestr("logs/app_tail.log", "tail")

    with issue_zip.open("rb") as file:
        response = client.post(
            "/api/diag/v1/issues",
            data={"meta": json.dumps({"sid": "device-a", "rid": "job-hash", "app": "0.0.1", "code": "E_STAGE_FAILED"})},
            files={"file": ("issue.zip", file, "application/zip")},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["id"]
    assert (tmp_path / "diag" / "issues" / f"{payload['id']}.zip").is_file()
    index = (tmp_path / "diag" / "issues" / "index.jsonl").read_text(encoding="utf-8")
    assert "job-hash" in index
    assert "E_STAGE_FAILED" in index

    client.post(
        "/api/diag/v1/events",
        json={
            "t": "scene_run_v1",
            "sid": "device-a",
            "rid": "job-hash",
            "ts": _recent_iso(),
            "app": {"v": "0.0.1", "p": "android"},
            "mode": "quick",
            "phase": "failed",
            "dur": {"total": 100},
            "in": {"frames": 10},
            "err": {"code": "E_STAGE_FAILED"},
        },
    )
    summary = client.get("/api/diag/v1/summary?days=7", auth=("admin", "secret")).json()
    failure = summary["recent_failures"][0]
    assert failure["rid"] == "job-hash"
    assert failure["download_url"] == f"/api/diag/v1/issues/{payload['id']}/download"

    unauthorized = client.get(f"/api/diag/v1/issues/{payload['id']}/download")
    assert unauthorized.status_code == 401

    download = client.get(
        f"/api/diag/v1/issues/{payload['id']}/download",
        auth=("admin", "secret"),
    )
    assert download.status_code == 200
    assert download.content == issue_zip.read_bytes()


def test_receiver_page_contains_script_settings_and_script_run_controls(tmp_path: Path) -> None:
    client = TestClient(create_app(ReceiverConfig(output_dir=tmp_path), automation_enabled=False))

    response = client.get("/")

    assert response.status_code == 200
    html = response.text
    assert "脚本设置" in html
    assert "脚本执行" in html
    assert "开始训练" in html
    assert "打开结果" in html
    assert "/api/scripts" in html
    assert "/api/script-runs" in html
    assert "renderScriptList" in html
    assert "renderTrainingMenuOptions" in html
    assert "startScriptRun" in html
    assert "task.scriptRun" in html


def test_receiver_page_uses_dropdown_training_mode_menu(tmp_path: Path) -> None:
    client = TestClient(create_app(ReceiverConfig(output_dir=tmp_path), automation_enabled=False))

    response = client.get("/")

    assert response.status_code == 200
    html = response.text
    assert "training-mode-menu" in html
    assert 'id="globalTrainingMenu"' in html
    assert "training-mode-option" in html
    assert "data-script-id" in html
    assert "暂无可用脚本" in html
    assert "toggleTrainingMenu" in html
    assert "positionTrainingMenu" in html
    assert "handleTrainingMenuViewportChange" in html
    assert 'document.addEventListener("scroll", handleTrainingMenuViewportChange, true);' in html
    assert 'window.addEventListener("resize", handleTrainingMenuViewportChange);' in html


def test_receiver_page_displays_dataset_count_instead_of_zip_package(tmp_path: Path) -> None:
    client = TestClient(create_app(ReceiverConfig(output_dir=tmp_path), automation_enabled=False))

    response = client.get("/")

    assert response.status_code == 200
    assert "package.zip" not in response.text
    assert "图片" in response.text
    assert "高清可同步" not in response.text
    assert '<div class="task-status">' not in response.text
    assert "openPath(card.dataset.openPath)" not in response.text
    assert 'class="folder-meta"' in response.text
    assert "点击打开" in response.text
    assert "model-result-button" in response.text
    assert "上传高清模型" in response.text
    assert "同步到手机" in response.text
    assert "再次点击确认删除会移除当前上传的高清模型" in response.text


def test_dashboard_includes_scripts_and_active_script_run(tmp_path: Path) -> None:
    upload_id = "upload-script-1"
    task_dir = tmp_path / "2026-07-04" / "job-script_abcd1234"
    dataset = task_dir / "dataset"
    dataset.mkdir(parents=True)
    (task_dir / "upload_report.json").write_text(
        json.dumps(
            {
                "ok": True,
                "uploadId": upload_id,
                "taskName": "job-script",
                "packagePath": str(dataset),
                "extractedPath": str(dataset),
                "datasetPath": str(dataset),
                "openPath": str(task_dir),
                "sizeBytes": 3,
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "script_registry.json").write_text(
        json.dumps(
            {
                "scripts": [
                    {
                        "scriptId": "script-1",
                        "name": "知天下脚本",
                        "description": "自动训练",
                        "scriptType": "platform",
                        "entryFile": str(tmp_path / "scripts" / "script-1" / "run.sh"),
                        "customActions": [
                            {
                                "actionId": "action-login",
                                "name": "登录",
                                "command": "run.sh --login",
                            }
                        ],
                        "enabled": True,
                        "createdAt": "2026-07-04T10:00:00+00:00",
                        "updatedAt": "2026-07-04T10:00:00+00:00",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    run_dir = task_dir / "script_runs" / "run-script-1"
    run_dir.mkdir(parents=True)
    (run_dir / "summary.json").write_text(
        json.dumps(
            {
                "scriptRunId": "run-script-1",
                "scriptId": "script-1",
                "scriptName": "知天下脚本",
                "scriptType": "platform",
                "uploadId": upload_id,
                "status": "running",
                "message": "脚本运行中",
                "startedAt": "2026-07-04T10:00:00+00:00",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    client = TestClient(create_app(ReceiverConfig(output_dir=tmp_path), automation_enabled=False))

    payload = client.get("/api/dashboard").json()

    assert payload["scripts"] == [
        {
            "scriptId": "script-1",
            "name": "知天下脚本",
            "description": "自动训练",
            "scriptType": "platform",
            "entryFile": str(tmp_path / "scripts" / "script-1" / "run.sh"),
            "entryFileRelative": "run.sh",
            "customActions": [
                {
                    "actionId": "action-login",
                    "name": "登录",
                    "command": "run.sh --login",
                }
            ],
            "enabled": True,
            "createdAt": "2026-07-04T10:00:00+00:00",
            "updatedAt": "2026-07-04T10:00:00+00:00",
        }
    ]
    assert payload["uploads"][0]["scriptRun"] == {
        "scriptRunId": "run-script-1",
        "scriptId": "script-1",
        "scriptName": "知天下脚本",
        "scriptType": "platform",
        "status": "running",
        "message": "脚本运行中",
    }


def test_script_api_creates_and_lists_script(tmp_path: Path) -> None:
    client = TestClient(create_app(ReceiverConfig(output_dir=tmp_path), automation_enabled=False))
    nonce = client.get("/api/dashboard").json()["managementNonce"]

    response = client.post(
        "/api/scripts",
        headers={"X-WorldGS-Management-Nonce": nonce},
        data={
            "name": "知天下训练脚本",
            "description": "自动上传并提交训练",
            "scriptType": "platform",
            "enabled": "true",
        },
        files={"file": ("run.sh", b"#!/bin/sh\nexit 0\n", "text/plain")},
    )

    assert response.status_code == 200
    created = response.json()["script"]
    assert created["name"] == "知天下训练脚本"
    assert created["customActions"] == []
    assert created["entryFileRelative"] == "run.sh"

    listed = client.get("/api/scripts")
    assert listed.status_code == 200
    assert listed.json()["scripts"][0]["scriptId"] == created["scriptId"]


def test_script_api_creates_zip_script_bundle(tmp_path: Path) -> None:
    client = TestClient(create_app(ReceiverConfig(output_dir=tmp_path), automation_enabled=False))
    nonce = client.get("/api/dashboard").json()["managementNonce"]
    bundle_path = tmp_path / "explorerglobal.zip"
    with zipfile.ZipFile(bundle_path, "w") as archive:
        archive.writestr("run_explorerglobal.sh", "#!/bin/sh\npython3 ./src/main.py\n")
        archive.writestr("src/main.py", "print('ok')\n")

    response = client.post(
        "/api/scripts",
        headers={"X-WorldGS-Management-Nonce": nonce},
        data={
            "name": "知天下项目脚本",
            "description": "zip 包脚本",
            "scriptType": "platform",
            "enabled": "true",
            "entryFile": "run_explorerglobal.sh",
        },
        files={"file": ("explorerglobal.zip", bundle_path.read_bytes(), "application/zip")},
    )

    assert response.status_code == 200
    created = response.json()["script"]
    entry_path = Path(str(created["entryFile"]))
    assert entry_path.name == "run_explorerglobal.sh"
    assert created["entryFileRelative"] == "run_explorerglobal.sh"
    assert (entry_path.parent / "src" / "main.py").is_file()


def test_script_api_updates_custom_actions(tmp_path: Path) -> None:
    client = TestClient(create_app(ReceiverConfig(output_dir=tmp_path), automation_enabled=False))
    nonce = client.get("/api/dashboard").json()["managementNonce"]
    created = client.post(
        "/api/scripts",
        headers={"X-WorldGS-Management-Nonce": nonce},
        data={
            "name": "知天下训练脚本",
            "description": "自动上传并提交训练",
            "scriptType": "platform",
            "enabled": "true",
        },
        files={"file": ("run.sh", b"#!/bin/sh\nexit 0\n", "text/plain")},
    ).json()["script"]

    response = client.put(
        f"/api/scripts/{created['scriptId']}",
        headers={"X-WorldGS-Management-Nonce": nonce},
        data={
            "name": "知天下训练脚本",
            "description": "自动上传并提交训练",
            "scriptType": "platform",
            "entryFile": "run.sh",
            "customActionsJson": json.dumps(
                [
                    {"name": "登录", "command": "run.sh --login"},
                    {"name": "检查登录", "command": "run.sh --check"},
                ],
                ensure_ascii=False,
            ),
        },
    )

    assert response.status_code == 200
    updated = response.json()["script"]
    assert [item["name"] for item in updated["customActions"]] == ["登录", "检查登录"]
    assert updated["customActions"][0]["command"] == "run.sh --login"


def test_receiver_page_uses_wide_scrollable_sfm_quality_modal(tmp_path: Path) -> None:
    client = TestClient(create_app(ReceiverConfig(output_dir=tmp_path), automation_enabled=False))

    response = client.get("/")

    assert response.status_code == 200
    html = response.text
    assert '<div class="modal-card sfm-quality-card">' in html
    assert '<div class="sfm-quality-modal-body" id="sfmQualityModalBody"></div>' in html
    assert "width: min(74vw, 920px);" in html
    assert "height: min(78vh, 680px);" in html
    assert "overflow-y: auto;" in html
    sfm_button = html.split("sfm-quality-button", 1)[1].split("</button>", 1)[0]
    assert "material-symbols-outlined" not in sfm_button
    assert 'aria-label="空三质量"' in sfm_button


def test_receiver_page_contains_script_editing_and_custom_action_controls(tmp_path: Path) -> None:
    client = TestClient(create_app(ReceiverConfig(output_dir=tmp_path), automation_enabled=False))

    response = client.get("/")

    assert response.status_code == 200
    html = response.text
    assert 'id="scriptEditingId"' in html
    assert 'id="addCustomActionButton"' in html
    assert 'id="customActionList"' in html
    assert 'id="scriptResetButton"' in html
    assert 'data-script-action="edit"' in html
    assert 'data-script-custom-run="true"' in html
    assert "customActionsJson" in html


def test_script_run_api_allows_global_custom_action_without_upload(tmp_path: Path) -> None:
    client = TestClient(create_app(ReceiverConfig(output_dir=tmp_path), automation_enabled=False))
    nonce = client.get("/api/dashboard").json()["managementNonce"]
    script_dir = tmp_path / "scripts" / "script-1"
    script_dir.mkdir(parents=True)
    entry_file = script_dir / "run.sh"
    entry_file.write_text(
        "#!/usr/bin/env bash\n"
        "set -e\n"
        "cat > \"$WORLDGS_OUTPUT_JSON\" <<JSON\n"
        "{\n"
        "  \"status\": \"succeeded\",\n"
        "  \"message\": \"global login ok\"\n"
        "}\n"
        "JSON\n",
        encoding="utf-8",
    )
    entry_file.chmod(0o755)
    (tmp_path / "script_registry.json").write_text(
        json.dumps(
            {
                "scripts": [
                    {
                        "scriptId": "script-1",
                        "name": "知天下脚本",
                        "description": "自动训练",
                        "scriptType": "platform",
                        "entryFile": str(entry_file),
                        "customActions": [
                            {
                                "actionId": "action-login",
                                "name": "登录",
                                "command": "run.sh --login",
                            }
                        ],
                        "enabled": True,
                        "createdAt": "2026-07-06T10:00:00+00:00",
                        "updatedAt": "2026-07-06T10:00:00+00:00",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    response = client.post(
        "/api/script-runs",
        headers={"X-WorldGS-Management-Nonce": nonce},
        json={"scriptId": "script-1", "actionId": "action-login"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["uploadId"] == ""
    assert payload["isGlobalAction"] is True
    assert payload["actionId"] == "action-login"


def test_receiver_page_guards_icon_font_fallback_and_blurs_upload_qr_in_sync_modal(tmp_path: Path) -> None:
    client = TestClient(create_app(ReceiverConfig(output_dir=tmp_path), automation_enabled=False))

    response = client.get("/")

    assert response.status_code == 200
    html = response.text
    assert "overflow: hidden;" in html
    assert "flex: 0 0 auto;" in html
    assert "max-width: 1em;" in html
    assert ".result-sync-modal-open .qr-wrap img" in html
    assert "filter: blur(10px)" in html
    assert 'document.body.classList.add("result-sync-modal-open")' in html
    assert 'document.body.classList.remove("result-sync-modal-open")' in html


def test_receiver_page_uses_split_task_action_layout_and_icon_only_top_actions(tmp_path: Path) -> None:
    client = TestClient(create_app(ReceiverConfig(output_dir=tmp_path), automation_enabled=False))

    response = client.get("/")

    assert response.status_code == 200
    html = response.text
    assert "task-top-actions" in html
    assert "task-primary-actions" in html
    assert "task-delete-button" in html
    assert "deleteTaskModal" in html
    assert "确认删除这个任务吗" in html
    assert "deleteTaskIcon()" in html
    sfm_button = html.split("sfm-quality-button", 1)[1].split("</button>", 1)[0]
    assert "空三质量" not in sfm_button.split(">", 1)[1]
    assert "sfmQualityIcon()" in sfm_button


def test_receiver_page_uses_inline_delete_confirmation_for_model_result(tmp_path: Path) -> None:
    client = TestClient(create_app(ReceiverConfig(output_dir=tmp_path), automation_enabled=False))

    response = client.get("/")

    assert response.status_code == 200
    html = response.text
    assert "window.confirm" not in html
    assert "resultModalDeletePending" in html
    assert "确认删除" in html
    assert "再次点击确认删除会移除当前上传的高清模型" in html


def test_upload_model_result_and_download_from_dashboard(tmp_path: Path) -> None:
    client = TestClient(create_app(ReceiverConfig(output_dir=tmp_path), automation_enabled=False))
    upload = _upload_sample_package(client, tmp_path)
    nonce = client.get("/api/dashboard").json()["managementNonce"]
    ply_content = b"ply\nformat ascii 1.0\nelement vertex 0\nend_header\n"

    response = client.post(
        f"/api/uploads/{upload['uploadId']}/result",
        headers={"X-WorldGS-Management-Nonce": nonce},
        files={"file": ("mobile_result.ply", ply_content, "application/octet-stream")},
    )

    assert response.status_code == 200
    model_result = response.json()["modelResult"]
    assert model_result["fileName"] == "mobile.ply"
    assert model_result["fileExt"] == "ply"
    assert model_result["sizeBytes"] == len(ply_content)
    assert model_result["downloadUrl"] == f"/api/uploads/{upload['uploadId']}/result/download"
    assert model_result["scanUrl"].startswith("worldgs://model-result?")
    assert Path(model_result["path"]).is_file()

    dashboard_upload = client.get("/api/dashboard").json()["uploads"][0]
    assert dashboard_upload["modelResult"]["fileName"] == "mobile.ply"

    download = client.get(model_result["downloadUrl"])
    assert download.status_code == 200
    assert download.content == ply_content


def test_dashboard_model_result_scan_url_uses_lan_ipv4_when_opened_from_localhost(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(app_module, "local_lan_addresses", lambda: ["192.168.1.8"])
    client = TestClient(create_app(ReceiverConfig(output_dir=tmp_path), automation_enabled=False))
    upload = _upload_sample_package(client, tmp_path)
    nonce = client.get("/api/dashboard", headers={"host": "localhost:8787"}).json()["managementNonce"]

    response = client.post(
        f"/api/uploads/{upload['uploadId']}/result",
        headers={
            "host": "localhost:8787",
            "X-WorldGS-Management-Nonce": nonce,
        },
        files={"file": ("mobile_result.ply", b"ply\n", "application/octet-stream")},
    )
    assert response.status_code == 200

    dashboard_upload = client.get(
        "/api/dashboard",
        headers={"host": "localhost:8787"},
    ).json()["uploads"][0]

    scan_url = dashboard_upload["modelResult"]["scanUrl"]
    assert "url=http%3A%2F%2F192.168.1.8%3A8787%2Fapi%2Fuploads%2F" in scan_url
    assert "localhost" not in scan_url


def test_delete_model_result_removes_uploaded_file_and_dashboard_state(tmp_path: Path) -> None:
    client = TestClient(create_app(ReceiverConfig(output_dir=tmp_path), automation_enabled=False))
    upload = _upload_sample_package(client, tmp_path)
    nonce = client.get("/api/dashboard").json()["managementNonce"]
    ply_content = b"ply\nformat ascii 1.0\nelement vertex 0\nend_header\n"

    upload_response = client.post(
        f"/api/uploads/{upload['uploadId']}/result",
        headers={"X-WorldGS-Management-Nonce": nonce},
        files={"file": ("mobile_result.ply", ply_content, "application/octet-stream")},
    )
    assert upload_response.status_code == 200
    model_result = upload_response.json()["modelResult"]
    model_path = Path(model_result["path"])
    metadata_path = model_path.parent / "model_result.json"
    assert model_path.is_file()
    assert metadata_path.is_file()

    delete_response = client.delete(
        f"/api/uploads/{upload['uploadId']}/result",
        headers={"X-WorldGS-Management-Nonce": nonce},
    )

    assert delete_response.status_code == 200
    assert delete_response.json() == {"ok": True}
    assert not model_path.exists()
    assert not metadata_path.exists()
    dashboard_upload = client.get("/api/dashboard").json()["uploads"][0]
    assert "modelResult" not in dashboard_upload
    assert client.get(model_result["downloadUrl"]).status_code == 404


def test_delete_upload_removes_task_directory_and_dashboard_state(tmp_path: Path) -> None:
    client = TestClient(create_app(ReceiverConfig(output_dir=tmp_path), automation_enabled=False))
    upload = _upload_sample_package(client, tmp_path)
    nonce = client.get("/api/dashboard").json()["managementNonce"]
    task_dir = Path(upload["openPath"])

    response = client.delete(
        f"/api/uploads/{upload['uploadId']}",
        headers={"X-WorldGS-Management-Nonce": nonce},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert not task_dir.exists()
    assert client.get("/api/dashboard").json()["uploads"] == []


def test_upload_model_result_rejects_unsupported_suffix(tmp_path: Path) -> None:
    client = TestClient(create_app(ReceiverConfig(output_dir=tmp_path), automation_enabled=False))
    upload = _upload_sample_package(client, tmp_path)
    nonce = client.get("/api/dashboard").json()["managementNonce"]

    response = client.post(
        f"/api/uploads/{upload['uploadId']}/result",
        headers={"X-WorldGS-Management-Nonce": nonce},
        files={"file": ("notes.txt", b"not a model", "text/plain")},
    )

    assert response.status_code == 400


def test_start_automation_reuses_active_run_for_same_upload(tmp_path: Path) -> None:
    upload_id = "upload-1"
    task_dir = tmp_path / "2026-06-24" / "job-001_abcd1234"
    dataset = task_dir / "dataset"
    images = dataset / "images"
    scene_dataset = dataset / "sceneDataset"
    images.mkdir(parents=True)
    scene_dataset.mkdir(parents=True)
    for index in range(89):
        (images / f"frame_{index:06d}.jpg").write_bytes(b"jpg")
    for name in ["cameras.txt", "images.txt", "points3D.txt"]:
        (scene_dataset / name).write_text("# colmap\n", encoding="utf-8")
    package = task_dir / "package.zip"
    extracted = task_dir / "extracted"
    extracted.mkdir()
    package.write_bytes(b"zip")
    (task_dir / "upload_report.json").write_text(
        (
            "{"
            f'"ok": true, "uploadId": "{upload_id}", "taskName": "job-001", '
            f'"packagePath": "{package}", "extractedPath": "{extracted}", '
            f'"openPath": "{task_dir}", "sizeBytes": 3'
            "}"
        ),
        encoding="utf-8",
    )
    client = TestClient(create_app(ReceiverConfig(output_dir=tmp_path), automation_enabled=False))
    nonce = client.get("/api/dashboard").json()["managementNonce"]

    first = client.post(
        "/api/automation/runs",
        headers={"X-WorldGS-Management-Nonce": nonce},
        json={"uploadId": upload_id, "platformId": "explorerglobal"},
    )
    second = client.post(
        "/api/automation/runs",
        headers={"X-WorldGS-Management-Nonce": nonce},
        json={"uploadId": upload_id, "platformId": "explorerglobal"},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["automationRunId"] == first.json()["automationRunId"]
    assert second.json()["existing"] is True


def test_start_automation_ignores_stale_running_summary_and_starts_new_runner(tmp_path: Path, monkeypatch) -> None:
    class FakeLoginLauncher:
        def __init__(self, output_dir: Path) -> None:
            self.output_dir = output_dir
            self.close_calls: list[str] = []

        def open(self, platform_id: str, url: str) -> None:
            return None

        def close(self, platform_id: str) -> None:
            self.close_calls.append(platform_id)

    class FakeRunner:
        instances: list["FakeRunner"] = []

        def __init__(self, store: object, platform: object) -> None:
            self.store = store
            self.platform = platform
            self.started: list[tuple[str, object]] = []
            FakeRunner.instances.append(self)

        def start_background(self, run_id: str, task_context: object) -> None:
            self.started.append((run_id, task_context))

        def has_live_run(self, run_id: str) -> bool:
            return False

        def request_continue(self, run_id: str) -> None:
            return None

        def request_cancel(self, run_id: str) -> None:
            return None

    monkeypatch.setattr(app_module, "PlatformProfileLoginLauncher", FakeLoginLauncher)
    monkeypatch.setattr(app_module, "PlatformAutomationRunner", FakeRunner)
    upload_id = "upload-stale"
    task_dir = tmp_path / "2026-06-24" / "job-stale_abcd1234"
    dataset = task_dir / "dataset"
    images = dataset / "images"
    scene_dataset = dataset / "sceneDataset"
    images.mkdir(parents=True)
    scene_dataset.mkdir(parents=True)
    for index in range(89):
        (images / f"frame_{index:06d}.jpg").write_bytes(b"jpg")
    for name in ["cameras.txt", "images.txt", "points3D.txt"]:
        (scene_dataset / name).write_text("# colmap\n", encoding="utf-8")
    package = task_dir / "package.zip"
    extracted = task_dir / "extracted"
    extracted.mkdir()
    package.write_bytes(b"zip")
    (task_dir / "upload_report.json").write_text(
        (
            "{"
            f'"ok": true, "uploadId": "{upload_id}", "taskName": "job-stale", '
            f'"packagePath": "{package}", "extractedPath": "{extracted}", '
            f'"openPath": "{task_dir}", "sizeBytes": 3'
            "}"
        ),
        encoding="utf-8",
    )
    stale_run_dir = tmp_path / "automations" / "pointcosm" / "runs" / "stale-run-id"
    stale_run_dir.mkdir(parents=True)
    (stale_run_dir / "summary.json").write_text(
        json.dumps(
            {
                "automationRunId": "stale-run-id",
                "uploadId": upload_id,
                "taskName": "job-stale",
                "status": "running",
                "currentStepId": "submit",
                "platformId": "explorerglobal",
                "platformName": "知天下",
                "entryUrl": "https://3d.explorerglobal.cn/compute",
                "datasetPath": str(dataset),
                "imagesDir": str(images),
                "imageCount": 89,
                "startedAt": "2026-06-24T10:00:00+00:00",
                "endedAt": None,
                "error": None,
                "message": "Firefox 已启动",
                "latestScreenshot": None,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (stale_run_dir / "run_log.jsonl").write_text("", encoding="utf-8")
    client = TestClient(create_app(ReceiverConfig(output_dir=tmp_path), automation_enabled=True))
    nonce = client.get("/api/dashboard").json()["managementNonce"]

    response = client.post(
        "/api/automation/runs",
        headers={"X-WorldGS-Management-Nonce": nonce},
        json={"uploadId": upload_id, "platformId": "explorerglobal"},
    )

    assert response.status_code == 200
    assert response.json()["existing"] is False
    assert response.json()["automationRunId"] != "stale-run-id"
    assert len(FakeRunner.instances[0].started) == 1
    stale_summary = json.loads((stale_run_dir / "summary.json").read_text(encoding="utf-8"))
    assert stale_summary["status"] == "failed"
    assert stale_summary["message"] == "检测到残留的自动化运行状态，已忽略并重新启动。"


def test_login_platform_endpoint_opens_manual_login_browser(tmp_path: Path, monkeypatch) -> None:
    class FakeLoginLauncher:
        instances: list["FakeLoginLauncher"] = []

        def __init__(self, output_dir: Path) -> None:
            self.output_dir = output_dir
            self.open_calls: list[tuple[str, str]] = []
            self.close_calls: list[str] = []
            FakeLoginLauncher.instances.append(self)

        def open(self, platform_id: str, url: str) -> None:
            self.open_calls.append((platform_id, url))

        def close(self, platform_id: str) -> None:
            self.close_calls.append(platform_id)

    monkeypatch.setattr(app_module, "PlatformProfileLoginLauncher", FakeLoginLauncher)
    client = TestClient(create_app(ReceiverConfig(output_dir=tmp_path), automation_enabled=False))
    nonce = client.get("/api/dashboard").json()["managementNonce"]

    response = client.post(
        "/api/automation/platforms/explorerglobal/login",
        headers={"X-WorldGS-Management-Nonce": nonce},
    )

    assert response.status_code == 200
    assert response.json()["platformId"] == "explorerglobal"
    assert response.json()["url"] == "https://3d.explorerglobal.cn/"
    assert response.json()["alreadyOpen"] is False
    assert FakeLoginLauncher.instances[0].open_calls == [("explorerglobal", "https://3d.explorerglobal.cn/")]


def test_login_platform_endpoint_returns_ok_when_login_window_is_already_open(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class FakeLoginLauncher:
        instances: list["FakeLoginLauncher"] = []

        def __init__(self, output_dir: Path) -> None:
            self.output_dir = output_dir
            FakeLoginLauncher.instances.append(self)

        def open(self, platform_id: str, url: str) -> str:
            return "already_open"

        def close(self, platform_id: str) -> None:
            return None

    monkeypatch.setattr(app_module, "PlatformProfileLoginLauncher", FakeLoginLauncher)
    client = TestClient(create_app(ReceiverConfig(output_dir=tmp_path), automation_enabled=False))
    nonce = client.get("/api/dashboard").json()["managementNonce"]

    response = client.post(
        "/api/automation/platforms/explorerglobal/login",
        headers={"X-WorldGS-Management-Nonce": nonce},
    )

    assert response.status_code == 200
    assert response.json()["alreadyOpen"] is True
    assert response.json()["message"] == "知天下登录窗口已经打开，请切换到 Firefox 完成登录。"


def test_login_platform_endpoint_returns_json_error_when_browser_launch_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class FakeLoginLauncher:
        def __init__(self, output_dir: Path) -> None:
            self.output_dir = output_dir

        def open(self, platform_id: str, url: str) -> str:
            raise RuntimeError("未找到内置 Firefox 浏览器资源，请重新安装最新 WorldGS 桌面包。")

        def close(self, platform_id: str) -> None:
            return None

    monkeypatch.setattr(app_module, "PlatformProfileLoginLauncher", FakeLoginLauncher)
    client = TestClient(create_app(ReceiverConfig(output_dir=tmp_path), automation_enabled=False))
    nonce = client.get("/api/dashboard").json()["managementNonce"]

    response = client.post(
        "/api/automation/platforms/explorerglobal/login",
        headers={"X-WorldGS-Management-Nonce": nonce},
    )

    assert response.status_code == 500
    assert response.json()["detail"] == "未找到内置 Firefox 浏览器资源，请重新安装最新 WorldGS 桌面包。"


def test_start_automation_closes_manual_login_browser_before_runner_starts(tmp_path: Path, monkeypatch) -> None:
    class FakeLoginLauncher:
        instances: list["FakeLoginLauncher"] = []

        def __init__(self, output_dir: Path) -> None:
            self.output_dir = output_dir
            self.open_calls: list[tuple[str, str]] = []
            self.close_calls: list[str] = []
            FakeLoginLauncher.instances.append(self)

        def open(self, platform_id: str, url: str) -> None:
            self.open_calls.append((platform_id, url))

        def close(self, platform_id: str) -> None:
            self.close_calls.append(platform_id)

    class FakeRunner:
        instances: list["FakeRunner"] = []

        def __init__(self, store: object, platform: object) -> None:
            self.store = store
            self.platform = platform
            self.started: list[tuple[str, object]] = []
            FakeRunner.instances.append(self)

        def start_background(self, run_id: str, task_context: object) -> None:
            self.started.append((run_id, task_context))

        def has_live_run(self, run_id: str) -> bool:
            return False

        def request_continue(self, run_id: str) -> None:
            return None

        def request_cancel(self, run_id: str) -> None:
            return None

    monkeypatch.setattr(app_module, "PlatformProfileLoginLauncher", FakeLoginLauncher)
    monkeypatch.setattr(app_module, "PlatformAutomationRunner", FakeRunner)
    upload_id = "upload-2"
    task_dir = tmp_path / "2026-06-24" / "job-002_efgh5678"
    dataset = task_dir / "dataset"
    images = dataset / "images"
    scene_dataset = dataset / "sceneDataset"
    images.mkdir(parents=True)
    scene_dataset.mkdir(parents=True)
    for index in range(89):
        (images / f"frame_{index:06d}.jpg").write_bytes(b"jpg")
    for name in ["cameras.txt", "images.txt", "points3D.txt"]:
        (scene_dataset / name).write_text("# colmap\n", encoding="utf-8")
    package = task_dir / "package.zip"
    extracted = task_dir / "extracted"
    extracted.mkdir()
    package.write_bytes(b"zip")
    (task_dir / "upload_report.json").write_text(
        (
            "{"
            f'"ok": true, "uploadId": "{upload_id}", "taskName": "job-002", '
            f'"packagePath": "{package}", "extractedPath": "{extracted}", '
            f'"openPath": "{task_dir}", "sizeBytes": 3'
            "}"
        ),
        encoding="utf-8",
    )
    client = TestClient(create_app(ReceiverConfig(output_dir=tmp_path), automation_enabled=True))
    nonce = client.get("/api/dashboard").json()["managementNonce"]
    login_response = client.post(
        "/api/automation/platforms/explorerglobal/login",
        headers={"X-WorldGS-Management-Nonce": nonce},
    )

    response = client.post(
        "/api/automation/runs",
        headers={"X-WorldGS-Management-Nonce": nonce},
        json={"uploadId": upload_id, "platformId": "explorerglobal"},
    )

    assert login_response.status_code == 200
    assert response.status_code == 200
    assert FakeLoginLauncher.instances[0].close_calls == ["explorerglobal"]
    assert len(FakeRunner.instances[0].started) == 1


def test_open_platform_my_endpoint_uses_shared_profile(tmp_path: Path, monkeypatch) -> None:
    class FakeLoginLauncher:
        instances: list["FakeLoginLauncher"] = []

        def __init__(self, output_dir: Path) -> None:
            self.output_dir = output_dir
            self.open_calls: list[tuple[str, str]] = []
            FakeLoginLauncher.instances.append(self)

        def open(self, platform_id: str, url: str) -> None:
            self.open_calls.append((platform_id, url))

        def close(self, platform_id: str) -> None:
            return None

    monkeypatch.setattr(app_module, "PlatformProfileLoginLauncher", FakeLoginLauncher)
    client = TestClient(create_app(ReceiverConfig(output_dir=tmp_path), automation_enabled=False))
    nonce = client.get("/api/dashboard").json()["managementNonce"]

    response = client.post(
        "/api/automation/platforms/explorerglobal/my",
        headers={"X-WorldGS-Management-Nonce": nonce},
    )

    assert response.status_code == 200
    assert response.json()["url"] == "https://3d.explorerglobal.cn/my"
    assert FakeLoginLauncher.instances[0].open_calls == [("explorerglobal", "https://3d.explorerglobal.cn/my")]


def test_receiver_page_handles_non_json_training_error_responses(tmp_path: Path) -> None:
    client = TestClient(create_app(ReceiverConfig(output_dir=tmp_path), automation_enabled=False))

    response = client.get("/")

    assert response.status_code == 200
    html = response.text
    assert "async function parseJsonResponse(response)" in html
    assert "const text = await response.text();" in html
    assert 'detail: text || `HTTP ${response.status}`' in html
    assert "const payload = await parseJsonResponse(response);" in html


def test_start_automation_returns_json_error_when_login_profile_close_fails(tmp_path: Path, monkeypatch) -> None:
    class FakeLoginLauncher:
        def __init__(self, output_dir: Path) -> None:
            self.output_dir = output_dir

        def open(self, platform_id: str, url: str) -> None:
            return None

        def close(self, platform_id: str) -> None:
            raise RuntimeError("关闭知天下登录窗口失败")

    class FakeRunner:
        def __init__(self, store: object, platform: object) -> None:
            self.store = store
            self.platform = platform

        def start_background(self, run_id: str, task_context: object) -> None:
            return None

        def has_live_run(self, run_id: str) -> bool:
            return False

        def request_continue(self, run_id: str) -> None:
            return None

        def request_cancel(self, run_id: str) -> None:
            return None

    monkeypatch.setattr(app_module, "PlatformProfileLoginLauncher", FakeLoginLauncher)
    monkeypatch.setattr(app_module, "PlatformAutomationRunner", FakeRunner)
    upload_id = "upload-close-error"
    task_dir = tmp_path / "2026-06-24" / "job-close-error_abcd1234"
    dataset = task_dir / "dataset"
    images = dataset / "images"
    scene_dataset = dataset / "sceneDataset"
    images.mkdir(parents=True)
    scene_dataset.mkdir(parents=True)
    for index in range(89):
        (images / f"frame_{index:06d}.jpg").write_bytes(b"jpg")
    for name in ["cameras.txt", "images.txt", "points3D.txt"]:
        (scene_dataset / name).write_text("# colmap\n", encoding="utf-8")
    package = task_dir / "package.zip"
    extracted = task_dir / "extracted"
    extracted.mkdir()
    package.write_bytes(b"zip")
    (task_dir / "upload_report.json").write_text(
        (
            "{"
            f'"ok": true, "uploadId": "{upload_id}", "taskName": "job-close-error", '
            f'"packagePath": "{package}", "extractedPath": "{extracted}", '
            f'"openPath": "{task_dir}", "sizeBytes": 3'
            "}"
        ),
        encoding="utf-8",
    )
    client = TestClient(create_app(ReceiverConfig(output_dir=tmp_path), automation_enabled=True))
    nonce = client.get("/api/dashboard").json()["managementNonce"]

    response = client.post(
        "/api/automation/runs",
        headers={"X-WorldGS-Management-Nonce": nonce},
        json={"uploadId": upload_id, "platformId": "explorerglobal"},
    )

    assert response.status_code == 500
    assert response.json()["detail"] == "关闭知天下登录窗口失败"


def _make_package(tmp_path: Path) -> Path:
    package = tmp_path / "input.zip"
    with zipfile.ZipFile(package, "w") as zip_file:
        zip_file.writestr("worldgs_task/manifest.json", '{"jobId":"job-001","jobName":"room"}')
        zip_file.writestr("worldgs_task/images/frame_000001.jpg", "image")
        zip_file.writestr(
            "worldgs_task/reports/sfm_quality_report.json",
            '{"schemaVersion":1,"sparsePointCount":2,"visualizations":{"coverageHeatmap":{"path":"quality/coverage_heatmap.png"},"residualPlots":[]}}',
        )
        zip_file.writestr("worldgs_task/reports/quality/coverage_heatmap.png", "png")
    return package


def _upload_sample_package(client: TestClient, tmp_path: Path) -> dict[str, object]:
    pair = client.get("/pair").json()
    package = _make_package(tmp_path)
    content = package.read_bytes()
    response = client.post(
        "/upload",
        data={"token": pair["token"], "deviceName": "android-test"},
        files={"file": ("worldgs_job-001.zip", content, "application/zip")},
    )
    assert response.status_code == 200
    return response.json()
