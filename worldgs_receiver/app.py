import io
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import secrets
import time
import uuid
from secrets import compare_digest
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, urlencode, urlsplit

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
import qrcode
import qrcode.image.svg

from worldgs_receiver.analytics import AnalyticsStore
from worldgs_receiver.automation_paths import pointcosm_flow_path
from worldgs_receiver.automation_context import build_task_context
from worldgs_receiver.automation_platforms import load_platform_from_file
from worldgs_receiver.automation_runner import (
    PlatformAutomationRunner,
    PlatformProfileLoginLauncher,
    PointCosmAutomationRunner,
    PointCosmRecorder,
)
from worldgs_receiver.automation_store import (
    AutomationStore,
    create_record_session,
    create_platform_run_summary,
    create_run_summary,
    find_upload_by_id,
    read_run_summary,
    stop_record_session,
    update_run_summary,
)
from worldgs_receiver.config import ReceiverConfig
from worldgs_receiver.diag import DiagStore
from worldgs_receiver.file_sync import FileSyncLimits, FileSyncStore
from worldgs_receiver.local_training import LocalTrainingConfig, LocalTrainingRunner, LocalTrainingStore
from worldgs_receiver.networking import local_lan_addresses
from worldgs_receiver.pairing import PairingStore
from worldgs_receiver.script_registry import ScriptRegistry
from worldgs_receiver.script_runner import ScriptRunner, ScriptRunnerStore
from worldgs_receiver.security import SecurityMonitor
from worldgs_receiver.storage import SavePackageResult, save_package_file
from worldgs_receiver.website_showcase import WebsiteShowcaseStore


MODEL_SHARE_MAX_BYTES = 200 * 1024 * 1024
MODEL_SHARE_ID_PREFIX = "sh_"


def create_app(config: ReceiverConfig, automation_enabled: bool = True) -> FastAPI:
    app = FastAPI(title="WorldGS Receiver")
    app.mount(
        "/static",
        StaticFiles(directory=Path(__file__).resolve().parent / "static"),
        name="static",
    )
    security = HTTPBasic()
    pairing_store = PairingStore(
        ttl_seconds=config.token_ttl_seconds,
        devices_path=config.output_dir / "paired_devices.json",
    )
    analytics_dir = config.analytics_dir or config.output_dir / "analytics"
    analytics_store = AnalyticsStore(event_log=analytics_dir / "events.jsonl")
    security_monitor = SecurityMonitor(root_dir=config.output_dir / "security")
    model_shares_log = analytics_dir / "model_shares.jsonl"
    model_shares_dir = config.output_dir / "model-shares"
    website_root_dir = (config.analytics_dir.parent if config.analytics_dir else config.output_dir) / "website"
    website_showcase_store = WebsiteShowcaseStore(config_path=website_root_dir / "showcase-config.json")
    diag_store = DiagStore(root_dir=config.output_dir / "diag")
    automation_store = AutomationStore(output_dir=config.output_dir)
    local_training_runner = LocalTrainingRunner(
        LocalTrainingStore(config.output_dir),
        LocalTrainingConfig(
            enabled=config.local_training_enabled,
            command=list(config.local_training_command),
            cwd=config.local_training_cwd,
            env=dict(config.local_training_env),
        ),
    )
    script_registry = ScriptRegistry(config.output_dir)
    script_runner = ScriptRunner(ScriptRunnerStore(config.output_dir), script_registry)
    automation_recorder = PointCosmRecorder(config.output_dir, config.pointcosm_base_url)
    automation_runner = PointCosmAutomationRunner(automation_store, pointcosm_flow_path(config.output_dir))
    platform_login_launcher = PlatformProfileLoginLauncher(config.output_dir)
    explorerglobal_platform = load_platform_from_file(
        Path(__file__).resolve().parent / "automation_platform_configs" / "explorerglobal.yaml",
    )
    platforms = {explorerglobal_platform.platform_id: explorerglobal_platform}
    platform_runners = {
        explorerglobal_platform.platform_id: PlatformAutomationRunner(automation_store, explorerglobal_platform),
    }
    file_sync_store = FileSyncStore(
        config.output_dir,
        FileSyncLimits(
            max_file_bytes=config.max_sync_file_bytes,
            max_total_bytes=config.max_sync_total_bytes,
            max_files=config.max_sync_files,
        ),
    )
    management_nonce = secrets.token_urlsafe(32)
    uploads: dict[str, dict[str, object]] = {}
    automation_runs: dict[str, dict[str, object]] = {}
    active_record_session_id: Optional[str] = None
    dashboard_pairing_token: Optional[str] = None

    def require_dashboard_auth(credentials: HTTPBasicCredentials = Depends(security)) -> str:
        username_ok = compare_digest(credentials.username, config.dashboard_username)
        password_ok = compare_digest(credentials.password, config.dashboard_password)
        if not (username_ok and password_ok):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid dashboard credentials",
                headers={"WWW-Authenticate": "Basic"},
            )
        return credentials.username

    def require_management_nonce(request: Request) -> None:
        provided = request.headers.get("X-WorldGS-Management-Nonce", "")
        if not provided or not compare_digest(provided, management_nonce):
            raise HTTPException(status_code=403, detail="invalid management nonce")

    @app.middleware("http")
    async def monitor_security_events(request: Request, call_next):
        response = await call_next(request)
        try:
            security_monitor.inspect_response(
                method=request.method,
                path=request.url.path,
                status_code=response.status_code,
                client_host=request.client.host if request.client else "",
                user_agent=request.headers.get("user-agent", ""),
                query_keys=sorted(set(request.query_params.keys())),
            )
        except Exception:
            pass
        return response

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _receiver_page()

    @app.get("/pair")
    def pair(request: Request) -> dict[str, object]:
        token = pairing_store.create_token()
        scan_url = _scan_url(request=request, config=config, token=token)
        return {
            "computerName": socket.gethostname(),
            "uploadUrl": "/upload",
            "token": token,
            "expiresInSeconds": config.token_ttl_seconds,
            "outputDir": str(config.output_dir),
            "scanUrl": scan_url,
            "lanUrls": [f"http://{address}:{config.port}" for address in local_lan_addresses()],
        }

    @app.get("/qr.svg")
    def qr_svg(data: str) -> Response:
        image = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage)
        stream = io.BytesIO()
        image.save(stream)
        return Response(content=stream.getvalue(), media_type="image/svg+xml")

    @app.get("/api/dashboard")
    def dashboard(request: Request) -> dict[str, object]:
        token, scan_url, expires_in_seconds = get_dashboard_pairing(request)
        return {
            "computerName": socket.gethostname(),
            "outputDir": str(config.output_dir),
            "port": config.port,
            "token": token,
            "expiresInSeconds": expires_in_seconds,
            "scanUrl": scan_url,
            "qrUrl": f"/qr.svg?data={quote(scan_url, safe='')}",
            "lanUrls": [f"http://{address}:{config.port}" for address in local_lan_addresses()],
            "uploads": _recent_uploads(
                config.output_dir,
                uploads,
                request=request,
                config=config,
                automation_training_by_upload=_automation_training_by_upload(config.output_dir),
                local_training_by_upload=_local_training_by_upload(config.output_dir),
                script_runs_by_upload=_script_runs_by_upload(config.output_dir),
            ),
            "scripts": script_registry.list_scripts(),
            "managementNonce": management_nonce,
            "automation": {
                "enabled": automation_enabled,
                "activeRecordSessionId": active_record_session_id,
                "runs": list(automation_runs.values()),
            },
        }

    @app.get("/api/healthz")
    def healthz() -> dict[str, object]:
        return {
            "ok": True,
            "status": "ready",
            "port": config.port,
            "outputDir": str(config.output_dir),
        }

    def get_dashboard_pairing(request: Request) -> tuple[str, str, int]:
        nonlocal dashboard_pairing_token
        now = time.time()
        expires_at = pairing_store.expires_at(dashboard_pairing_token or "")
        if expires_at is None or expires_at <= now:
            dashboard_pairing_token = pairing_store.create_token()
            expires_at = pairing_store.expires_at(dashboard_pairing_token) or (now + config.token_ttl_seconds)
        scan_url = _scan_url(request=request, config=config, token=dashboard_pairing_token)
        return dashboard_pairing_token, scan_url, max(0, int(expires_at - now))

    @app.post("/api/open-path")
    async def open_path(payload: dict[str, str], request: Request) -> dict[str, object]:
        require_management_nonce(request)
        requested_path = payload.get("path", "")
        target = Path(requested_path).expanduser().resolve()
        output_root = config.output_dir.expanduser().resolve()
        if not _is_relative_to(target, output_root):
            raise HTTPException(status_code=403, detail="path outside output directory")
        if not target.exists():
            raise HTTPException(status_code=404, detail="path not found")
        _open_in_file_manager(target)
        return {"ok": True}

    @app.post("/upload")
    async def upload(
        token: str = Form(...),
        sha256: Optional[str] = Form(None),
        deviceName: str = Form("unknown"),
        file: UploadFile = File(...),
    ) -> dict[str, object]:
        if not pairing_store.consume_token(token):
            raise HTTPException(status_code=403, detail="invalid or expired token")

        temp_dir = config.output_dir / ".tmp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        temp_path = temp_dir / f"{uuid.uuid4().hex}.zip"
        digest = hashlib.sha256()
        size_bytes = 0
        try:
            with temp_path.open("wb") as output:
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break
                    size_bytes += len(chunk)
                    if size_bytes > config.max_upload_bytes:
                        raise HTTPException(status_code=413, detail="upload too large")
                    digest.update(chunk)
                    output.write(chunk)
            actual_sha256 = digest.hexdigest()
            if sha256 and sha256.lower() != actual_sha256:
                raise HTTPException(status_code=400, detail="sha256 mismatch")
            result = save_package_file(
                output_dir=config.output_dir,
                filename=file.filename or "worldgs_package.zip",
                source_path=temp_path,
                sha256=actual_sha256,
                size_bytes=size_bytes,
                device_name=deviceName,
            )
        except HTTPException:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"invalid package: {exc}") from exc
        finally:
            temp_path.unlink(missing_ok=True)

        status = _status_from_result(result)
        uploads[result.upload_id] = status
        return status

    @app.post("/api/sync/sessions")
    def create_sync_session(payload: dict[str, object]) -> dict[str, object]:
        device = pairing_store.device_for_token(str(payload.get("deviceToken") or ""))
        if device is None:
            token = str(payload.get("token") or "")
            device = pairing_store.exchange_token_for_device(token)
        if device is None:
            raise HTTPException(status_code=403, detail="invalid or expired token")
        try:
            session = file_sync_store.create_session(
                job_id=str(payload.get("jobId") or ""),
                task_name=str(payload.get("taskName") or "WorldGS 数据集"),
                files=list(payload.get("files") or []),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "sessionId": session.session_id,
            "sessionToken": session.session_token,
            "deviceId": device["deviceId"],
            "deviceToken": device["deviceToken"],
            "status": "receiving",
            "datasetPath": str(session.dataset_dir),
        }

    @app.get("/api/sync/sessions/{session_id}")
    def get_sync_session(session_id: str, sessionToken: str) -> dict[str, object]:
        try:
            return file_sync_store.read_status(session_id, sessionToken)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.put("/api/sync/sessions/{session_id}/files")
    async def upload_sync_file(
        session_id: str,
        sessionToken: str = Form(...),
        relativePath: str = Form(...),
        sha256: str = Form(""),
        sizeBytes: int = Form(...),
        file: UploadFile = File(...),
    ) -> dict[str, object]:
        try:
            return await file_sync_store.save_file(
                session_id=session_id,
                session_token=sessionToken,
                relative_path=relativePath,
                expected_sha256=sha256,
                expected_size_bytes=sizeBytes,
                file=file,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/sync/sessions/{session_id}/finalize")
    def finalize_sync_session(session_id: str, payload: dict[str, str]) -> dict[str, object]:
        try:
            report = file_sync_store.finalize(session_id, payload.get("sessionToken") or "")
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        status_payload = {
            "uploadId": report["uploadId"],
            "taskName": report["taskName"],
            "createdAt": Path(str(report["openPath"])).stat().st_mtime,
            "sha256": report.get("sha256", ""),
            "sizeBytes": report.get("sizeBytes", 0),
            "fileCount": report.get("fileCount", 0),
            "imageCount": report.get("imageCount", 0),
            "savePath": report.get("packagePath", ""),
            "extractedPath": report.get("extractedPath", ""),
            "datasetPath": report.get("datasetPath", report.get("packagePath", "")),
            "reportPath": str(Path(str(report["openPath"])) / "upload_report.json"),
            "openPath": report["openPath"],
            "ok": True,
        }
        uploads[str(report["uploadId"])] = status_payload
        return status_payload

    @app.get("/uploads/{upload_id}")
    def get_upload(upload_id: str) -> dict[str, object]:
        status = uploads.get(upload_id)
        if status is None:
            raise HTTPException(status_code=404, detail="upload not found")
        return status

    @app.get("/api/uploads/{upload_id}/sfm-quality/{filename}")
    def download_sfm_quality_image(upload_id: str, filename: str) -> FileResponse:
        try:
            upload = find_upload_by_id(config.output_dir, uploads, upload_id)
            image_path = _find_sfm_quality_image(config.output_dir, upload, filename)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return FileResponse(image_path, media_type="image/png", filename=image_path.name)

    @app.post("/api/uploads/{upload_id}/result")
    async def upload_model_result(
        upload_id: str,
        request: Request,
        file: UploadFile = File(...),
    ) -> dict[str, object]:
        require_management_nonce(request)
        try:
            upload = find_upload_by_id(config.output_dir, uploads, upload_id)
            model_result = _save_model_result(config.output_dir, upload, file)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        payload = _model_result_payload(request, config, upload_id, model_result)
        return {"ok": True, "modelResult": payload}

    @app.get("/api/uploads/{upload_id}/result/download")
    def download_model_result(upload_id: str) -> FileResponse:
        try:
            upload = find_upload_by_id(config.output_dir, uploads, upload_id)
            model_result = _read_model_result(config.output_dir, upload)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        path = Path(str(model_result["path"]))
        if not path.is_file():
            raise HTTPException(status_code=404, detail="model result not found")
        return FileResponse(
            path,
            media_type="application/octet-stream",
            filename=str(model_result["fileName"]),
        )

    @app.delete("/api/uploads/{upload_id}/result")
    def delete_model_result(upload_id: str, request: Request) -> dict[str, bool]:
        require_management_nonce(request)
        try:
            upload = find_upload_by_id(config.output_dir, uploads, upload_id)
            _delete_model_result(config.output_dir, upload)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True}

    @app.delete("/api/uploads/{upload_id}")
    def delete_upload(upload_id: str, request: Request) -> dict[str, bool]:
        require_management_nonce(request)
        try:
            upload = find_upload_by_id(config.output_dir, uploads, upload_id)
            _delete_upload_task(config.output_dir, upload)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        uploads.pop(upload_id, None)
        return {"ok": True}

    @app.post("/api/local-training/runs")
    def local_training_start(payload: dict[str, str], request: Request) -> dict[str, object]:
        require_management_nonce(request)
        upload_id = payload.get("uploadId") or ""
        preset = payload.get("preset") or "fast"
        try:
            return local_training_runner.start(upload_id=upload_id, preset=preset)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/local-training/runs/{training_run_id}")
    def local_training_status(training_run_id: str) -> dict[str, object]:
        try:
            return local_training_runner.read(training_run_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/local-training/runs/{training_run_id}/cancel")
    def local_training_cancel(training_run_id: str, request: Request) -> dict[str, object]:
        require_management_nonce(request)
        try:
            return local_training_runner.cancel(training_run_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/scripts")
    def list_scripts() -> dict[str, object]:
        return {"scripts": script_registry.list_scripts()}

    @app.post("/api/scripts")
    async def create_script(
        request: Request,
        name: str = Form(...),
        description: str = Form(""),
        scriptType: str = Form("generic"),
        enabled: str = Form("true"),
        entryFile: str = Form(""),
        customActionsJson: str = Form("[]"),
        file: UploadFile = File(...),
    ) -> dict[str, object]:
        require_management_nonce(request)
        try:
            content = await file.read()
            script = script_registry.create_script(
                name=name,
                description=description,
                script_type=scriptType,
                filename=file.filename or "",
                content=content,
                entry_file=entryFile or None,
                custom_actions=_parse_custom_actions_form(customActionsJson),
                enabled=_parse_form_bool(enabled),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            await file.close()
        return {"ok": True, "script": script}

    @app.put("/api/scripts/{script_id}")
    async def update_script(
        script_id: str,
        request: Request,
        name: Optional[str] = Form(None),
        description: Optional[str] = Form(None),
        scriptType: Optional[str] = Form(None),
        enabled: Optional[str] = Form(None),
        entryFile: Optional[str] = Form(None),
        customActionsJson: Optional[str] = Form(None),
        file: Optional[UploadFile] = File(None),
    ) -> dict[str, object]:
        require_management_nonce(request)
        try:
            script = script_registry.update_script(
                script_id,
                name=name,
                description=description,
                script_type=scriptType,
                enabled=None if enabled is None else _parse_form_bool(enabled),
                filename=(file.filename or "") if file and file.filename else None,
                content=(await file.read()) if file and file.filename else None,
                entry_file=entryFile,
                custom_actions=None if customActionsJson is None else _parse_custom_actions_form(customActionsJson),
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            if file is not None:
                await file.close()
        return {"ok": True, "script": script}

    @app.delete("/api/scripts/{script_id}")
    def delete_script(script_id: str, request: Request) -> dict[str, object]:
        require_management_nonce(request)
        try:
            script_registry.delete_script(script_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True}

    @app.post("/api/script-runs")
    def create_script_run(payload: dict[str, str], request: Request) -> dict[str, object]:
        require_management_nonce(request)
        upload_id = payload.get("uploadId") or ""
        script_id = payload.get("scriptId") or ""
        action_id = payload.get("actionId") or None
        try:
            return script_runner.start(script_id=script_id, upload_id=upload_id, action_id=action_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/script-runs/{script_run_id}")
    def script_run_status(script_run_id: str) -> dict[str, object]:
        try:
            return script_runner.read(script_run_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/script-runs/{script_run_id}/cancel")
    def cancel_script_run(script_run_id: str, request: Request) -> dict[str, object]:
        require_management_nonce(request)
        try:
            return script_runner.cancel(script_run_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/automation/pointcosm/record/start")
    def pointcosm_record_start(request: Request) -> dict[str, object]:
        require_management_nonce(request)
        nonlocal active_record_session_id
        record = create_record_session(automation_store, config.pointcosm_base_url)
        if automation_enabled:
            try:
                automation_recorder.start(record.record_session_id)
            except RuntimeError as exc:
                raise HTTPException(status_code=500, detail=str(exc)) from exc
        active_record_session_id = record.record_session_id
        return {
            "recordSessionId": record.record_session_id,
            "status": "recording",
            "url": config.pointcosm_base_url,
        }

    @app.post("/api/automation/pointcosm/record/stop")
    def pointcosm_record_stop(payload: dict[str, str], request: Request) -> dict[str, object]:
        require_management_nonce(request)
        nonlocal active_record_session_id
        record_session_id = payload.get("recordSessionId") or ""
        try:
            if automation_enabled:
                automation_recorder.stop(record_session_id)
            record = stop_record_session(automation_store, record_session_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        active_record_session_id = None
        return {
            "recordSessionId": record.record_session_id,
            "status": "completed",
            "recordDir": str(record.record_dir),
        }

    @app.get("/api/automation/platforms")
    def automation_platforms() -> dict[str, object]:
        return {
            "platforms": [
                {
                    "platformId": platform.platform_id,
                    "displayName": platform.display_name,
                    "entryUrl": platform.entry_url,
                    "minImageCountExclusive": platform.min_image_count_exclusive,
                }
                for platform in platforms.values()
            ],
        }

    @app.post("/api/automation/platforms/{platform_id}/login")
    def automation_platform_login(platform_id: str, request: Request) -> dict[str, object]:
        require_management_nonce(request)
        platform_config = platforms.get(platform_id)
        if platform_config is None:
            raise HTTPException(status_code=404, detail=f"未知训练平台：{platform_id}")
        login_url = _platform_login_url(platform_config.entry_url)
        try:
            open_status = platform_login_launcher.open(platform_id, login_url)
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {
            "ok": True,
            "platformId": platform_id,
            "displayName": platform_config.display_name,
            "url": login_url,
            "alreadyOpen": open_status == "already_open",
            "message": "知天下登录窗口已经打开，请切换到 Firefox 完成登录。"
            if open_status == "already_open"
            else "已打开知天下登录窗口，请在 Firefox 中手动登录。",
        }

    @app.post("/api/automation/platforms/{platform_id}/my")
    def automation_platform_open_my(platform_id: str, request: Request) -> dict[str, object]:
        require_management_nonce(request)
        platform_config = platforms.get(platform_id)
        if platform_config is None:
            raise HTTPException(status_code=404, detail=f"未知训练平台：{platform_id}")
        my_url = _platform_path_url(platform_config.entry_url, "/my")
        try:
            open_status = platform_login_launcher.open(platform_id, my_url)
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {
            "ok": True,
            "platformId": platform_id,
            "displayName": platform_config.display_name,
            "url": my_url,
            "alreadyOpen": open_status == "already_open",
        }

    def start_platform_run(payload: dict[str, str], request: Request) -> dict[str, object]:
        require_management_nonce(request)
        upload_id = payload.get("uploadId") or ""
        platform_id = payload.get("platformId") or config.default_automation_platform
        platform_config = platforms.get(platform_id)
        if platform_config is None:
            raise HTTPException(status_code=400, detail=f"未知训练平台：{platform_id}")
        try:
            upload = find_upload_by_id(config.output_dir, uploads, upload_id)
            task_context = build_task_context(config.output_dir, upload, platform_config)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        existing_summary = _find_active_run_for_upload(
            automation_store,
            upload_id,
            platform_id,
            automation_enabled=automation_enabled,
            platform_runners=platform_runners,
            legacy_runner=automation_runner,
        )
        if existing_summary:
            automation_runs[str(existing_summary["automationRunId"])] = existing_summary
            return {
                "automationRunId": existing_summary["automationRunId"],
                "status": existing_summary["status"],
                "platformId": existing_summary.get("platformId"),
                "message": existing_summary.get("message"),
                "existing": True,
            }

        summary = create_platform_run_summary(automation_store, task_context)
        if not automation_enabled:
            update_run_summary(
                automation_store,
                summary.automation_run_id,
                status="queued",
                message="自动化已排队，等待执行。",
            )
        else:
            try:
                platform_login_launcher.close(platform_id)
                platform_runners[platform_id].start_background(summary.automation_run_id, task_context)
            except RuntimeError as exc:
                update_run_summary(
                    automation_store,
                    summary.automation_run_id,
                    status="failed",
                    message=str(exc),
                    error=str(exc),
                )
                raise HTTPException(status_code=500, detail=str(exc)) from exc
        status_payload = read_run_summary(automation_store, summary.automation_run_id)
        automation_runs[summary.automation_run_id] = status_payload
        return {
            "automationRunId": summary.automation_run_id,
            "status": status_payload["status"],
            "platformId": status_payload.get("platformId"),
            "message": status_payload.get("message"),
            "existing": False,
        }

    @app.post("/api/automation/runs")
    def automation_run_start(payload: dict[str, str], request: Request) -> dict[str, object]:
        return start_platform_run(payload, request)

    @app.get("/api/automation/runs/{automation_run_id}")
    def automation_run_status(automation_run_id: str) -> dict[str, object]:
        try:
            summary = read_run_summary(automation_store, automation_run_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        automation_runs[automation_run_id] = summary
        return _automation_run_status_payload(summary)

    @app.post("/api/automation/runs/{automation_run_id}/continue")
    def automation_run_continue(automation_run_id: str, request: Request) -> dict[str, object]:
        return continue_automation_run(automation_run_id, request)

    @app.post("/api/automation/runs/{automation_run_id}/cancel")
    def automation_run_cancel(automation_run_id: str, request: Request) -> dict[str, object]:
        return cancel_automation_run(automation_run_id, request)

    @app.post("/api/automation/pointcosm/runs")
    def pointcosm_run_start(payload: dict[str, str], request: Request) -> dict[str, object]:
        payload = dict(payload)
        payload["platformId"] = config.default_automation_platform
        return start_platform_run(payload, request)

    @app.get("/api/automation/pointcosm/runs/{automation_run_id}")
    def pointcosm_run_status(automation_run_id: str) -> dict[str, object]:
        try:
            summary = read_run_summary(automation_store, automation_run_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        automation_runs[automation_run_id] = summary
        return _automation_run_status_payload(summary)

    @app.post("/api/automation/pointcosm/runs/{automation_run_id}/continue")
    def pointcosm_run_continue(automation_run_id: str, request: Request) -> dict[str, object]:
        return continue_automation_run(automation_run_id, request)

    def continue_automation_run(automation_run_id: str, request: Request) -> dict[str, object]:
        require_management_nonce(request)
        try:
            read_run_summary(automation_store, automation_run_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if automation_enabled:
            summary = read_run_summary(automation_store, automation_run_id)
            platform_id = str(summary.get("platformId") or "")
            runner = platform_runners.get(platform_id)
            if runner:
                runner.request_continue(automation_run_id)
            else:
                automation_runner.request_continue(automation_run_id)
        return {"ok": True}

    @app.post("/api/automation/pointcosm/runs/{automation_run_id}/cancel")
    def pointcosm_run_cancel(automation_run_id: str, request: Request) -> dict[str, object]:
        return cancel_automation_run(automation_run_id, request)

    def cancel_automation_run(automation_run_id: str, request: Request) -> dict[str, object]:
        require_management_nonce(request)
        try:
            read_run_summary(automation_store, automation_run_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if automation_enabled:
            summary = read_run_summary(automation_store, automation_run_id)
            platform_id = str(summary.get("platformId") or "")
            runner = platform_runners.get(platform_id)
            if runner:
                runner.request_cancel(automation_run_id)
            else:
                automation_runner.request_cancel(automation_run_id)
        else:
            update_run_summary(automation_store, automation_run_id, status="cancelled")
        automation_runs[automation_run_id] = read_run_summary(automation_store, automation_run_id)
        return {"ok": True}

    @app.post("/api/track")
    async def track(payload: dict[str, Any], request: Request) -> dict[str, object]:
        client_host = request.client.host if request.client else ""
        user_agent = request.headers.get("user-agent", "")
        return analytics_store.record(payload, client_host=client_host, user_agent=user_agent)

    @app.get("/api/analytics/summary")
    def analytics_summary(_: str = Depends(require_dashboard_auth)) -> dict[str, object]:
        return analytics_store.summary()

    @app.get("/api/security/summary")
    def security_summary(days: int = 7, _: str = Depends(require_dashboard_auth)) -> dict[str, object]:
        if days < 1 or days > 30:
            raise HTTPException(status_code=400, detail="days must be between 1 and 30")
        return security_monitor.summary(days=days)

    @app.post("/api/model-shares")
    async def create_model_share(
        request: Request,
        title: str = Form(...),
        description: str = Form(""),
        format: str = Form("sog"),
        source_format: str = Form("sog"),
        device_model: str = Form(""),
        model: UploadFile = File(...),
        cover: Optional[UploadFile] = File(None),
    ) -> dict[str, object]:
        clean_title = _validate_model_share_text(title, "title", 80, required=True)
        clean_description = _validate_model_share_text(description, "description", 500, required=False)
        clean_format = (format or "").strip().lower()
        clean_source_format = (source_format or "sog").strip().lower()
        clean_device_model = _validate_model_share_text(device_model, "device_model", 80, required=False)
        if clean_format not in {"sog", "ply"}:
            raise HTTPException(status_code=415, detail="only sog and ply model shares are supported")
        filename = Path(model.filename or "").name.lower()
        source_extension = Path(filename).suffix.lower().lstrip(".")
        if source_extension not in {"sog", "ply"}:
            raise HTTPException(status_code=415, detail="model file must use .sog or .ply extension")
        if clean_format != source_extension:
            clean_format = source_extension

        share_id = _new_model_share_id()
        share_dir = model_shares_dir / share_id
        share_dir.mkdir(parents=True, exist_ok=False)
        uploaded_path = share_dir / f"source.{source_extension}"
        model_path = uploaded_path
        sha256 = hashlib.sha256()
        total_bytes = 0
        try:
            with uploaded_path.open("wb") as output:
                while True:
                    chunk = await model.read(1024 * 1024)
                    if not chunk:
                        break
                    total_bytes += len(chunk)
                    if total_bytes > MODEL_SHARE_MAX_BYTES:
                        raise HTTPException(status_code=413, detail="model file exceeds 200MB limit")
                    sha256.update(chunk)
                    output.write(chunk)
        except Exception:
            shutil.rmtree(share_dir, ignore_errors=True)
            raise
        finally:
            await model.close()

        conversion_error = None
        served_format = "sog"
        if source_extension == "ply":
            model_path = share_dir / "model.sog"
            try:
                _convert_model_share_ply_to_sog(uploaded_path, model_path)
            except HTTPException as exc:
                conversion_error = str(exc.detail)
                compressed_path = share_dir / "model.compressed.ply"
                try:
                    _convert_model_share_ply_to_compressed_ply(uploaded_path, compressed_path)
                    served_format = "compressed.ply"
                    model_path = compressed_path
                except HTTPException as fallback_exc:
                    conversion_error = f"{conversion_error}; compressed ply fallback failed: {fallback_exc.detail}"
                    served_format = "ply"
                    model_path = share_dir / "model.ply"
                    shutil.copyfile(uploaded_path, model_path)
        else:
            model_path = share_dir / "model.sog"
            if uploaded_path != model_path:
                shutil.copyfile(uploaded_path, model_path)

        cover_url = None
        if cover and cover.filename:
            cover_url = await _save_model_share_cover(cover, share_dir, share_id, request)

        now = _utc_timestamp()
        asset_url = f"{_request_public_base_url(request)}/uploads/model-shares/{share_id}/model.{served_format}"
        record = {
            "id": share_id,
            "title": clean_title,
            "description": clean_description,
            "status": "ready",
            "format": served_format,
            "source_format": clean_source_format or source_extension,
            "device_model": clean_device_model,
            "conversion_error": conversion_error,
            "asset_path": str(model_path),
            "asset_url": asset_url,
            "cover_url": cover_url,
            "file_size_bytes": total_bytes,
            "checksum_sha256": sha256.hexdigest(),
            "created_at": now,
            "updated_at": now,
        }
        _append_model_share_record(model_shares_log, record)
        return {
            "id": share_id,
            "url": f"{_request_public_base_url(request)}/share/{share_id}",
            "status": "ready",
        }

    @app.get("/api/model-shares/{share_id}")
    def get_model_share(share_id: str) -> dict[str, object]:
        record = _find_model_share_record(model_shares_log, share_id)
        if not record or record.get("status") == "deleted":
            raise HTTPException(status_code=404, detail="model share not found")
        return _public_model_share_record(record)

    @app.get("/api/website/showcase-config")
    def website_showcase_config() -> dict[str, object]:
        return website_showcase_store.read()

    @app.put("/api/website/showcase-config")
    def update_website_showcase_config(payload: dict[str, Any], _: str = Depends(require_dashboard_auth)) -> dict[str, object]:
        try:
            return website_showcase_store.update(payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/diag/v1/events")
    async def diag_event(payload: dict[str, Any], request: Request) -> dict[str, object]:
        client_host = request.client.host if request.client else ""
        user_agent = request.headers.get("user-agent", "")
        return diag_store.record_event(payload, client_host=client_host, user_agent=user_agent)

    @app.get("/api/diag/v1/summary")
    def diag_summary(days: int = 7, _: str = Depends(require_dashboard_auth)) -> dict[str, object]:
        if days < 1 or days > 30:
            raise HTTPException(status_code=400, detail="days must be between 1 and 30")
        return diag_store.summary(days=days)

    @app.post("/api/diag/v1/issues")
    async def diag_issue(meta: str = Form(...), file: UploadFile = File(...)) -> dict[str, object]:
        try:
            payload = json.loads(meta)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="invalid issue meta") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="invalid issue meta")
        return diag_store.record_issue(payload, file.file, file.filename or "issue.zip")

    @app.get("/api/diag/v1/issues/{issue_id}/download")
    def diag_issue_download(issue_id: str, _: str = Depends(require_dashboard_auth)) -> FileResponse:
        issue_file = diag_store.issue_file(issue_id)
        if issue_file is None:
            raise HTTPException(status_code=404, detail="issue not found")
        return FileResponse(
            issue_file,
            media_type="application/zip",
            filename=f"{issue_id}.zip",
        )

    return app


def _status_from_result(result: SavePackageResult) -> dict[str, object]:
    return {
        "uploadId": result.upload_id,
        "taskName": _task_name_from_path(result.package_path.parent),
        "createdAt": result.package_path.stat().st_mtime,
        "sha256": result.sha256,
        "sizeBytes": result.size_bytes,
        "fileCount": 0,
        "imageCount": 0,
        "savePath": str(result.package_path),
        "extractedPath": str(result.extracted_dir),
        "reportPath": str(result.report_path),
        "openPath": str(result.package_path.parent),
        "ok": True,
    }


def _automation_run_status_payload(summary: dict[str, object]) -> dict[str, object]:
    return {
        "automationRunId": summary["automationRunId"],
        "status": summary["status"],
        "currentStepId": summary.get("currentStepId"),
        "platformId": summary.get("platformId"),
        "platformName": summary.get("platformName"),
        "message": summary.get("message"),
        "latestScreenshot": summary.get("latestScreenshot"),
    }


def _find_active_run_for_upload(
    store: AutomationStore,
    upload_id: str,
    platform_id: str,
    *,
    automation_enabled: bool,
    platform_runners: dict[str, PlatformAutomationRunner],
    legacy_runner: PointCosmAutomationRunner,
) -> Optional[dict[str, object]]:
    active_statuses = {"queued", "running", "paused"}
    runs_root = store.output_dir / "automations" / "pointcosm" / "runs"
    if not runs_root.is_dir():
        return None
    candidates: list[dict[str, object]] = []
    for summary_file in runs_root.glob("*/summary.json"):
        try:
            summary = json.loads(summary_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if summary.get("uploadId") != upload_id:
            continue
        if summary.get("platformId") != platform_id:
            continue
        if summary.get("status") not in active_statuses:
            continue
        candidates.append(summary)
    if not candidates:
        return None
    candidates.sort(key=lambda item: str(item.get("startedAt") or ""), reverse=True)
    for summary in candidates:
        if not automation_enabled:
            return summary
        automation_run_id = str(summary.get("automationRunId") or "")
        runner = platform_runners.get(platform_id) if platform_id else None
        if runner is not None and runner.has_live_run(automation_run_id):
            return summary
        if not platform_id and legacy_runner.has_live_run(automation_run_id):
            return summary
        update_run_summary(
            store,
            automation_run_id,
            status="failed",
            error="检测到残留的自动化运行状态，已忽略并重新启动。",
            message="检测到残留的自动化运行状态，已忽略并重新启动。",
        )
    return None


def _platform_login_url(entry_url: str) -> str:
    parsed = urlsplit(entry_url)
    if not parsed.scheme or not parsed.netloc:
        return entry_url
    return f"{parsed.scheme}://{parsed.netloc}/"


def _platform_path_url(entry_url: str, path: str) -> str:
    parsed = urlsplit(entry_url)
    if not parsed.scheme or not parsed.netloc:
        return entry_url
    normalized = path if path.startswith("/") else f"/{path}"
    return f"{parsed.scheme}://{parsed.netloc}{normalized}"


def _receiver_page() -> str:
    return """<!doctype html>
<html class="light" lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>WorldGS Receiver</title>
  <link href="https://fonts.googleapis.com/css2?family=Geist:wght@100..900&family=Geist+Mono:wght@100..900&display=swap" rel="stylesheet">
  <link href="https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined:wght,FILL@100..700,0..1&display=swap" rel="stylesheet">
  <style>
    :root {
      --surface: #f8f9ff;
      --surface-low: #eff4ff;
      --surface-card: #ffffff;
      --surface-variant: #d3e4fe;
      --primary: #005c86;
      --primary-container: #0e76a8;
      --secondary: #006a61;
      --secondary-container: #86f2e4;
      --error: #ba1a1a;
      --error-container: #ffdad6;
      --outline: #707880;
      --outline-variant: #bfc7d0;
      --on-surface: #0b1c30;
      --on-surface-variant: #40484f;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      height: 100vh;
      overflow: hidden;
      background: var(--surface);
      color: var(--on-surface);
      font-family: Geist, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    .material-symbols-outlined {
      font-family: "Material Symbols Outlined";
      font-weight: normal;
      font-style: normal;
      font-size: 22px;
      line-height: 1;
      letter-spacing: normal;
      text-transform: none;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 1em;
      height: 1em;
      min-width: 1em;
      max-width: 1em;
      overflow: hidden;
      flex: 0 0 auto;
      white-space: nowrap;
      direction: ltr;
      -webkit-font-feature-settings: "liga";
      -webkit-font-smoothing: antialiased;
      font-variation-settings: "FILL" 0, "wght" 450, "GRAD" 0, "opsz" 24;
    }
    .shell {
      display: grid;
      grid-template-columns: minmax(420px, 1fr) minmax(460px, 1fr);
      height: 100vh;
    }
    .scan-pane {
      position: relative;
      display: flex;
      align-items: center;
      justify-content: center;
      border-right: 1px solid rgba(191, 199, 208, 0.45);
      background: #f8f9ff;
    }
    .brand {
      position: absolute;
      top: 32px;
      left: 32px;
      display: flex;
      align-items: center;
      gap: 12px;
    }
    .brand-mark {
      width: 36px;
      height: 36px;
      object-fit: contain;
      display: block;
    }
    .brand-title {
      font-size: 20px;
      line-height: 24px;
      font-weight: 700;
      letter-spacing: -0.02em;
    }
    .brand-subtitle {
      margin-top: -2px;
      font-size: 12px;
      color: rgba(64, 72, 79, 0.65);
      font-weight: 600;
    }
    .scan-card {
      width: min(380px, 78%);
      display: flex;
      flex-direction: column;
      align-items: center;
      text-align: center;
    }
    h1 {
      margin: 0;
      font-size: 24px;
      line-height: 30px;
      letter-spacing: -0.02em;
    }
    .lead {
      margin: 6px 0 34px;
      color: var(--on-surface-variant);
      font-size: 14px;
      font-weight: 600;
    }
    .qr-wrap {
      position: relative;
      width: 250px;
      height: 250px;
      border-radius: 16px;
      display: grid;
      place-items: center;
      background: linear-gradient(135deg, #ffffff 0%, #eff4ff 100%);
      box-shadow: 0 18px 42px rgba(11, 28, 48, 0.04);
    }
    .qr-wrap img {
      width: 100%;
      height: 100%;
      display: block;
      transition: filter 160ms ease, opacity 160ms ease;
    }
    .result-sync-modal-open .qr-wrap img {
      filter: blur(10px);
      opacity: 0.24;
    }
    .refresh-button {
      margin-top: 30px;
      border: 0;
      background: transparent;
      color: var(--on-surface-variant);
      display: inline-flex;
      align-items: center;
      gap: 8px;
      font-weight: 700;
      cursor: pointer;
      padding: 8px 12px;
      border-radius: 10px;
    }
    .refresh-button:hover { background: rgba(211, 228, 254, 0.42); color: var(--on-surface); }
    .token {
      margin-top: 14px;
      font-family: "Geist Mono", monospace;
      font-size: 12px;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      color: rgba(64, 72, 79, 0.62);
    }
    .link-box {
      margin-top: 18px;
      width: 100%;
      padding: 11px 12px;
      border-radius: 12px;
      background: rgba(239, 244, 255, 0.72);
      border: 1px solid rgba(191, 199, 208, 0.45);
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 8px;
      align-items: center;
    }
    .scan-url {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      font-family: "Geist Mono", monospace;
      font-size: 11px;
      color: var(--on-surface-variant);
      text-align: left;
    }
    .copy-button {
      border: 1px solid rgba(191, 199, 208, 0.7);
      background: #ffffff;
      color: var(--on-surface);
      border-radius: 9px;
      height: 30px;
      padding: 0 10px;
      font-weight: 700;
      cursor: pointer;
    }
    .tasks-pane {
      height: 100vh;
      overflow-y: auto;
      background: var(--surface-low);
      padding: 32px;
    }
    .tasks-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      margin: 0 auto 28px;
      max-width: 620px;
    }
    .tasks-title {
      font-size: 22px;
      line-height: 28px;
      font-weight: 700;
      letter-spacing: -0.01em;
    }
    .header-actions {
      display: inline-flex;
      align-items: center;
      gap: 10px;
    }
    .summary-pill {
      border-radius: 999px;
      background: rgba(134, 242, 228, 0.35);
      color: var(--secondary);
      font-size: 12px;
      font-weight: 800;
      padding: 6px 12px;
    }
    .record-button {
      border: 1px solid rgba(191, 199, 208, 0.85);
      background: var(--surface-card);
      color: var(--on-surface);
      border-radius: 999px;
      padding: 7px 13px;
      font-size: 12px;
      font-weight: 850;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      cursor: pointer;
    }
    .record-button.recording {
      background: var(--error-container);
      border-color: rgba(186, 26, 26, 0.24);
      color: var(--error);
    }
    .task-list {
      max-width: 620px;
      margin: 0 auto;
      display: flex;
      flex-direction: column;
      gap: 18px;
    }
    .automation-panel {
      max-width: 620px;
      margin: 0 auto 18px;
      border: 1px solid rgba(191, 199, 208, 0.72);
      border-radius: 16px;
      background: rgba(255, 255, 255, 0.72);
      padding: 14px 16px;
      display: none;
    }
    .automation-panel.show { display: block; }
    .automation-title {
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 14px;
      font-weight: 850;
      margin-bottom: 8px;
    }
    .automation-message {
      color: var(--on-surface-variant);
      font-size: 12px;
      font-weight: 700;
      line-height: 18px;
      word-break: break-word;
    }
    .automation-actions {
      margin-top: 12px;
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }
    .automation-actions button {
      border: 1px solid rgba(191, 199, 208, 0.85);
      background: var(--surface-low);
      color: var(--on-surface);
      border-radius: 10px;
      padding: 7px 10px;
      font-size: 12px;
      font-weight: 800;
      cursor: pointer;
    }
    .task-card {
      border: 1px solid rgba(191, 199, 208, 0.85);
      border-radius: 16px;
      background: var(--surface-card);
      padding: 16px;
      display: grid;
      grid-template-columns: 52px 1fr minmax(190px, auto);
      gap: 16px;
      align-items: stretch;
      cursor: pointer;
      transition: border-color 150ms ease, transform 150ms ease, background 150ms ease;
    }
    .task-card:hover {
      border-color: rgba(112, 120, 128, 0.86);
      transform: translateY(-1px);
    }
    .task-card.uploading {
      grid-template-columns: 52px 1fr;
    }
    .task-icon {
      width: 46px;
      height: 46px;
      border-radius: 11px;
      display: grid;
      place-items: center;
      background: rgba(14, 118, 168, 0.12);
      color: var(--primary);
      align-self: center;
    }
    .task-icon.success {
      background: rgba(134, 242, 228, 0.28);
      color: var(--secondary);
    }
    .task-main {
      min-width: 0;
      align-self: center;
    }
    .task-row {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      margin-bottom: 6px;
    }
    .task-name {
      font-size: 16px;
      line-height: 22px;
      font-weight: 700;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .task-status,
    .task-sync-status {
      font-size: 13px;
      font-weight: 800;
      color: var(--secondary);
      white-space: nowrap;
    }
    .task-meta {
      display: flex;
      flex-wrap: wrap;
      gap: 14px;
      color: rgba(64, 72, 79, 0.78);
      font-size: 12px;
      font-weight: 700;
    }
    .task-meta span {
      display: inline-flex;
      align-items: center;
      gap: 5px;
    }
    .task-meta-status {
      position: relative;
      min-width: 0;
      max-width: min(420px, 100%);
      align-items: flex-start !important;
    }
    .task-meta-status-main {
      min-width: 0;
      display: inline-flex;
      align-items: flex-start;
      gap: 5px;
    }
    .task-meta-status-copy {
      min-width: 0;
      display: -webkit-box;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: normal;
      line-height: 17px;
      word-break: break-word;
    }
    .task-meta-status[data-tooltip]:hover::after {
      content: attr(data-tooltip);
      position: absolute;
      left: 0;
      bottom: calc(100% + 8px);
      z-index: 8;
      width: max-content;
      max-width: min(440px, 60vw);
      padding: 10px 12px;
      border-radius: 12px;
      background: rgba(15, 23, 42, 0.96);
      color: #ffffff;
      font-size: 12px;
      line-height: 18px;
      font-weight: 650;
      white-space: normal;
      box-shadow: 0 14px 30px rgba(15, 23, 42, 0.22);
      pointer-events: none;
    }
    .task-meta .material-symbols-outlined { font-size: 15px; }
    .progress {
      grid-column: 1 / -1;
      height: 6px;
      border-radius: 999px;
      background: var(--surface-variant);
      overflow: hidden;
    }
    .progress > div {
      height: 100%;
      border-radius: inherit;
      background: var(--primary);
    }
    .task-action {
      border: 1px solid rgba(191, 199, 208, 0.85);
      background: var(--surface-low);
      color: var(--on-surface);
      border-radius: 10px;
      padding: 8px 11px;
      font-size: 12px;
      font-weight: 800;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      cursor: pointer;
      white-space: nowrap;
    }
    .task-action:hover { background: var(--surface-variant); }
    .task-action:disabled,
    .record-button:disabled {
      cursor: not-allowed;
      opacity: 0.62;
    }
    .task-action.loading {
      background: rgba(211, 228, 254, 0.64);
      color: var(--primary);
    }
    .card-actions {
      position: relative;
      min-height: 64px;
      display: flex;
      align-items: center;
      justify-content: flex-end;
      align-self: stretch;
      padding-top: 22px;
    }
    .task-top-actions {
      position: absolute;
      top: 0;
      right: 0;
      display: inline-flex;
      align-items: center;
      gap: 6px;
    }
    .task-primary-actions {
      position: relative;
      display: inline-flex;
      align-items: center;
      justify-content: flex-end;
      gap: 8px;
    }
    .training-mode-menu {
      position: fixed;
      top: 0;
      left: 0;
      z-index: 19;
      min-width: 168px;
      padding: 6px;
      border: 1px solid rgba(191, 199, 208, 0.85);
      border-radius: 12px;
      background: #ffffff;
      box-shadow: 0 18px 36px rgba(15, 23, 42, 0.18);
    }
    .training-mode-menu[hidden] { display: none; }
    .training-mode-menu[data-ready="false"] { opacity: 0; pointer-events: none; }
    .training-mode-option {
      width: 100%;
      border: 0;
      background: transparent;
      color: var(--on-surface);
      border-radius: 9px;
      padding: 10px 11px;
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 12px;
      font-weight: 800;
      text-align: left;
      cursor: pointer;
      white-space: normal;
    }
    .training-mode-option:hover { background: var(--surface-variant); }
    .training-mode-option .material-symbols-outlined {
      font-size: 18px;
      color: var(--primary);
    }
    .training-mode-copy {
      display: flex;
      flex-direction: column;
      gap: 2px;
      min-width: 0;
    }
    .training-mode-label {
      font-size: 12px;
      font-weight: 800;
      color: var(--on-surface);
    }
    .training-mode-desc {
      font-size: 11px;
      color: var(--on-surface-variant);
      font-weight: 650;
      line-height: 16px;
    }
    .training-menu-empty {
      padding: 10px 11px;
      color: var(--on-surface-variant);
      font-size: 12px;
      line-height: 18px;
      font-weight: 700;
    }
    .icon-action {
      width: 40px;
      height: 40px;
      border: 1px solid rgba(191, 199, 208, 0.85);
      background: #ffffff;
      color: var(--secondary);
      border-radius: 12px;
      display: grid;
      place-items: center;
      cursor: pointer;
    }
    .icon-action:hover { background: rgba(134, 242, 228, 0.18); }
    .icon-action.subtle {
      width: 28px;
      height: 28px;
      border-radius: 999px;
      color: var(--on-surface-variant);
      background: rgba(255, 255, 255, 0.82);
    }
    .icon-action.danger {
      color: var(--error);
      border-color: rgba(186, 26, 26, 0.24);
    }
    .icon-action.subtle:hover { background: var(--surface-low); }
    .icon-action.danger:hover { background: var(--error-container); }
    .icon-action svg {
      width: 21px;
      height: 21px;
      display: block;
    }
    .icon-action.subtle svg {
      width: 16px;
      height: 16px;
    }
    .modal-backdrop {
      position: fixed;
      inset: 0;
      display: none;
      place-items: center;
      background: rgba(11, 28, 48, 0.38);
      z-index: 20;
      padding: 24px;
      overflow-y: auto;
    }
    .modal-backdrop.show { display: grid; }
    .modal-card {
      width: min(420px, 100%);
      border-radius: 22px;
      background: var(--surface-card);
      border: 1px solid rgba(191, 199, 208, 0.78);
      box-shadow: 0 24px 70px rgba(11, 28, 48, 0.18);
      padding: 22px;
    }
    .sfm-quality-card {
      width: min(74vw, 920px);
      max-width: calc(100vw - 48px);
      height: min(78vh, 680px);
      max-height: calc(100vh - 48px);
      display: flex;
      flex-direction: column;
    }
    .sfm-quality-modal-body {
      flex: 1;
      min-height: 0;
      overflow-y: auto;
      padding-right: 4px;
    }
    .modal-title {
      margin: 0 0 8px;
      font-size: 20px;
      line-height: 26px;
      font-weight: 850;
    }
    .modal-desc {
      margin: 0 0 18px;
      color: var(--on-surface-variant);
      font-size: 13px;
      line-height: 20px;
      font-weight: 650;
    }
    .upload-box {
      border: 1px dashed rgba(112, 120, 128, 0.52);
      border-radius: 16px;
      background: rgba(239, 244, 255, 0.6);
      padding: 22px;
      text-align: center;
    }
    .upload-box input { width: 100%; }
    .script-settings-card {
      width: min(860px, 100%);
      max-width: calc(100vw - 48px);
      max-height: calc(100vh - 48px);
      display: flex;
      flex-direction: column;
    }
    .script-settings-body {
      display: grid;
      grid-template-columns: minmax(0, 1.15fr) minmax(280px, 320px);
      gap: 18px;
      flex: 1;
      min-height: 0;
      overflow: hidden;
    }
    .script-settings-list {
      min-height: 0;
      max-height: min(56vh, 560px);
      overflow-y: auto;
      display: flex;
      flex-direction: column;
      gap: 12px;
      padding-right: 4px;
    }
    .script-item {
      position: relative;
      border: 1px solid rgba(191, 199, 208, 0.72);
      border-radius: 16px;
      background: rgba(239, 244, 255, 0.52);
      padding: 14px;
    }
    .script-item-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 8px;
    }
    .script-item-name {
      font-size: 14px;
      font-weight: 850;
      line-height: 20px;
    }
    .script-type-chip {
      border-radius: 999px;
      background: rgba(14, 118, 168, 0.12);
      color: var(--primary);
      font-size: 11px;
      font-weight: 800;
      padding: 4px 10px;
      white-space: nowrap;
    }
    .script-item-desc,
    .script-item-meta {
      font-size: 12px;
      line-height: 18px;
      color: var(--on-surface-variant);
    }
    .script-item-meta {
      margin-top: 8px;
      font-family: "Geist Mono", monospace;
      word-break: break-all;
    }
    .script-item-entry {
      font-family: "Geist Mono", monospace;
      word-break: break-all;
    }
    .script-item-custom-summary {
      margin-top: 8px;
    }
    .script-item-actions {
      margin-top: 12px;
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }
    .script-item-actions button,
    .script-form button,
    .script-form select,
    .script-form input,
    .script-form textarea {
      font: inherit;
    }
    .script-item-actions button {
      border-radius: 10px;
      border: 1px solid rgba(191, 199, 208, 0.85);
      background: #ffffff;
      color: var(--on-surface);
      padding: 7px 10px;
      font-size: 12px;
      font-weight: 800;
      cursor: pointer;
    }
    .script-item-actions button.danger {
      color: var(--error);
      border-color: rgba(186, 26, 26, 0.24);
    }
    .script-custom-popover {
      margin-top: 10px;
      border: 1px solid rgba(191, 199, 208, 0.85);
      border-radius: 14px;
      background: #ffffff;
      box-shadow: 0 12px 30px rgba(15, 23, 42, 0.08);
      padding: 12px;
      display: flex;
      flex-direction: column;
      gap: 10px;
    }
    .script-custom-popover-title {
      font-size: 12px;
      font-weight: 850;
      color: var(--on-surface);
    }
    .script-custom-popover-empty {
      font-size: 12px;
      line-height: 18px;
      color: var(--on-surface-variant);
    }
    .script-custom-popover-item {
      border-radius: 12px;
      background: rgba(239, 244, 255, 0.72);
      padding: 10px 12px;
    }
    .script-custom-popover-name {
      font-size: 12px;
      font-weight: 850;
      color: var(--on-surface);
      margin-bottom: 4px;
    }
    .script-custom-popover-command {
      font-family: "Geist Mono", monospace;
      font-size: 11px;
      line-height: 16px;
      color: var(--on-surface-variant);
      word-break: break-all;
    }
    .script-custom-popover-actions {
      margin-top: 10px;
      display: flex;
      justify-content: flex-end;
    }
    .script-custom-run-button {
      border: 0;
      border-radius: 999px;
      background: var(--primary);
      color: #ffffff;
      padding: 7px 12px;
      font-size: 12px;
      font-weight: 800;
      cursor: pointer;
    }
    .script-custom-run-button:disabled {
      cursor: not-allowed;
      opacity: 0.45;
    }
    .script-custom-popover-disabled {
      margin-top: 6px;
      font-size: 11px;
      line-height: 16px;
      color: var(--on-surface-variant);
      font-weight: 650;
    }
    .script-form {
      border: 1px solid rgba(191, 199, 208, 0.72);
      border-radius: 18px;
      background: rgba(255, 255, 255, 0.8);
      padding: 16px;
      display: flex;
      flex-direction: column;
      gap: 12px;
      align-self: start;
      min-height: 0;
      max-height: 100%;
      overflow-y: auto;
      padding-right: 12px;
    }
    .script-form-label {
      display: flex;
      flex-direction: column;
      gap: 6px;
      font-size: 12px;
      font-weight: 750;
      color: var(--on-surface-variant);
    }
    .script-form input,
    .script-form textarea,
    .script-form select {
      width: 100%;
      border: 1px solid rgba(191, 199, 208, 0.85);
      border-radius: 12px;
      background: #ffffff;
      padding: 10px 12px;
      color: var(--on-surface);
    }
    .script-form textarea {
      min-height: 96px;
      resize: vertical;
    }
    .script-form button {
      border-radius: 12px;
      border: 0;
      background: var(--secondary);
      color: #ffffff;
      padding: 10px 14px;
      font-weight: 850;
      cursor: pointer;
    }
    .script-form-hint {
      margin: -2px 0 0;
      color: var(--on-surface-variant);
      font-size: 12px;
      line-height: 18px;
    }
    .script-file-display {
      margin-top: -4px;
      border-radius: 12px;
      border: 1px dashed rgba(191, 199, 208, 0.85);
      background: rgba(239, 244, 255, 0.64);
      padding: 10px 12px;
      color: var(--on-surface);
      font-size: 12px;
      line-height: 18px;
    }
    .script-file-display strong {
      display: block;
      margin-bottom: 2px;
      font-weight: 850;
    }
    .sync-qr {
      width: 240px;
      height: 240px;
      margin: 0 auto 14px;
      border-radius: 16px;
      background: #ffffff;
      display: grid;
      place-items: center;
    }
    .sync-qr img {
      width: 100%;
      height: 100%;
      display: block;
    }
    .modal-actions {
      margin-top: 18px;
      display: flex;
      justify-content: flex-end;
      gap: 10px;
    }
    .modal-actions button {
      border-radius: 11px;
      padding: 9px 14px;
      font-weight: 800;
      cursor: pointer;
      border: 1px solid rgba(191, 199, 208, 0.85);
      background: var(--surface-low);
      color: var(--on-surface);
    }
    .modal-actions .primary {
      border-color: transparent;
      background: var(--secondary);
      color: #ffffff;
    }
    .quality-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
      margin: 14px 0;
    }
    .quality-metric {
      border-radius: 14px;
      border: 1px solid rgba(191, 199, 208, 0.72);
      background: rgba(239, 244, 255, 0.58);
      padding: 12px;
    }
    .quality-label {
      color: var(--on-surface-variant);
      font-size: 12px;
      font-weight: 750;
    }
    .quality-value {
      margin-top: 5px;
      color: var(--on-surface);
      font-size: 20px;
      font-weight: 900;
    }
    .quality-section-title {
      margin: 16px 0 8px;
      font-size: 13px;
      font-weight: 900;
      color: var(--secondary);
    }
    .quality-list {
      margin: 0;
      padding-left: 18px;
      color: var(--on-surface-variant);
      font-size: 13px;
      line-height: 20px;
      font-weight: 650;
    }
    .quality-image-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 12px;
      margin-top: 10px;
    }
    .quality-image-card {
      border-radius: 16px;
      border: 1px solid rgba(191, 199, 208, 0.72);
      background: rgba(15, 23, 42, 0.04);
      padding: 10px;
    }
    .quality-image-card img {
      display: block;
      width: 100%;
      max-height: 320px;
      object-fit: contain;
      border-radius: 12px;
      background: #0f172a;
    }
    .quality-image-caption {
      margin-top: 8px;
      color: var(--on-surface-variant);
      font-size: 12px;
      font-weight: 750;
    }
    .modal-actions .danger {
      border-color: rgba(186, 26, 26, 0.36);
      background: #ffffff;
      color: var(--error);
    }
    .empty {
      border: 1px dashed rgba(112, 120, 128, 0.45);
      border-radius: 18px;
      background: rgba(255, 255, 255, 0.54);
      padding: 34px 24px;
      text-align: center;
      color: var(--on-surface-variant);
      font-weight: 650;
    }
    .toast {
      position: fixed;
      right: 28px;
      bottom: 28px;
      background: #213145;
      color: #eaf1ff;
      border-radius: 14px;
      padding: 12px 16px;
      font-weight: 700;
      box-shadow: 0 12px 30px rgba(11, 28, 48, 0.16);
      opacity: 0;
      transform: translateY(10px);
      transition: opacity 180ms ease, transform 180ms ease;
      pointer-events: none;
    }
    .toast.show { opacity: 1; transform: translateY(0); }
    @media (max-width: 1100px), (max-height: 700px) {
      body { font-size: 14px; }
      .shell { grid-template-columns: minmax(320px, 0.9fr) minmax(360px, 1.1fr); }
      .brand { top: 20px; left: 24px; gap: 10px; }
      .brand-mark { width: 30px; height: 30px; }
      .brand-title { font-size: 18px; line-height: 22px; }
      .brand-subtitle { font-size: 11px; }
      .scan-card { width: min(300px, 82%); }
      h1 { font-size: 22px; line-height: 26px; }
      .lead { margin: 4px 0 20px; font-size: 13px; }
      .qr-wrap { width: min(34vh, 210px); height: min(34vh, 210px); border-radius: 14px; }
      .refresh-button { margin-top: 16px; padding: 6px 10px; }
      .token { margin-top: 8px; font-size: 11px; }
      .link-box { margin-top: 10px; padding: 8px 10px; }
      .tasks-pane { padding: 22px; }
      .tasks-header { max-width: 560px; margin-bottom: 18px; gap: 10px; }
      .tasks-title { font-size: 20px; line-height: 25px; }
      .header-actions { gap: 6px; flex-wrap: wrap; justify-content: flex-end; }
      .summary-pill { padding: 5px 10px; }
      .record-button { padding: 6px 10px; font-size: 11px; }
      .task-list { max-width: 560px; gap: 12px; }
      .automation-panel { max-width: 560px; margin-bottom: 12px; padding: 12px 14px; }
      .task-card { grid-template-columns: 42px 1fr minmax(150px, auto); gap: 12px; padding: 12px; }
      .task-card.uploading { grid-template-columns: 42px 1fr; }
      .task-icon { width: 40px; height: 40px; border-radius: 10px; }
      .task-name { font-size: 15px; line-height: 20px; }
      .task-status, .task-sync-status { font-size: 12px; }
      .task-meta { gap: 10px; font-size: 11px; }
      .card-actions { min-height: 54px; padding-top: 19px; }
      .task-primary-actions { gap: 6px; }
      .task-action { padding: 7px 9px; font-size: 11px; }
      .icon-action { width: 34px; height: 34px; border-radius: 10px; }
      .icon-action.subtle { width: 26px; height: 26px; }
      .empty { padding: 24px 18px; }
      .modal-backdrop { padding: 16px; }
      .modal-card { padding: 18px; }
      .sync-qr { width: min(42vh, 210px); height: min(42vh, 210px); }
    }
    @media (max-height: 620px) and (min-width: 921px) {
      .brand { top: 16px; }
      .scan-card { transform: translateY(10px); }
      .qr-wrap { width: min(32vh, 190px); height: min(32vh, 190px); }
      .tasks-pane { padding-top: 18px; padding-bottom: 18px; }
    }
    @media (max-width: 920px) {
      body { overflow: auto; }
      .shell { grid-template-columns: 1fr; height: auto; min-height: 100vh; }
      .scan-pane { min-height: min(52vh, 430px); border-right: 0; border-bottom: 1px solid rgba(191, 199, 208, 0.45); }
      .tasks-pane { height: auto; min-height: 0; }
      .script-settings-body { grid-template-columns: 1fr; }
      .script-settings-list { max-height: none; }
      .script-form { max-height: none; overflow: visible; padding-right: 16px; }
    }
  </style>
</head>
<body>
  <main class="shell">
    <section class="scan-pane">
      <div class="brand">
        <img class="brand-mark" src="/static/logo.png" alt="WorldGS Logo">
        <div>
          <div class="brand-title">WorldGS</div>
          <div class="brand-subtitle">我的3D场景</div>
        </div>
      </div>
      <div class="scan-card">
        <h1>扫码上传</h1>
        <p class="lead">同步移动端训练素材</p>
        <div class="qr-wrap">
          <img id="qrImage" alt="上传二维码">
        </div>
        <button class="refresh-button" id="refreshBtn" type="button">
          <span class="material-symbols-outlined">refresh</span>
          刷新
        </button>
        <div class="token">二维码配对有效期 <span id="countdown">--:--</span></div>
        <div class="link-box">
          <div class="scan-url" id="scanUrl">正在生成上传链接...</div>
          <button class="copy-button" id="copyBtn" type="button">复制</button>
        </div>
      </div>
    </section>
    <section class="tasks-pane">
      <div class="tasks-header">
        <div class="tasks-title">活动任务</div>
        <div class="header-actions">
          <button class="record-button" id="scriptSettingsButton" type="button">
            <span class="material-symbols-outlined">tune</span>
            脚本设置
          </button>
          <div class="summary-pill" id="summaryPill">等待上传</div>
        </div>
      </div>
      <div class="automation-panel" id="automationPanel">
        <div class="automation-title">
          <span class="material-symbols-outlined">terminal</span>
          脚本执行
        </div>
        <div class="automation-message" id="automationMessage">等待执行脚本。</div>
        <div class="automation-actions">
          <button id="continueButton" type="button" hidden>继续</button>
          <button id="cancelButton" type="button">取消</button>
        </div>
      </div>
      <div class="task-list" id="taskList"></div>
    </section>
  </main>
  <div class="toast" id="toast"></div>
  <div class="modal-backdrop" id="resultModal">
    <div class="modal-card">
      <h2 class="modal-title" id="resultModalTitle">上传高清模型</h2>
      <p class="modal-desc" id="resultModalDesc">支持上传 PLY 或 SOG 文件，保存后可扫码同步到手机。</p>
      <div id="resultModalBody"></div>
      <div class="modal-actions">
        <button class="danger" id="resultModalDelete" type="button">删除</button>
        <button id="resultModalCancel" type="button">取消</button>
        <button class="primary" id="resultModalConfirm" type="button">确定</button>
      </div>
    </div>
  </div>
  <div class="modal-backdrop" id="deleteTaskModal">
    <div class="modal-card">
      <h2 class="modal-title">确认删除这个任务吗？</h2>
      <p class="modal-desc">删除后会移除 Receiver 本地任务目录、素材和已上传的高清模型文件。这个操作不可恢复。</p>
      <div class="modal-actions">
        <button id="deleteTaskCancel" type="button">取消</button>
        <button class="danger" id="deleteTaskConfirm" type="button">确认删除</button>
      </div>
    </div>
  </div>
  <div class="modal-backdrop" id="sfmQualityModal">
    <div class="modal-card sfm-quality-card">
      <h2 class="modal-title">空三质量报告</h2>
      <p class="modal-desc">基于本地 COLMAP 产物生成的 P0 指标。严格重投影 RMS、残差图和覆盖热力图将在后续版本补齐。</p>
      <div class="sfm-quality-modal-body" id="sfmQualityModalBody"></div>
      <div class="modal-actions">
        <button id="sfmQualityModalClose" type="button">关闭</button>
      </div>
    </div>
  </div>
  <div class="modal-backdrop" id="scriptSettingsModal">
    <div class="modal-card script-settings-card">
      <h2 class="modal-title">脚本设置</h2>
      <p class="modal-desc">Receiver 只负责传参、执行和结果注册。登录态、Playwright、本地训练环境由你自己的脚本项目维护。</p>
      <div class="script-settings-body">
        <div class="script-settings-list" id="scriptList"></div>
        <form class="script-form" id="scriptForm">
          <input id="scriptEditingId" type="hidden" value="">
          <label class="script-form-label">
            脚本名称
            <input id="scriptNameInput" name="name" type="text" maxlength="80" placeholder="例如：知天下训练脚本" required>
          </label>
          <label class="script-form-label">
            脚本类型
            <select id="scriptTypeInput" name="scriptType">
              <option value="platform">平台脚本</option>
              <option value="local_training">本地训练脚本</option>
              <option value="generic">通用脚本</option>
            </select>
          </label>
          <label class="script-form-label">
            描述
            <textarea id="scriptDescriptionInput" name="description" maxlength="500" placeholder="说明这个脚本做什么、需要什么环境。"></textarea>
          </label>
          <label class="script-form-label">
            脚本入口文件
            <input id="scriptFileInput" name="file" type="file" accept=".sh,.bash,.py,.zip" required>
          </label>
          <div class="script-file-display" id="scriptFileDisplay"><strong>当前未选择文件</strong>新增脚本时请上传 `.sh/.bash/.py/.zip`。</div>
          <div class="script-form-hint" id="scriptFileHint">新增脚本时必传；编辑时如果不想替换脚本文件，可以留空。</div>
          <label class="script-form-label">
            入口相对路径
            <input id="scriptEntryFileInput" name="entryFile" type="text" maxlength="200" placeholder="zip 包必填，例如：run_explorerglobal.sh 或 scripts/run.sh">
          </label>
          <div class="script-form-label">
            <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;">
              <span>自定义脚本</span>
              <button id="addCustomActionButton" type="button">+</button>
            </div>
            <div class="script-form-hint">命令相对脚本目录，例如：`run_explorerglobal.sh --login`、`run_explorerglobal.sh --check`。</div>
            <div id="customActionList"></div>
          </div>
          <div class="script-form-hint">单文件脚本支持 `.sh/.bash/.py`。多文件脚本项目请上传 `.zip`，并填写 zip 包内的入口相对路径。脚本会收到 `WORLDGS_*` 公共变量。</div>
          <div style="display:flex;gap:12px;align-items:center;">
            <button id="scriptSubmitButton" type="submit">添加脚本</button>
            <button id="scriptResetButton" type="button">取消编辑</button>
          </div>
        </form>
      </div>
      <div class="modal-actions">
        <button id="scriptSettingsClose" type="button">关闭</button>
      </div>
    </div>
  </div>
  <div class="training-mode-menu" id="globalTrainingMenu" hidden data-ready="false" data-upload-id="" role="menu" aria-hidden="true">
  </div>
  <script>
    const state = {
      expiresAt: 0,
      scanUrl: "",
      managementNonce: "",
      scripts: [],
      scriptCustomPanelScriptId: "",
      activeRunId: null,
      activeRunKind: "",
      activeRunUploadId: null,
      runPollTimer: null,
      trainingBusyUploadIds: new Set(),
      resultModalUploadId: "",
      resultModalMode: "upload",
      resultModalFile: null,
      resultModalDeletePending: false,
      deleteTaskUploadId: "",
      trainingMenuUploadId: ""
    };

    function formatTime(seconds) {
      const safe = Math.max(0, Math.floor(seconds));
      const mins = String(Math.floor(safe / 60)).padStart(2, "0");
      const secs = String(safe % 60).padStart(2, "0");
      return `${mins}:${secs}`;
    }

    function formatBytes(value) {
      if (!value) return "0 B";
      const units = ["B", "KB", "MB", "GB"];
      let size = Number(value);
      let index = 0;
      while (size >= 1024 && index < units.length - 1) {
        size /= 1024;
        index += 1;
      }
      return `${size >= 10 || index === 0 ? size.toFixed(0) : size.toFixed(1)} ${units[index]}`;
    }

    function showToast(message) {
      const toast = document.getElementById("toast");
      toast.textContent = message;
      toast.classList.add("show");
      window.clearTimeout(showToast.timer);
      showToast.timer = window.setTimeout(() => toast.classList.remove("show"), 1800);
    }

    async function refreshDashboard() {
      const response = await fetch("/api/dashboard", { cache: "no-store" });
      if (!response.ok) throw new Error("dashboard fetch failed");
      const payload = await response.json();
      state.managementNonce = payload.managementNonce || "";
      state.scripts = Array.isArray(payload.scripts) ? payload.scripts : [];
      state.expiresAt = Date.now() + payload.expiresInSeconds * 1000;
      if (payload.scanUrl !== state.scanUrl) {
        state.scanUrl = payload.scanUrl;
        document.getElementById("scanUrl").textContent = payload.scanUrl;
        document.getElementById("qrImage").src = `${payload.qrUrl}&_=${Date.now()}`;
      }
      renderScriptList();
      renderTasks(payload.uploads || []);
    }

    function enabledScripts() {
      return state.scripts.filter((script) => script.enabled !== false);
    }

    function scriptTypeLabel(scriptType) {
      if (scriptType === "platform") return "平台脚本";
      if (scriptType === "local_training") return "本地训练脚本";
      return "通用脚本";
    }

    function scriptTypeIcon(scriptType) {
      if (scriptType === "platform") return "cloud_upload";
      if (scriptType === "local_training") return "developer_board";
      return "terminal";
    }

    function customActionsOf(script) {
      return Array.isArray(script && script.customActions) ? script.customActions : [];
    }

    function customActionRowsHtml(actions) {
      if (!actions.length) {
        return `<div class="script-form-hint" data-empty-custom-actions="true">还没有自定义动作。点击右上角 + 添加，例如“登录”或“检查登录”。</div>`;
      }
      return actions.map((action) => `
        <div class="custom-action-row" data-action-id="${escapeHtml(action.actionId || "")}" style="display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.4fr) auto;gap:8px;margin-top:8px;">
          <input data-custom-action-field="name" type="text" maxlength="80" placeholder="按钮名字，例如：登录" value="${escapeHtml(action.name || "")}">
          <input data-custom-action-field="command" type="text" maxlength="240" placeholder="例如：run_explorerglobal.sh --login" value="${escapeHtml(action.command || "")}">
          <button type="button" class="danger" data-custom-action-delete="true">删除</button>
        </div>
      `).join("");
    }

    function renderCustomActionList(actions) {
      const container = document.getElementById("customActionList");
      if (!container) return;
      container.innerHTML = customActionRowsHtml(actions);
    }

    function setScriptFileDisplay(contentHtml) {
      const node = document.getElementById("scriptFileDisplay");
      if (!node) return;
      node.innerHTML = contentHtml;
    }

    function scriptCurrentFileHtml(script) {
      const scriptName = escapeHtml(script && script.name || "未命名脚本");
      const entryFile = escapeHtml(script && (script.entryFileRelative || shortPath(script.entryFile || "")) || "未配置入口");
      return `<strong>当前已保存脚本：${scriptName}</strong>入口文件：${entryFile}。如不替换脚本包，可直接留空。`;
    }

    function scriptCustomActionsPopoverHtml(script) {
      const actions = customActionsOf(script);
      if (!actions.length) {
        return `<div class="script-custom-popover"><div class="script-custom-popover-title">自定义脚本</div><div class="script-custom-popover-empty">当前还没有配置自定义脚本。</div></div>`;
      }
      return `
        <div class="script-custom-popover">
          <div class="script-custom-popover-title">自定义脚本</div>
          ${actions.map((action) => `
            <div class="script-custom-popover-item">
              <div class="script-custom-popover-name">${escapeHtml(action.name || "未命名动作")}</div>
              <div class="script-custom-popover-command">${escapeHtml(action.command || "")}</div>
              <div class="script-custom-popover-actions">
                <button
                  type="button"
                  class="script-custom-run-button"
                  data-script-custom-run="true"
                  data-action-id="${escapeHtml(action.actionId || "")}"
                  ${script.enabled === false ? "disabled" : ""}
                >执行</button>
              </div>
            </div>
          `).join("")}
          ${script.enabled === false ? '<div class="script-custom-popover-disabled">脚本已禁用。要执行这些全局动作，请先启用脚本。</div>' : ""}
        </div>
      `;
    }

    function taskTrainingStatusHtml(trainingStatus) {
      if (!trainingStatus) return "";
      const message = trainingStatus.scriptName
        ? `${trainingStatus.scriptName} · ${trainingStatus.message || trainingStatus.status || "运行中"}`
        : (trainingStatus.message || trainingStatus.status || "训练中");
      return `
        <span class="task-meta-status" data-tooltip="${escapeHtml(message)}" title="${escapeHtml(message)}">
          <span class="task-meta-status-main">
            <span class="material-symbols-outlined">model_training</span>
            <span class="task-meta-status-copy">${escapeHtml(message)}</span>
          </span>
        </span>
      `;
    }

    function resetScriptForm() {
      document.getElementById("scriptForm").reset();
      document.getElementById("scriptEditingId").value = "";
      document.getElementById("scriptFileInput").required = true;
      document.getElementById("scriptSubmitButton").textContent = "添加脚本";
      setScriptFileDisplay("<strong>当前未选择文件</strong>新增脚本时请上传 `.sh/.bash/.py/.zip`。");
      renderCustomActionList([]);
    }

    function addCustomActionRow(action = {}) {
      const currentActions = readCustomActionsFromForm();
      currentActions.push({
        actionId: action.actionId || "",
        name: action.name || "",
        command: action.command || ""
      });
      renderCustomActionList(currentActions);
    }

    function readCustomActionsFromForm() {
      return Array.from(document.querySelectorAll("#customActionList .custom-action-row")).map((row) => {
        const nameInput = row.querySelector('[data-custom-action-field="name"]');
        const commandInput = row.querySelector('[data-custom-action-field="command"]');
        return {
          actionId: row.dataset.actionId || "",
          name: (nameInput && nameInput.value || "").trim(),
          command: (commandInput && commandInput.value || "").trim()
        };
      }).filter((action) => action.name || action.command);
    }

    function populateScriptForm(script) {
      document.getElementById("scriptEditingId").value = script.scriptId || "";
      document.getElementById("scriptNameInput").value = script.name || "";
      document.getElementById("scriptTypeInput").value = script.scriptType || "generic";
      document.getElementById("scriptDescriptionInput").value = script.description || "";
      document.getElementById("scriptEntryFileInput").value = script.entryFileRelative || "";
      document.getElementById("scriptFileInput").value = "";
      document.getElementById("scriptFileInput").required = false;
      document.getElementById("scriptSubmitButton").textContent = "保存修改";
      setScriptFileDisplay(scriptCurrentFileHtml(script));
      renderCustomActionList(customActionsOf(script));
    }

    function renderTrainingMenuOptions() {
      const menu = getGlobalTrainingMenu();
      if (!menu) return;
      const scripts = enabledScripts();
      if (!scripts.length) {
        menu.innerHTML = `<div class="training-menu-empty">暂无可用脚本，请先到“脚本设置”里添加脚本。</div>`;
        return;
      }
      menu.innerHTML = scripts.map((script) => `
        <button class="training-mode-option" type="button" role="menuitem" data-script-id="${escapeHtml(script.scriptId || "")}">
          <span class="material-symbols-outlined">${escapeHtml(scriptTypeIcon(script.scriptType || ""))}</span>
          <span class="training-mode-copy">
            <span class="training-mode-label">${escapeHtml(script.name || "未命名脚本")}</span>
            <span class="training-mode-desc">${escapeHtml(script.description || scriptTypeLabel(script.scriptType || ""))}</span>
          </span>
        </button>
      `).join("");
    }

    function renderScriptList() {
      const container = document.getElementById("scriptList");
      if (!container) return;
      if (!state.scripts.length) {
        container.innerHTML = `<div class="empty">还没有脚本。先在右侧表单上传一个入口脚本。</div>`;
        return;
      }
      container.innerHTML = state.scripts.map((script) => `
        <div class="script-item" data-script-id="${escapeHtml(script.scriptId || "")}">
          <div class="script-item-header">
            <div class="script-item-name">${escapeHtml(script.name || "未命名脚本")}</div>
            <div class="script-type-chip">${escapeHtml(scriptTypeLabel(script.scriptType || ""))}</div>
          </div>
          <div class="script-item-desc">${escapeHtml(script.description || "暂无描述")}</div>
          <div class="script-item-meta script-item-entry">${escapeHtml(script.entryFile || "")}</div>
          <div class="script-item-meta script-item-custom-summary">${customActionsOf(script).length ? `自定义动作：${escapeHtml(customActionsOf(script).map((action) => action.name || "未命名动作").join(" / "))}` : "自定义动作：暂无"}</div>
          <div class="script-item-actions">
            <button type="button" data-script-action="edit">编辑</button>
            <button type="button" data-script-action="custom-actions">自定义脚本</button>
            <button type="button" data-script-action="toggle">${script.enabled === false ? "启用" : "禁用"}</button>
            <button type="button" class="danger" data-script-action="delete">删除</button>
          </div>
          ${state.scriptCustomPanelScriptId === script.scriptId ? scriptCustomActionsPopoverHtml(script) : ""}
        </div>
      `).join("");
    }

    function openScriptSettingsModal() {
      state.scriptCustomPanelScriptId = "";
      renderScriptList();
      resetScriptForm();
      document.getElementById("scriptSettingsModal").classList.add("show");
    }

    function closeScriptSettingsModal() {
      document.getElementById("scriptSettingsModal").classList.remove("show");
      state.scriptCustomPanelScriptId = "";
      resetScriptForm();
    }

    async function submitScriptForm(event) {
      event.preventDefault();
      const editingId = document.getElementById("scriptEditingId").value.trim();
      const fileInput = document.getElementById("scriptFileInput");
      if (!editingId && (!fileInput.files || !fileInput.files[0])) {
        showToast("请先选择脚本文件");
        return;
      }
      const form = new FormData();
      form.append("name", document.getElementById("scriptNameInput").value);
      form.append("description", document.getElementById("scriptDescriptionInput").value);
      form.append("scriptType", document.getElementById("scriptTypeInput").value);
      if (!editingId) {
        form.append("enabled", "true");
      }
      form.append("entryFile", document.getElementById("scriptEntryFileInput").value.trim());
      form.append("customActionsJson", JSON.stringify(readCustomActionsFromForm()));
      if (fileInput.files && fileInput.files[0]) {
        form.append("file", fileInput.files[0]);
      }
      const response = await fetch(editingId ? `/api/scripts/${encodeURIComponent(editingId)}` : "/api/scripts", {
        method: editingId ? "PUT" : "POST",
        headers: managementFormHeaders(),
        body: form
      });
      const payload = await parseJsonResponse(response);
      if (!response.ok) {
        showToast(payload.detail || (editingId ? "脚本更新失败" : "脚本添加失败"));
        return;
      }
      state.scriptCustomPanelScriptId = "";
      resetScriptForm();
      showToast(editingId ? "脚本已更新" : "脚本已添加");
      await refreshDashboard();
    }

    async function toggleScriptEnabled(scriptId, enabled) {
      const form = new FormData();
      form.append("enabled", enabled ? "true" : "false");
      const response = await fetch(`/api/scripts/${encodeURIComponent(scriptId)}`, {
        method: "PUT",
        headers: managementFormHeaders(),
        body: form
      });
      const payload = await parseJsonResponse(response);
      if (!response.ok) {
        showToast(payload.detail || "脚本更新失败");
        return;
      }
      showToast(enabled ? "脚本已启用" : "脚本已禁用");
      await refreshDashboard();
    }

    async function deleteScript(scriptId) {
      const response = await fetch(`/api/scripts/${encodeURIComponent(scriptId)}`, {
        method: "DELETE",
        headers: managementHeaders()
      });
      const payload = await parseJsonResponse(response);
      if (!response.ok) {
        showToast(payload.detail || "脚本删除失败");
        return;
      }
      if (state.scriptCustomPanelScriptId === scriptId) {
        state.scriptCustomPanelScriptId = "";
      }
      showToast("脚本已删除");
      await refreshDashboard();
    }

    function renderTasks(tasks) {
      const list = document.getElementById("taskList");
      const successCount = tasks.filter((task) => task.ok).length;
      const receivingCount = tasks.filter((task) => task.status === "receiving").length;
      document.getElementById("summaryPill").textContent = tasks.length
        ? receivingCount
          ? `${receivingCount}个同步中`
          : `${successCount}个已接收`
        : "等待上传";

      if (!tasks.length) {
        list.innerHTML = `<div class="empty">暂无本地任务。请用手机扫描左侧二维码上传素材。</div>`;
        closeTrainingMenus();
        return;
      }

      list.innerHTML = tasks.map((task) => {
        if (task.status === "receiving") {
          const progress = task.syncProgress || {};
          const completedFiles = Number(progress.completedFiles || 0);
          const totalFiles = Number(progress.totalFiles || 0);
          const percent = totalFiles > 0 ? Math.max(0, Math.min(100, Math.round(completedFiles * 100 / totalFiles))) : 0;
          return `
          <article class="task-card uploading" data-upload-id="${escapeHtml(task.uploadId || "")}">
            <div class="task-icon"><span class="material-symbols-outlined">sync</span></div>
            <div class="task-main">
              <div class="task-row">
                <div class="task-name">${escapeHtml(task.taskName || "WorldGS 数据集")}</div>
                <div class="task-sync-status">同步中 ${completedFiles}/${totalFiles}</div>
              </div>
              <div class="task-meta">
                <span><span class="material-symbols-outlined">upload_file</span> 文件 ${completedFiles}/${totalFiles}</span>
                <span><span class="material-symbols-outlined">storage</span> ${formatBytes(progress.completedBytes || 0)} / ${formatBytes(progress.totalBytes || 0)}</span>
                <span class="folder-meta" data-open-path="${escapeHtml(task.openPath || "")}"><span class="material-symbols-outlined">folder_open</span> ${escapeHtml(shortPath(task.openPath || task.datasetPath || ""))} · 点击打开</span>
              </div>
            </div>
            <div class="progress" aria-label="同步进度"><div style="width:${percent}%"></div></div>
          </article>
        `;
        }
        const hasModelResult = Boolean(task.modelResult);
        const hasSfmQualityReport = Boolean(task.sfmQualityReport);
        const hasScriptRun = Boolean(task.scriptRun);
        const hasAutomationTraining = Boolean(task.automationTraining);
        const hasLocalTraining = Boolean(task.localTraining);
        const trainingStatus = task.scriptRun || task.localTraining || task.automationTraining || null;
        const isScriptBusy = hasScriptRun && ["queued", "running"].includes(task.scriptRun.status || "");
        const isTrainingBusy = state.trainingBusyUploadIds.has(task.uploadId || "") || isScriptBusy || hasAutomationTraining || hasLocalTraining;
        return `
        <article class="task-card" data-upload-id="${escapeHtml(task.uploadId || "")}">
          <div class="task-icon success"><span class="material-symbols-outlined" style="font-variation-settings:'FILL' 1;">check_circle</span></div>
          <div class="task-main">
            <div class="task-row">
              <div class="task-name">${escapeHtml(task.taskName || "WorldGS 任务包")}</div>
            </div>
            <div class="task-meta">
              <span><span class="material-symbols-outlined">image</span> 图片 ${formatCount(task.imageCount)} 张</span>
              <span><span class="material-symbols-outlined">storage</span> ${formatBytes(task.sizeBytes)}</span>
              ${taskTrainingStatusHtml(trainingStatus)}
              <span class="folder-meta" data-open-path="${escapeHtml(task.openPath || task.extractedPath || "")}"><span class="material-symbols-outlined">folder_open</span> ${escapeHtml(shortPath(task.openPath || task.extractedPath || ""))} · 点击打开</span>
            </div>
          </div>
          <div class="card-actions">
            ${hasSfmQualityReport ? `
            <div class="task-top-actions">
              <button class="icon-action sfm-quality-button subtle" type="button" title="空三质量" aria-label="空三质量" data-report='${escapeHtml(JSON.stringify(task.sfmQualityReport))}'>
                ${sfmQualityIcon()}
              </button>
              <button class="icon-action subtle danger task-delete-button" type="button" title="删除任务" aria-label="删除任务">
                ${deleteTaskIcon()}
              </button>
            </div>
            ` : `
            <div class="task-top-actions">
              <button class="icon-action subtle danger task-delete-button" type="button" title="删除任务" aria-label="删除任务">
                ${deleteTaskIcon()}
              </button>
            </div>
            `}
            <div class="task-primary-actions">
              <button class="icon-action model-result-button" type="button" title="${hasModelResult ? "同步到手机" : "上传高清模型"}" data-model-result='${escapeHtml(JSON.stringify(task.modelResult || null))}'>
                ${hasModelResult ? phoneSyncIcon() : uploadIcon()}
              </button>
              <button class="task-action train-button${!hasModelResult && isTrainingBusy ? " loading" : ""}" type="button" data-has-model-result="${hasModelResult ? "true" : "false"}" data-result-open-path="${escapeHtml((task.modelResult && task.modelResult.path) || task.openPath || "")}">
                <span class="material-symbols-outlined">${hasModelResult ? "folder_open" : (!hasModelResult && isTrainingBusy ? "progress_activity" : "play_arrow")}</span>
                ${hasModelResult ? "打开结果" : (!hasModelResult && isTrainingBusy ? "运行中" : "开始训练")}
              </button>
            </div>
          </div>
        </article>
      `;
      }).join("");
      if (state.trainingMenuUploadId) {
        window.requestAnimationFrame(() => {
          positionTrainingMenu(state.trainingMenuUploadId);
        });
      }
    }

    function uploadIcon() {
      return `<svg viewBox="0 0 1024 1024" aria-hidden="true"><path d="M150.592 655.232c12.48 0 22.592 10.112 22.592 22.613333v116.138667a67.754667 67.754667 0 0 0 67.754667 67.776h542.122666a67.754667 67.754667 0 0 0 67.754667-67.776v-116.16a22.592 22.592 0 1 1 45.184 0v116.16a112.938667 112.938667 0 0 1-112.938667 112.96H240.938667A112.938667 112.938667 0 0 1 128 794.005333v-116.181333c0-12.48 10.112-22.592 22.592-22.592z" fill="currentColor"></path><path d="M517.76 204.074667c11.84 0 21.461333 6.741333 21.461333 15.04v512c0 8.32-9.6 15.061333-21.482666 15.061333-11.861333 0-21.482667-6.741333-21.482667-15.04v-512c0-8.32 9.6-15.061333 21.482667-15.061333z" fill="currentColor"></path><path d="M503.36 188.842667a21.354667 21.354667 0 0 1 30.293333 0l234.24 235.413333a21.610667 21.610667 0 0 1 0 30.464 21.354667 21.354667 0 0 1-30.293333 0l-234.24-235.434667a21.610667 21.610667 0 0 1 0-30.442666z" fill="currentColor"></path><path d="M531.264 188.416c-8.362667-8.426667-21.397333-8.96-29.077333-1.237333L264.618667 425.941333c-7.701333 7.722667-7.168 20.821333 1.194666 29.226667 8.384 8.426667 21.397333 8.96 29.098667 1.237333L532.48 217.642667c7.701333-7.744 7.146667-20.821333-1.216-29.226667z" fill="currentColor"></path></svg>`;
    }

    function phoneSyncIcon() {
      return `<svg viewBox="0 0 1024 1024" aria-hidden="true"><path d="M675.730015 65.279772 350.594937 65.279772c-44.89449 0-81.286328 36.389792-81.286328 81.282235l0 731.557508c0 44.89449 36.391838 81.284281 81.286328 81.284281l325.134055 0c44.89449 0 81.286328-36.389792 81.286328-81.284281L757.01532 146.562007C757.016343 101.669564 720.624505 65.279772 675.730015 65.279772zM716.374714 878.119515c0 22.447245-18.197454 40.642652-40.644699 40.642652L350.594937 918.762167c-22.447245 0-40.642652-18.195408-40.642652-40.642652l0-81.284281 406.42243 0L716.374714 878.119515zM716.374714 756.194628 309.952284 756.194628 309.952284 248.168126l406.42243 0L716.374714 756.194628zM716.374714 207.52445 309.952284 207.52445l0-60.962443c0-22.445198 18.195408-40.640606 40.642652-40.640606l325.134055 0c22.447245 0 40.644699 18.195408 40.644699 40.640606L716.373691 207.52445zM513.165546 878.117468c11.222599 0 20.319791-9.099239 20.319791-20.319791 0-11.222599-9.097192-20.321838-20.319791-20.321838-11.224646 0-20.321838 9.099239-20.321838 20.321838C492.843708 869.019253 501.9409 878.117468 513.165546 878.117468z" fill="currentColor"></path></svg>`;
    }

    function sfmQualityIcon() {
      return `<svg viewBox="0 0 1024 1024" aria-hidden="true"><path d="M298.6 221.8c-29.4 0-53.3 23.9-53.3 53.3v524.7c0 29.4 23.9 53.3 53.3 53.3h426.7c29.4 0 53.3-23.9 53.3-53.3V275.1c0-29.4-23.9-53.3-53.3-53.3h-64v-64h64c64.8 0 117.3 52.5 117.3 117.3v524.7c0 64.8-52.5 117.3-117.3 117.3H298.6c-64.8 0-117.3-52.5-117.3-117.3V275.1c0-64.8 52.5-117.3 117.3-117.3h64v64h-64z" fill="currentColor"></path><path d="M640 106.9H384c-23.6 0-42.7 19.1-42.7 42.7v85.3c0 23.6 19.1 42.7 42.7 42.7h256c23.6 0 42.7-19.1 42.7-42.7v-85.3c0-23.6-19.1-42.7-42.7-42.7z m-21.3 106.6H405.3v-42.7h213.3v42.7h0.1z" fill="currentColor"></path><path d="M492.09 671.84c-12.5 12.5-32.76 12.5-45.25 0l-96.17-96.17c-12.5-12.5-12.5-32.76 0-45.25 12.5-12.5 32.76-12.5 45.25 0l96.17 96.17c12.5 12.49 12.5 32.75 0 45.25z" fill="currentColor"></path><path d="M673.33 445.81c12.5 12.5 12.5 32.76 0 45.25L492.31 672.09c-12.5 12.5-32.76 12.5-45.25 0-12.5-12.5-12.5-32.76 0-45.25l181.02-181.02c12.49-12.51 32.75-12.51 45.25-0.01z" fill="currentColor"></path></svg>`;
    }

    function deleteTaskIcon() {
      return `<svg viewBox="0 0 1024 1024" aria-hidden="true"><path d="M614.190939 259.036661c-22.116717 0-40.047088 17.910928-40.047088 40.047088l0.37146 502.160911c0 22.097274 17.930371 40.048111 40.047088 40.048111s40.048111-17.950837 40.048111-40.048111l-0.350994-502.160911C654.259516 276.948613 636.328122 259.036661 614.190939 259.036661zM893.234259 140.105968l-318.891887 0.148379-0.178055-41.407062c0-22.13616-17.933441-40.048111-40.067554-40.048111-7.294127 0-14.126742 1.958608-20.017916 5.364171-5.894244-3.405563-12.729929-5.364171-20.031219-5.364171-22.115694 0-40.047088 17.911952-40.047088 40.048111l0.188288 41.463344-230.115981 0.106424c-3.228531-0.839111-6.613628-1.287319-10.104125-1.287319-3.502777 0-6.89913 0.452301-10.136871 1.296529l-73.067132 0.033769c-22.115694 0-40.048111 17.950837-40.048111 40.047088 0 22.13616 17.931395 40.048111 40.048111 40.048111l43.176358-0.020466 0.292666 617.902982 0.059352 0 0 42.551118c0 44.233434 35.862789 80.095199 80.095199 80.095199l40.048111 0 0 0.302899 440.523085-0.25685 0-0.046049 40.048111 0c43.663452 0 79.146595-34.95 80.054267-78.395488l-0.329505-583.369468c0-22.135136-17.930371-40.047088-40.048111-40.047088-22.115694 0-40.047088 17.911952-40.047088 40.047088l0.287549 509.324054c-1.407046 60.314691-18.594497 71.367421-79.993892 71.367421l41.575908 1.022283-454.442096 0.26606 52.398394-1.288343c-62.715367 0-79.305207-11.522428-80.0645-75.308173l0.493234 76.611865-0.543376 0-0.313132-660.818397 236.82273-0.109494c1.173732 0.103354 2.360767 0.166799 3.561106 0.166799 1.215688 0 2.416026-0.063445 3.604084-0.169869l32.639375-0.01535c1.25355 0.118704 2.521426 0.185218 3.805676 0.185218 1.299599 0 2.582825-0.067538 3.851725-0.188288l354.913289-0.163729c22.115694 0 40.050158-17.911952 40.050158-40.047088C933.283394 158.01792 915.349953 140.105968 893.234259 140.105968zM413.953452 259.036661c-22.116717 0-40.048111 17.910928-40.048111 40.047088l0.37146 502.160911c0 22.097274 17.931395 40.048111 40.049135 40.048111 22.115694 0 40.047088-17.950837 40.047088-40.048111l-0.37146-502.160911C454.00054 276.948613 436.069145 259.036661 413.953452 259.036661z" fill="currentColor"></path></svg>`;
    }

    function openSfmQualityModal(report) {
      document.getElementById("sfmQualityModalBody").innerHTML = renderSfmQualityReport(report || {});
      document.getElementById("sfmQualityModal").classList.add("show");
    }

    function closeSfmQualityModal() {
      document.getElementById("sfmQualityModal").classList.remove("show");
    }

    function renderSfmQualityReport(report) {
      const track = report.trackLengthStats || {};
      const pointError = report.pointErrorStats || {};
      const camera = report.cameraSummary || {};
      const strategy = report.strategySummary || {};
      const visualizations = report.visualizations || {};
      const recommendations = Array.isArray(report.recommendations) ? report.recommendations : [];
      return `
        <div class="quality-grid">
          ${qualityMetric("注册率", formatRatio(report.registeredRatio))}
          ${qualityMetric("注册图片", `${formatCount(report.registeredImageCount)} / ${formatCount(report.inputImageCount)}`)}
          ${qualityMetric("连接点数", formatCount(report.sparsePointCount))}
          ${qualityMetric("质量等级", qualityLevelText(report.qualityLevel))}
          ${qualityMetric("重投影 RMS", formatPixels(report.reprojectionRmsPx))}
          ${qualityMetric("残差观测", formatCount(report.reprojectionObservationCount))}
        </div>
        <div class="quality-section-title">稀疏几何</div>
        <div class="quality-grid">
          ${qualityMetric("Track 平均", formatNumber(track.mean))}
          ${qualityMetric("Track P90", formatNumber(track.p90))}
          ${qualityMetric("点误差均值", formatNumber(pointError.mean))}
          ${qualityMetric("点误差 P90", formatNumber(pointError.p90))}
        </div>
        <div class="quality-section-title">相机与策略</div>
        <div class="quality-grid">
          ${qualityMetric("相机数量", formatCount(camera.count))}
          ${qualityMetric("策略", strategy.strategy || "暂无")}
          ${qualityMetric("匹配对", formatCount(strategy.pairCount))}
          ${qualityMetric("回环对", formatCount(strategy.loopPairCount))}
        </div>
        <div class="quality-section-title">建议</div>
        ${recommendations.length ? `<ul class="quality-list">${recommendations.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>` : `<p class="modal-desc">暂无建议。</p>`}
        <div class="quality-section-title">可视化</div>
        ${renderSfmQualityVisualizations(visualizations)}
      `;
    }

    function renderSfmQualityVisualizations(visualizations) {
      const cards = [];
      const coverage = visualizations.coverageHeatmap || null;
      if (coverage && coverage.url) {
        cards.push(qualityImageCard(coverage.url, "覆盖热力图 · 稀疏点 XY / track density"));
      }
      const residualPlots = Array.isArray(visualizations.residualPlots) ? visualizations.residualPlots.slice(0, 3) : [];
      residualPlots.forEach((plot) => {
        if (plot && plot.url) {
          cards.push(qualityImageCard(plot.url, `相机 ${plot.cameraId || "未知"} 残差图 · ${formatCount(plot.observationCount)} 个观测`));
        }
      });
      if (!cards.length) return `<p class="modal-desc">当前任务暂无可视化图。</p>`;
      return `<div class="quality-image-grid">${cards.join("")}</div>`;
    }

    function qualityImageCard(url, caption) {
      return `
        <div class="quality-image-card">
          <img src="${escapeHtml(url)}" alt="${escapeHtml(caption)}" loading="lazy" />
          <div class="quality-image-caption">${escapeHtml(caption)}</div>
        </div>
      `;
    }

    function qualityMetric(label, value) {
      return `<div class="quality-metric"><div class="quality-label">${escapeHtml(label)}</div><div class="quality-value">${escapeHtml(value)}</div></div>`;
    }

    function formatRatio(value) {
      const numeric = Number(value);
      if (!Number.isFinite(numeric)) return "暂无";
      return `${Math.round(numeric * 100)}%`;
    }

    function formatNumber(value) {
      const numeric = Number(value);
      if (!Number.isFinite(numeric)) return "暂无";
      return numeric >= 10 ? numeric.toFixed(0) : numeric.toFixed(2);
    }

    function formatPixels(value) {
      const numeric = Number(value);
      if (!Number.isFinite(numeric)) return "暂无";
      return `${formatNumber(numeric)} px`;
    }

    function qualityLevelText(level) {
      if (level === "good") return "良好";
      if (level === "warning") return "需关注";
      if (level === "poor") return "较差";
      return "暂无";
    }

    function formatCount(value) {
      const count = Number(value || 0);
      return Number.isFinite(count) ? String(count) : "0";
    }

    function shortPath(value) {
      if (!value) return "本地目录";
      const parts = value.split(/[\\\\/]/).filter(Boolean);
      return parts.slice(-2).join("/");
    }

    function escapeHtml(value) {
      return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
    }

    async function parseJsonResponse(response) {
      const text = await response.text();
      if (!text) return {};
      try {
        return JSON.parse(text);
      } catch (_error) {
        return { detail: text || `HTTP ${response.status}` };
      }
    }

    async function openPath(path) {
      if (!path) return;
      const response = await fetch("/api/open-path", {
        method: "POST",
        headers: managementHeaders(),
        body: JSON.stringify({ path }),
      });
      showToast(response.ok ? "已打开本地文件夹" : "打开目录失败");
    }

    function setAutomationMessage(message) {
      const panel = document.getElementById("automationPanel");
      panel.classList.add("show");
      document.getElementById("automationMessage").textContent = message;
    }

    function setTrainingBusy(uploadId, busy) {
      if (!uploadId) return;
      if (busy) {
        state.trainingBusyUploadIds.add(uploadId);
        if (state.trainingMenuUploadId === uploadId) closeTrainingMenus();
      } else {
        state.trainingBusyUploadIds.delete(uploadId);
      }
      document.querySelectorAll(`.task-card[data-upload-id="${CSS.escape(uploadId)}"] .train-button`).forEach((button) => {
        button.classList.toggle("loading", busy);
        button.innerHTML = busy
          ? `<span class="material-symbols-outlined">progress_activity</span>训练中`
          : `<span class="material-symbols-outlined">play_arrow</span>开始训练`;
      });
    }

    function getGlobalTrainingMenu() {
      return document.getElementById("globalTrainingMenu");
    }

    function getTrainingMenuAnchor(uploadId) {
      if (!uploadId) return null;
      return document.querySelector(`.task-card[data-upload-id="${CSS.escape(uploadId)}"] .train-button`);
    }

    function closeTrainingMenus() {
      const menu = getGlobalTrainingMenu();
      if (!menu) return;
      menu.hidden = true;
      menu.dataset.ready = "false";
      menu.dataset.uploadId = "";
      menu.setAttribute("aria-hidden", "true");
      state.trainingMenuUploadId = "";
    }

    function positionTrainingMenu(uploadId) {
      const menu = getGlobalTrainingMenu();
      const anchor = getTrainingMenuAnchor(uploadId);
      if (!menu || !anchor) {
        closeTrainingMenus();
        return false;
      }
      menu.hidden = false;
      menu.dataset.ready = "false";
      const anchorRect = anchor.getBoundingClientRect();
      const menuRect = menu.getBoundingClientRect();
      const viewportWidth = document.documentElement.clientWidth;
      const viewportHeight = window.innerHeight;
      const gap = 8;
      const maxLeft = Math.max(gap, viewportWidth - menuRect.width - gap);
      const left = Math.min(Math.max(gap, anchorRect.right - menuRect.width), maxLeft);
      let top = anchorRect.bottom + gap;
      if (top + menuRect.height > viewportHeight - gap) {
        top = Math.max(gap, anchorRect.top - menuRect.height - gap);
      }
      menu.style.left = `${Math.round(left)}px`;
      menu.style.top = `${Math.round(top)}px`;
      menu.dataset.ready = "true";
      return true;
    }

    function openTrainingMenu(uploadId) {
      const menu = getGlobalTrainingMenu();
      if (!menu || !uploadId) return;
      renderTrainingMenuOptions();
      state.trainingMenuUploadId = uploadId;
      menu.dataset.uploadId = uploadId;
      menu.setAttribute("aria-hidden", "false");
      positionTrainingMenu(uploadId);
    }

    function toggleTrainingMenu(uploadId, event) {
      event.stopPropagation();
      if (!uploadId) return;
      const menu = getGlobalTrainingMenu();
      const isSameMenuOpen = menu && !menu.hidden && state.trainingMenuUploadId === uploadId;
      if (isSameMenuOpen) {
        closeTrainingMenus();
        return;
      }
      openTrainingMenu(uploadId);
    }

    function handleTrainingMenuViewportChange() {
      if (!state.trainingMenuUploadId) return;
      positionTrainingMenu(state.trainingMenuUploadId);
    }

    function clearActiveRun() {
      state.activeRunId = null;
      state.activeRunKind = "";
      state.activeRunUploadId = null;
      updateRunActionButtons();
    }

    function updateRunActionButtons() {
      const continueButton = document.getElementById("continueButton");
      const cancelButton = document.getElementById("cancelButton");
      if (!continueButton || !cancelButton) return;
      continueButton.hidden = state.activeRunKind !== "automation";
      cancelButton.hidden = !state.activeRunId;
    }

    async function startScriptRun(uploadId, scriptId, event, actionId = "") {
      if (event) event.stopPropagation();
      const script = state.scripts.find((item) => item.scriptId === scriptId);
      if (!script) {
        showToast("脚本不存在或已被删除");
        return;
      }
      const selectedAction = actionId
        ? customActionsOf(script).find((item) => item.actionId === actionId)
        : null;
      const actionLabel = selectedAction ? `${script.name} · ${selectedAction.name || "未命名动作"}` : script.name;
      if (!uploadId && !actionId) {
        showToast("默认训练必须绑定具体任务");
        return;
      }
      if (uploadId) {
        setTrainingBusy(uploadId, true);
      }
      setAutomationMessage(`正在启动脚本：${actionLabel}`);
      try {
        const response = await fetch("/api/script-runs", {
          method: "POST",
          headers: managementHeaders(),
          body: JSON.stringify({ uploadId, scriptId, actionId })
        });
        const payload = await parseJsonResponse(response);
        if (!response.ok) {
          if (uploadId) {
            setTrainingBusy(uploadId, false);
          }
          setAutomationMessage(payload.detail || "脚本启动失败");
          showToast("脚本启动失败");
          return;
        }
        state.activeRunId = payload.scriptRunId;
        state.activeRunKind = "script";
        state.activeRunUploadId = uploadId;
        updateRunActionButtons();
        setAutomationMessage(`脚本 ${actionLabel} 已启动：${payload.status}`);
        showToast("脚本已启动");
        pollScriptRunStatus(state.activeRunId);
      } catch (error) {
        if (uploadId) {
          setTrainingBusy(uploadId, false);
        }
        setAutomationMessage(error.message || "脚本启动失败");
        showToast("脚本启动失败");
      }
    }

    async function startGlobalScriptAction(scriptId, actionId, event) {
      return startScriptRun("", scriptId, event, actionId);
    }

    async function startLocalTraining(uploadId, event) {
      if (event) event.stopPropagation();
      if (!uploadId) return;
      setTrainingBusy(uploadId, true);
      setAutomationMessage("正在启动本地高斯训练...");
      try {
        const response = await fetch("/api/local-training/runs", {
          method: "POST",
          headers: managementHeaders(),
          body: JSON.stringify({ uploadId, preset: "fast" })
        });
        const payload = await parseJsonResponse(response);
        if (!response.ok) {
          throw new Error(payload.detail || "本地高斯训练启动失败");
        }
        state.activeRunId = payload.trainingRunId;
        state.activeRunKind = "local";
        state.activeRunUploadId = uploadId;
        updateRunActionButtons();
        setAutomationMessage(`已启动本地高斯训练：${payload.status}`);
        showToast("本地训练已启动");
        pollLocalTrainingStatus(payload.trainingRunId);
      } catch (error) {
        setTrainingBusy(uploadId, false);
        setAutomationMessage(error.message || "本地高斯训练启动失败");
        showToast("本地训练启动失败");
      }
    }

    async function pollScriptRunStatus(runId) {
      window.clearTimeout(state.runPollTimer);
      const response = await fetch(`/api/script-runs/${runId}`, { cache: "no-store" });
      const payload = await parseJsonResponse(response);
      if (!response.ok) {
        setAutomationMessage(payload.detail || "脚本状态获取失败");
        return;
      }
      setAutomationMessage(`脚本 ${payload.scriptName || ""}：${payload.status} ${payload.message || ""}`.trim());
      if (["queued", "running"].includes(payload.status)) {
        state.runPollTimer = window.setTimeout(() => pollScriptRunStatus(runId), 1800);
      } else if (state.activeRunId === runId) {
        setTrainingBusy(state.activeRunUploadId, false);
        clearActiveRun();
        refreshDashboard().catch(() => {});
      }
    }

    async function pollRunStatus(runId) {
      window.clearTimeout(state.runPollTimer);
      const response = await fetch(`/api/automation/runs/${runId}`, { cache: "no-store" });
      const payload = await parseJsonResponse(response);
      if (!response.ok) {
        setAutomationMessage(payload.detail || "运行状态查询失败");
        return;
      }
      const step = payload.currentStepId ? `，当前步骤：${payload.currentStepId}` : "";
      const message = payload.message ? `，${payload.message}` : "";
      setAutomationMessage(`状态：${payload.status}${step}${message}`);
      if (["queued", "running", "paused"].includes(payload.status)) {
        state.runPollTimer = window.setTimeout(() => pollRunStatus(runId), 1800);
      } else if (state.activeRunId === runId) {
        setTrainingBusy(state.activeRunUploadId, false);
        clearActiveRun();
      }
    }

    async function pollLocalTrainingStatus(runId) {
      window.clearTimeout(state.runPollTimer);
      const response = await fetch(`/api/local-training/runs/${runId}`, { cache: "no-store" });
      const payload = await parseJsonResponse(response);
      if (!response.ok) {
        setAutomationMessage(payload.detail || "本地高斯训练状态获取失败");
        return;
      }
      setAutomationMessage(`本地训练：${payload.status} ${payload.progressPercent || 0}% ${payload.message || ""}`);
      if (["queued", "running"].includes(payload.status)) {
        state.runPollTimer = window.setTimeout(() => pollLocalTrainingStatus(runId), 2000);
      } else if (state.activeRunId === runId) {
        setTrainingBusy(state.activeRunUploadId, false);
        clearActiveRun();
        refreshDashboard().catch(() => {});
      }
    }

    async function continueRun() {
      if (!state.activeRunId || state.activeRunKind !== "automation") return;
      const response = await fetch(`/api/automation/runs/${state.activeRunId}/continue`, {
        method: "POST",
        headers: managementHeaders()
      });
      showToast(response.ok ? "已继续观察" : "继续失败");
      if (response.ok) pollRunStatus(state.activeRunId);
    }

    async function cancelRun() {
      if (!state.activeRunId) return;
      const endpoint = state.activeRunKind === "script"
        ? `/api/script-runs/${state.activeRunId}/cancel`
        : state.activeRunKind === "local"
          ? `/api/local-training/runs/${state.activeRunId}/cancel`
          : `/api/automation/runs/${state.activeRunId}/cancel`;
      const response = await fetch(endpoint, {
        method: "POST",
        headers: managementHeaders()
      });
      showToast(response.ok ? "已取消运行" : "取消失败");
      if (!response.ok) return;
      if (state.activeRunKind === "script") {
        pollScriptRunStatus(state.activeRunId);
      } else if (state.activeRunKind === "local") {
        pollLocalTrainingStatus(state.activeRunId);
      } else {
        pollRunStatus(state.activeRunId);
      }
    }

    function openUploadResultModal(uploadId) {
      state.resultModalUploadId = uploadId;
      state.resultModalMode = "upload";
      state.resultModalFile = null;
      state.resultModalDeletePending = false;
      document.body.classList.remove("result-sync-modal-open");
      document.getElementById("resultModalTitle").textContent = "上传高清模型";
      document.getElementById("resultModalDesc").textContent = "选择电脑离线训练完成的 PLY 或 SOG 文件，保存后即可同步到手机。";
      document.getElementById("resultModalBody").innerHTML = `
        <div class="upload-box">
          <input id="modelResultInput" type="file" accept=".ply,.sog">
        </div>
      `;
      document.getElementById("resultModalConfirm").style.display = "";
      document.getElementById("resultModalConfirm").textContent = "确定";
      document.getElementById("resultModalDelete").style.display = "none";
      document.getElementById("resultModalDelete").textContent = "删除";
      document.getElementById("resultModal").classList.add("show");
      document.getElementById("modelResultInput").addEventListener("change", (event) => {
        state.resultModalFile = event.target.files && event.target.files[0] ? event.target.files[0] : null;
      });
    }

    function openSyncResultModal(uploadId, modelResult) {
      state.resultModalUploadId = uploadId;
      state.resultModalMode = "sync";
      state.resultModalDeletePending = false;
      const scanUrl = modelResult.scanUrl || "";
      document.getElementById("resultModalTitle").textContent = "同步到手机";
      document.getElementById("resultModalDesc").textContent = "在手机结果页点击扫一扫，扫描下方二维码下载高清模型。";
      document.getElementById("resultModalBody").innerHTML = `
        <div class="sync-qr"><img alt="高清模型同步二维码" src="/qr.svg?data=${encodeURIComponent(scanUrl)}"></div>
        <div class="scan-url">${escapeHtml(scanUrl)}</div>
      `;
      document.getElementById("resultModalConfirm").style.display = "none";
      document.getElementById("resultModalDelete").style.display = "";
      document.getElementById("resultModalDelete").textContent = "删除";
      document.getElementById("resultModal").classList.add("show");
      document.body.classList.add("result-sync-modal-open");
    }

    function closeResultModal() {
      document.getElementById("resultModal").classList.remove("show");
      document.body.classList.remove("result-sync-modal-open");
      state.resultModalUploadId = "";
      state.resultModalMode = "upload";
      state.resultModalFile = null;
      state.resultModalDeletePending = false;
    }

    async function confirmResultModal() {
      if (state.resultModalMode !== "upload") return;
      if (!state.resultModalFile) {
        showToast("请选择 PLY 或 SOG 文件");
        return;
      }
      const form = new FormData();
      form.append("file", state.resultModalFile);
      const response = await fetch(`/api/uploads/${state.resultModalUploadId}/result`, {
        method: "POST",
        headers: managementFormHeaders(),
        body: form
      });
      const payload = await parseJsonResponse(response);
      if (!response.ok) {
        showToast(payload.detail || "高清模型上传失败");
        return;
      }
      closeResultModal();
      showToast("高清模型已保存");
      await refreshDashboard();
    }

    async function deleteResultModal() {
      if (!state.resultModalUploadId) return;
      if (!state.resultModalDeletePending) {
        state.resultModalDeletePending = true;
        document.getElementById("resultModalDesc").textContent = "再次点击确认删除会移除当前上传的高清模型。删除后需要重新上传才能同步到手机。";
        document.getElementById("resultModalDelete").textContent = "确认删除";
        return;
      }
      const response = await fetch(`/api/uploads/${state.resultModalUploadId}/result`, {
        method: "DELETE",
        headers: managementHeaders()
      });
      const payload = await parseJsonResponse(response);
      if (!response.ok) {
        showToast(payload.detail || "高清模型删除失败");
        return;
      }
      closeResultModal();
      showToast("高清模型已删除");
      await refreshDashboard();
    }

    function openDeleteTaskModal(uploadId) {
      state.deleteTaskUploadId = uploadId || "";
      if (!state.deleteTaskUploadId) return;
      document.getElementById("deleteTaskModal").classList.add("show");
    }

    function closeDeleteTaskModal() {
      document.getElementById("deleteTaskModal").classList.remove("show");
      state.deleteTaskUploadId = "";
    }

    async function confirmDeleteTask() {
      if (!state.deleteTaskUploadId) return;
      const response = await fetch(`/api/uploads/${state.deleteTaskUploadId}`, {
        method: "DELETE",
        headers: managementHeaders()
      });
      const payload = await parseJsonResponse(response);
      if (!response.ok) {
        showToast(payload.detail || "任务删除失败");
        return;
      }
      closeDeleteTaskModal();
      showToast("任务已删除");
      await refreshDashboard();
    }

    function managementHeaders() {
      return {
        "Content-Type": "application/json",
        "X-WorldGS-Management-Nonce": state.managementNonce
      };
    }

    function managementFormHeaders() {
      return {
        "X-WorldGS-Management-Nonce": state.managementNonce
      };
    }

    document.getElementById("refreshBtn").addEventListener("click", async () => {
      await refreshDashboard();
      showToast("二维码已刷新");
    });

    document.getElementById("copyBtn").addEventListener("click", async () => {
      await navigator.clipboard.writeText(state.scanUrl);
      showToast("上传链接已复制");
    });

    document.getElementById("scriptSettingsButton").addEventListener("click", openScriptSettingsModal);
    document.getElementById("scriptSettingsClose").addEventListener("click", closeScriptSettingsModal);
    document.getElementById("scriptForm").addEventListener("submit", submitScriptForm);
    document.getElementById("scriptResetButton").addEventListener("click", () => {
      resetScriptForm();
      state.scriptCustomPanelScriptId = "";
      renderScriptList();
    });
    document.getElementById("addCustomActionButton").addEventListener("click", () => addCustomActionRow());
    document.getElementById("scriptFileInput").addEventListener("change", (event) => {
      const file = event.target.files && event.target.files[0] ? event.target.files[0] : null;
      if (!file) {
        const editingId = document.getElementById("scriptEditingId").value.trim();
        const editingScript = editingId ? state.scripts.find((script) => script.scriptId === editingId) : null;
        setScriptFileDisplay(editingScript ? scriptCurrentFileHtml(editingScript) : "<strong>当前未选择文件</strong>新增脚本时请上传 `.sh/.bash/.py/.zip`。");
        return;
      }
      setScriptFileDisplay(`<strong>已选择新文件：${escapeHtml(file.name)}</strong>保存后会替换当前脚本文件。`);
    });
    document.getElementById("scriptSettingsModal").addEventListener("click", (event) => {
      if (event.target.id === "scriptSettingsModal") closeScriptSettingsModal();
    });
    document.getElementById("customActionList").addEventListener("click", (event) => {
      const deleteButton = event.target.closest("[data-custom-action-delete]");
      if (!deleteButton) return;
      const row = deleteButton.closest(".custom-action-row");
      if (!row) return;
      row.remove();
      if (!document.querySelector("#customActionList .custom-action-row")) {
        renderCustomActionList([]);
      }
    });
    document.getElementById("scriptList").addEventListener("click", (event) => {
      const item = event.target.closest(".script-item");
      if (!item) return;
      const customRunButton = event.target.closest("[data-script-custom-run]");
      if (customRunButton) {
        const scriptId = item.dataset.scriptId || "";
        const actionId = customRunButton.dataset.actionId || "";
        startGlobalScriptAction(scriptId, actionId, event);
        return;
      }
      const actionButton = event.target.closest("[data-script-action]");
      if (!actionButton) return;
      const scriptId = item.dataset.scriptId || "";
      const action = actionButton.dataset.scriptAction || "";
      const script = state.scripts.find((candidate) => candidate.scriptId === scriptId);
      if (!script) return;
      if (action === "edit") {
        populateScriptForm(script);
      } else if (action === "custom-actions") {
        state.scriptCustomPanelScriptId = state.scriptCustomPanelScriptId === scriptId ? "" : scriptId;
        renderScriptList();
      } else if (action === "toggle") {
        toggleScriptEnabled(scriptId, script.enabled === false);
      } else if (action === "delete") {
        deleteScript(scriptId);
      }
    });
    document.getElementById("continueButton").addEventListener("click", continueRun);
    document.getElementById("cancelButton").addEventListener("click", cancelRun);
    document.getElementById("resultModalCancel").addEventListener("click", closeResultModal);
    document.getElementById("resultModalConfirm").addEventListener("click", confirmResultModal);
    document.getElementById("resultModalDelete").addEventListener("click", deleteResultModal);
    document.getElementById("resultModal").addEventListener("click", (event) => {
      if (event.target.id === "resultModal") closeResultModal();
    });
    document.getElementById("deleteTaskCancel").addEventListener("click", closeDeleteTaskModal);
    document.getElementById("deleteTaskConfirm").addEventListener("click", confirmDeleteTask);
    document.getElementById("deleteTaskModal").addEventListener("click", (event) => {
      if (event.target.id === "deleteTaskModal") closeDeleteTaskModal();
    });
    document.getElementById("sfmQualityModalClose").addEventListener("click", closeSfmQualityModal);
    document.getElementById("sfmQualityModal").addEventListener("click", (event) => {
      if (event.target.id === "sfmQualityModal") closeSfmQualityModal();
    });
    document.addEventListener("click", (event) => {
      if (event.target.closest("#globalTrainingMenu")) return;
      if (event.target.closest(".train-button")) return;
      closeTrainingMenus();
    });
    document.addEventListener("scroll", handleTrainingMenuViewportChange, true);
    window.addEventListener("resize", handleTrainingMenuViewportChange);
    document.getElementById("globalTrainingMenu").addEventListener("click", (event) => {
      const trainingModeOption = event.target.closest(".training-mode-option");
      if (!trainingModeOption) return;
      event.stopPropagation();
      const menu = event.currentTarget;
      const uploadId = menu.dataset.uploadId || "";
      const scriptId = trainingModeOption.dataset.scriptId || "";
      closeTrainingMenus();
      startScriptRun(uploadId, scriptId, event);
    });

    document.getElementById("taskList").addEventListener("click", (event) => {
      const sfmQualityButton = event.target.closest(".sfm-quality-button");
      if (sfmQualityButton) {
        event.stopPropagation();
        openSfmQualityModal(JSON.parse(sfmQualityButton.dataset.report || "{}"));
        return;
      }
      const deleteTaskButton = event.target.closest(".task-delete-button");
      if (deleteTaskButton) {
        event.stopPropagation();
        const card = event.target.closest(".task-card");
        openDeleteTaskModal(card ? card.dataset.uploadId : "");
        return;
      }
      const modelButton = event.target.closest(".model-result-button");
      if (modelButton) {
        event.stopPropagation();
        const card = event.target.closest(".task-card");
        const uploadId = card ? card.dataset.uploadId : "";
        const rawModelResult = modelButton.dataset.modelResult || "null";
        const modelResult = JSON.parse(rawModelResult);
        if (modelResult) {
          openSyncResultModal(uploadId, modelResult);
        } else {
          openUploadResultModal(uploadId);
        }
        return;
      }
      const trainButton = event.target.closest(".train-button");
      if (trainButton) {
        const card = event.target.closest(".task-card");
        if (trainButton.dataset.hasModelResult === "true") {
          openPath(trainButton.dataset.resultOpenPath || (card ? card.dataset.openPath : ""));
        } else {
          toggleTrainingMenu(card ? card.dataset.uploadId : "", event);
        }
        return;
      }
      closeTrainingMenus();
      const folderMeta = event.target.closest(".folder-meta");
      if (folderMeta) openPath(folderMeta.dataset.openPath);
    });

    window.setInterval(() => {
      document.getElementById("countdown").textContent = formatTime((state.expiresAt - Date.now()) / 1000);
      if (state.expiresAt && state.expiresAt - Date.now() < 1000) refreshDashboard().catch(() => {});
    }, 1000);

    window.setInterval(() => refreshDashboard().catch(() => {}), 30000);
    updateRunActionButtons();
    refreshDashboard().catch(() => showToast("页面状态加载失败"));
  </script>
</body>
</html>
"""


def _scan_url(request: Request, config: ReceiverConfig, token: str) -> str:
    request_host = request.url.hostname or ""
    if request_host in {"localhost", "127.0.0.1", "::1"}:
        addresses = local_lan_addresses()
        if addresses:
            host = addresses[0]
            return f"http://{host}:{config.port}/upload?token={token}"
    return str(request.url_for("upload")) + f"?token={token}"


def _recent_uploads(
    output_dir: Path,
    uploads: dict[str, dict[str, object]],
    request: Optional[Request] = None,
    config: Optional[ReceiverConfig] = None,
    automation_training_by_upload: Optional[dict[str, dict[str, object]]] = None,
    local_training_by_upload: Optional[dict[str, dict[str, object]]] = None,
    script_runs_by_upload: Optional[dict[str, dict[str, object]]] = None,
) -> list[dict[str, object]]:
    reports: list[dict[str, object]] = []
    seen = set()
    automation_training_by_upload = automation_training_by_upload or {}
    local_training_by_upload = local_training_by_upload or {}
    script_runs_by_upload = script_runs_by_upload or {}
    for item in uploads.values():
        upload_id = str(item.get("uploadId", ""))
        if upload_id:
            seen.add(upload_id)
        reports.append(_with_training_statuses(
            _with_current_dataset_counts(output_dir, dict(item), request=request, config=config),
            automation_training_by_upload,
            local_training_by_upload,
            script_runs_by_upload,
        ))

    for report_path in output_dir.glob("*/*/upload_report.json"):
        try:
            payload = json.loads(report_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        upload_id = str(payload.get("uploadId", ""))
        if upload_id in seen:
            continue
        package_path = Path(str(payload.get("packagePath", "")))
        task_dir = report_path.parent
        reports.append(_with_training_statuses(
            _with_current_dataset_counts(
                output_dir,
                {
                    "uploadId": upload_id,
                    "taskName": _task_name_from_path(task_dir),
                    "createdAt": report_path.stat().st_mtime,
                    "sha256": payload.get("sha256", ""),
                    "sizeBytes": payload.get("sizeBytes", 0),
                    "fileCount": payload.get("fileCount", 0),
                    "imageCount": payload.get("imageCount", 0),
                    "savePath": str(package_path),
                    "extractedPath": str(payload.get("extractedPath", task_dir / "extracted")),
                    "datasetPath": str(payload.get("datasetPath", "")),
                    "reportPath": str(report_path),
                    "openPath": str(task_dir),
                    "ok": bool(payload.get("ok", True)),
                },
                request=request,
                config=config,
            ),
            automation_training_by_upload,
            local_training_by_upload,
            script_runs_by_upload,
        ))

    for manifest_path in output_dir.glob("*/*/sync_manifest.json"):
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        upload_id = str(payload.get("sessionId", ""))
        if not upload_id or upload_id in seen:
            continue
        status = str(payload.get("status") or "receiving")
        if status == "completed" and (manifest_path.parent / "upload_report.json").exists():
            continue
        seen.add(upload_id)
        reports.append(_sync_manifest_status(manifest_path, payload))

    reports.sort(key=lambda item: float(item.get("createdAt", 0)), reverse=True)
    return reports[:8]


def _automation_training_by_upload(output_dir: Path) -> dict[str, dict[str, object]]:
    active_statuses = {"queued", "running", "paused"}
    runs_root = output_dir / "automations" / "pointcosm" / "runs"
    if not runs_root.is_dir():
        return {}
    latest_by_upload: dict[str, dict[str, object]] = {}
    for summary_file in runs_root.glob("*/summary.json"):
        try:
            summary = json.loads(summary_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        upload_id = str(summary.get("uploadId") or "")
        status = str(summary.get("status") or "")
        if not upload_id or status not in active_statuses:
            continue
        current = latest_by_upload.get(upload_id)
        if current and str(current.get("startedAt") or "") >= str(summary.get("startedAt") or ""):
            continue
        latest_by_upload[upload_id] = {
            "automationRunId": summary.get("automationRunId"),
            "status": status,
            "platformId": summary.get("platformId"),
            "platformName": summary.get("platformName"),
            "message": summary.get("message"),
            "startedAt": summary.get("startedAt"),
        }
    return {
        upload_id: {
            key: value
            for key, value in training.items()
            if key != "startedAt" and value is not None
        }
        for upload_id, training in latest_by_upload.items()
    }


def _with_automation_training(
    item: dict[str, object],
    automation_training_by_upload: dict[str, dict[str, object]],
) -> dict[str, object]:
    training = automation_training_by_upload.get(str(item.get("uploadId") or ""))
    if training:
        item["automationTraining"] = training
    return item


def _script_runs_by_upload(output_dir: Path) -> dict[str, dict[str, object]]:
    latest_by_upload: dict[str, dict[str, object]] = {}
    for summary_file in output_dir.glob("*/*/script_runs/*/summary.json"):
        try:
            summary = json.loads(summary_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        upload_id = str(summary.get("uploadId") or "")
        if not upload_id:
            continue
        current = latest_by_upload.get(upload_id)
        if current and str(current.get("startedAt") or "") >= str(summary.get("startedAt") or ""):
            continue
        latest_by_upload[upload_id] = {
            "scriptRunId": summary.get("scriptRunId"),
            "scriptId": summary.get("scriptId"),
            "scriptName": summary.get("scriptName"),
            "scriptType": summary.get("scriptType"),
            "status": summary.get("status"),
            "message": summary.get("message"),
            "previewUrl": summary.get("previewUrl"),
            "startedAt": summary.get("startedAt"),
        }
    return {
        upload_id: {
            key: value
            for key, value in script_run.items()
            if key != "startedAt" and value is not None
        }
        for upload_id, script_run in latest_by_upload.items()
    }


def _with_script_run(
    item: dict[str, object],
    script_runs_by_upload: dict[str, dict[str, object]],
) -> dict[str, object]:
    script_run = script_runs_by_upload.get(str(item.get("uploadId") or ""))
    if script_run:
        item["scriptRun"] = script_run
    return item


def _local_training_by_upload(output_dir: Path) -> dict[str, dict[str, object]]:
    latest_by_upload: dict[str, dict[str, object]] = {}
    for summary_file in output_dir.glob("*/*/local_training/*/summary.json"):
        try:
            summary = json.loads(summary_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        upload_id = str(summary.get("uploadId") or "")
        if not upload_id:
            continue
        current = latest_by_upload.get(upload_id)
        if current and str(current.get("startedAt") or "") >= str(summary.get("startedAt") or ""):
            continue
        latest_by_upload[upload_id] = {
            "trainingRunId": summary.get("trainingRunId"),
            "status": summary.get("status"),
            "progressPercent": summary.get("progressPercent"),
            "currentStep": summary.get("currentStep"),
            "message": summary.get("message"),
            "startedAt": summary.get("startedAt"),
        }
    return {
        upload_id: {
            key: value
            for key, value in training.items()
            if key != "startedAt" and value is not None
        }
        for upload_id, training in latest_by_upload.items()
    }


def _with_local_training(
    item: dict[str, object],
    local_training_by_upload: dict[str, dict[str, object]],
) -> dict[str, object]:
    training = local_training_by_upload.get(str(item.get("uploadId") or ""))
    if training:
        item["localTraining"] = training
    return item


def _with_training_statuses(
    item: dict[str, object],
    automation_training_by_upload: dict[str, dict[str, object]],
    local_training_by_upload: dict[str, dict[str, object]],
    script_runs_by_upload: dict[str, dict[str, object]],
) -> dict[str, object]:
    item = _with_script_run(item, script_runs_by_upload)
    item = _with_local_training(item, local_training_by_upload)
    return _with_automation_training(item, automation_training_by_upload)


def _parse_form_bool(value: str) -> bool:
    normalized = (value or "").strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError("invalid boolean form value")


def _parse_custom_actions_form(value: str) -> list[dict[str, object]]:
    raw_value = (value or "").strip()
    if not raw_value:
        return []
    try:
        payload = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise ValueError("custom actions json is invalid") from exc
    if not isinstance(payload, list):
        raise ValueError("custom actions json must be a list")
    return payload


def _sync_manifest_status(manifest_path: Path, payload: dict[str, object]) -> dict[str, object]:
    task_dir = manifest_path.parent
    files = dict(payload.get("files") or {})
    total_files = len(files)
    completed_files = 0
    total_bytes = 0
    completed_bytes = 0
    for item in files.values():
        if not isinstance(item, dict):
            continue
        size_bytes = int(item.get("sizeBytes") or 0)
        total_bytes += size_bytes
        if item.get("status") == "completed":
            completed_files += 1
            completed_bytes += size_bytes
    dataset_path = str(payload.get("datasetPath") or "")
    return {
        "uploadId": str(payload.get("sessionId") or ""),
        "status": str(payload.get("status") or "receiving"),
        "taskName": str(payload.get("taskName") or task_dir.name),
        "createdAt": manifest_path.stat().st_mtime,
        "sha256": "",
        "sizeBytes": completed_bytes,
        "fileCount": completed_files,
        "imageCount": 0,
        "savePath": dataset_path,
        "extractedPath": dataset_path,
        "datasetPath": dataset_path,
        "reportPath": "",
        "openPath": str(task_dir),
        "ok": False,
        "syncProgress": {
            "completedFiles": completed_files,
            "totalFiles": total_files,
            "completedBytes": completed_bytes,
            "totalBytes": total_bytes,
        },
    }


def _with_current_dataset_counts(
    output_dir: Path,
    item: dict[str, object],
    request: Optional[Request] = None,
    config: Optional[ReceiverConfig] = None,
) -> dict[str, object]:
    dataset_dir = _dataset_dir_for_upload(output_dir, item)
    if dataset_dir is not None:
        item["imageCount"] = _count_dataset_images(dataset_dir)
    sfm_quality_report = _read_sfm_quality_report(output_dir, item)
    if sfm_quality_report is not None:
        item["sfmQualityReport"] = _sfm_quality_report_payload(str(item.get("uploadId", "")), sfm_quality_report)
    model_result = _read_model_result(output_dir, item, missing_ok=True)
    if model_result is not None:
        item["modelResult"] = _model_result_payload(request, config, str(item.get("uploadId", "")), model_result)
    return item


def _read_sfm_quality_report(output_dir: Path, item: dict[str, object]) -> Optional[dict[str, object]]:
    candidates: list[Path] = []
    for key in ("datasetPath", "extractedPath", "savePath"):
        value = item.get(key)
        if not value:
            continue
        base = Path(str(value))
        candidates.append(base / "reports" / "sfm_quality_report.json")
        candidates.append(base / "worldgs_task" / "reports" / "sfm_quality_report.json")
    open_path = item.get("openPath")
    if open_path:
        task_dir = Path(str(open_path))
        candidates.append(task_dir / "dataset" / "reports" / "sfm_quality_report.json")
        candidates.append(task_dir / "extracted" / "worldgs_task" / "reports" / "sfm_quality_report.json")

    output_root = output_dir.resolve()
    for candidate in candidates:
        if not candidate.is_absolute():
            continue
        try:
            resolved = candidate.resolve()
            resolved.relative_to(output_root)
        except (OSError, ValueError):
            continue
        if not resolved.is_file() or resolved.stat().st_size > 256 * 1024:
            continue
        try:
            payload = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _sfm_quality_report_payload(upload_id: str, report: dict[str, object]) -> dict[str, object]:
    payload = dict(report)
    visualizations = payload.get("visualizations")
    if isinstance(visualizations, dict):
        payload["visualizations"] = _sfm_quality_visualizations_payload(upload_id, visualizations)
    return payload


def _sfm_quality_visualizations_payload(upload_id: str, visualizations: dict[str, object]) -> dict[str, object]:
    result = dict(visualizations)
    coverage = result.get("coverageHeatmap")
    if isinstance(coverage, dict):
        result["coverageHeatmap"] = _sfm_quality_image_payload(upload_id, coverage)
    residuals = result.get("residualPlots")
    if isinstance(residuals, list):
        result["residualPlots"] = [
            _sfm_quality_image_payload(upload_id, item)
            for item in residuals
            if isinstance(item, dict)
        ]
    return result


def _sfm_quality_image_payload(upload_id: str, item: dict[str, object]) -> dict[str, object]:
    payload = dict(item)
    path = str(payload.get("path") or "")
    filename = Path(path).name
    if filename and path.startswith("quality/"):
        payload["url"] = f"/api/uploads/{quote(upload_id, safe='')}/sfm-quality/{quote(filename, safe='')}"
    return payload


def _find_sfm_quality_image(output_dir: Path, upload: Any, filename: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+\.png", filename):
        raise FileNotFoundError("sfm quality image path invalid")
    task_dir = _task_dir_for_upload(output_dir, upload)
    candidates = [
        task_dir / "dataset" / "reports" / "quality" / filename,
        task_dir / "extracted" / "worldgs_task" / "reports" / "quality" / filename,
    ]
    output_root = output_dir.resolve()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
            resolved.relative_to(output_root)
        except (OSError, ValueError):
            continue
        if resolved.is_file():
            return resolved
    raise FileNotFoundError("sfm quality image not found")


def _save_model_result(output_dir: Path, upload: Any, file: UploadFile) -> dict[str, object]:
    original_name = Path(file.filename or "").name
    suffix = Path(original_name).suffix.lower()
    if suffix not in {".ply", ".sog"}:
        raise ValueError("只支持上传 .ply 或 .sog 模型文件")
    task_dir = _task_dir_for_upload(output_dir, upload)
    results_dir = task_dir / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    file_ext = suffix.lstrip(".")
    model_path = results_dir / f"mobile.{file_ext}"
    with model_path.open("wb") as output:
        shutil.copyfileobj(file.file, output)
    metadata = {
        "fileName": model_path.name,
        "fileExt": file_ext,
        "sizeBytes": model_path.stat().st_size,
        "path": str(model_path),
        "uploadedAt": time.time(),
    }
    (results_dir / "model_result.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2))
    return metadata


def _read_model_result(
    output_dir: Path,
    upload: Any,
    missing_ok: bool = False,
) -> Optional[dict[str, object]]:
    try:
        task_dir = _task_dir_for_upload(output_dir, upload)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise
    metadata_path = task_dir / "results" / "model_result.json"
    if not metadata_path.is_file():
        if missing_ok:
            return None
        raise FileNotFoundError("model result not found")
    try:
        payload = json.loads(metadata_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise FileNotFoundError("model result metadata invalid") from exc
    path = Path(str(payload.get("path", "")))
    if not path.is_absolute():
        path = metadata_path.parent / path
    try:
        resolved = path.resolve()
        resolved.relative_to(output_dir.resolve())
    except (OSError, ValueError) as exc:
        raise FileNotFoundError("model result path invalid") from exc
    if not resolved.is_file():
        if missing_ok:
            return None
        raise FileNotFoundError("model result not found")
    payload["path"] = str(resolved)
    payload["fileName"] = str(payload.get("fileName") or resolved.name)
    payload["fileExt"] = str(payload.get("fileExt") or resolved.suffix.lstrip("."))
    payload["sizeBytes"] = int(payload.get("sizeBytes") or resolved.stat().st_size)
    return payload


def _delete_model_result(output_dir: Path, upload: Any) -> None:
    model_result = _read_model_result(output_dir, upload)
    path = Path(str(model_result["path"]))
    results_dir = path.parent
    metadata_path = results_dir / "model_result.json"
    for candidate in (path, metadata_path):
        try:
            resolved = candidate.resolve()
            resolved.relative_to(output_dir.resolve())
        except (OSError, ValueError) as exc:
            raise FileNotFoundError("model result path invalid") from exc
        if resolved.exists():
            resolved.unlink()


def _delete_upload_task(output_dir: Path, upload: Any) -> None:
    task_dir = _task_dir_for_upload(output_dir, upload)
    try:
        resolved = task_dir.resolve()
        resolved.relative_to(output_dir.resolve())
    except (OSError, ValueError) as exc:
        raise FileNotFoundError("upload task directory path invalid") from exc
    if resolved == output_dir.resolve() or not resolved.exists():
        raise FileNotFoundError("upload task directory not found")
    shutil.rmtree(resolved)


def _task_dir_for_upload(output_dir: Path, upload: Any) -> Path:
    if hasattr(upload, "open_path"):
        open_path = Path(str(upload.open_path))
        try:
            resolved = open_path.resolve()
            resolved.relative_to(output_dir.resolve())
        except (OSError, ValueError):
            pass
        else:
            if resolved.exists():
                return resolved
    candidates = [
        upload.get("openPath"),
        Path(str(upload.get("reportPath", ""))).parent if upload.get("reportPath") else None,
        Path(str(upload.get("savePath", ""))).parent if upload.get("savePath") else None,
    ]
    for value in candidates:
        if not value:
            continue
        path = Path(str(value))
        if not path.is_absolute():
            continue
        try:
            resolved = path.resolve()
            resolved.relative_to(output_dir.resolve())
        except (OSError, ValueError):
            continue
        if resolved.exists():
            return resolved
    raise FileNotFoundError("upload task directory not found")


def _model_result_payload(
    request: Optional[Request],
    config: Optional[ReceiverConfig],
    upload_id: str,
    model_result: dict[str, object],
) -> dict[str, object]:
    download_path = f"/api/uploads/{upload_id}/result/download"
    download_url = download_path
    if request is not None and config is not None:
        download_url = _absolute_receiver_url(request, config, download_path)
    scan_params = {
        "url": download_url,
        "uploadId": upload_id,
        "fileExt": str(model_result["fileExt"]),
        "fileName": str(model_result["fileName"]),
    }
    payload = dict(model_result)
    payload["downloadUrl"] = download_path
    payload["scanUrl"] = f"worldgs://model-result?{urlencode(scan_params)}"
    return payload


def _absolute_receiver_url(request: Request, config: ReceiverConfig, path: str) -> str:
    request_host = request.url.hostname or ""
    if request_host in {"localhost", "127.0.0.1", "::1"}:
        addresses = local_lan_addresses()
        if addresses:
            return f"http://{addresses[0]}:{config.port}{path}"
    base = str(request.base_url).rstrip("/")
    return f"{base}{path}"


def _dataset_dir_for_upload(output_dir: Path, item: dict[str, object]) -> Optional[Path]:
    candidates = [
        item.get("datasetPath"),
        item.get("savePath"),
        item.get("extractedPath"),
    ]
    open_path = item.get("openPath")
    if open_path:
        candidates.append(str(Path(str(open_path)) / "dataset"))

    for value in candidates:
        if not value:
            continue
        path = Path(str(value))
        if not path.is_absolute():
            continue
        try:
            resolved = path.resolve()
            resolved.relative_to(output_dir.resolve())
        except (OSError, ValueError):
            continue
        if (resolved / "images").is_dir():
            return resolved
    return None


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


def _task_name_from_path(path: Path) -> str:
    name = path.name
    if "_" in name:
        return name.rsplit("_", 1)[0]
    return name or "WorldGS 任务包"


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _run_splat_transform(source_path: Path, output_path: Path, label: str) -> None:
    cli_path = Path("/srv/worldgs/viewer-tools/node_modules/.bin/splat-transform")
    if not cli_path.exists():
        raise HTTPException(status_code=500, detail="splat-transform is not installed")
    env = os.environ.copy()
    env["PATH"] = "/root/.nvm/versions/node/v22.22.0/bin:" + env.get("PATH", "")
    try:
        subprocess.run(
            [str(cli_path), str(source_path), str(output_path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=900,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=500, detail=f"{label} conversion timed out") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or f"{label} conversion failed").strip()
        raise HTTPException(status_code=500, detail=detail[-500:]) from exc
    if not output_path.is_file() or output_path.stat().st_size <= 0:
        raise HTTPException(status_code=500, detail=f"{label} conversion produced no output")


def _convert_model_share_ply_to_sog(source_path: Path, output_path: Path) -> None:
    _run_splat_transform(source_path, output_path, "ply to sog")


def _convert_model_share_ply_to_compressed_ply(source_path: Path, output_path: Path) -> None:
    _run_splat_transform(source_path, output_path, "ply to compressed ply")


def _validate_model_share_text(value: str, field_name: str, max_length: int, *, required: bool) -> str:
    text = (value or "").strip()
    if required and not text:
        raise HTTPException(status_code=400, detail=f"{field_name} is required")
    if len(text) > max_length:
        raise HTTPException(status_code=400, detail=f"{field_name} exceeds {max_length} characters")
    return text


def _new_model_share_id() -> str:
    token = secrets.token_urlsafe(18).replace("-", "").replace("_", "")[:24]
    return f"{MODEL_SHARE_ID_PREFIX}{token}"


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _request_public_base_url(request: Request) -> str:
    host = request.headers.get("host") or request.url.netloc
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme or "https"
    if host == "127.0.0.1:18083":
        host = "worldgs.notemeld.wiki"
        proto = "https"
    return f"{proto}://{host}".rstrip("/")


def _append_model_share_record(log_path: Path, record: dict[str, object]) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


def _find_model_share_record(log_path: Path, share_id: str) -> Optional[dict[str, object]]:
    if not share_id.startswith(MODEL_SHARE_ID_PREFIX):
        return None
    if not log_path.exists():
        return None
    found: Optional[dict[str, object]] = None
    with log_path.open("r", encoding="utf-8") as source:
        for line in source:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if item.get("id") == share_id:
                found = item
    return found


def _public_model_share_record(record: dict[str, object]) -> dict[str, object]:
    return {
        "id": record.get("id"),
        "title": record.get("title") or "WorldGS 模型",
        "description": record.get("description") or "",
        "status": record.get("status") or "ready",
        "format": record.get("format") or "sog",
        "asset_url": record.get("asset_url"),
        "cover_url": record.get("cover_url"),
        "device_model": record.get("device_model") or "",
        "created_at": record.get("created_at"),
    }


async def _save_model_share_cover(
    cover: UploadFile,
    share_dir: Path,
    share_id: str,
    request: Request,
) -> Optional[str]:
    filename = Path(cover.filename or "").name.lower()
    suffix = Path(filename).suffix
    if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
        await cover.close()
        return None
    cover_path = share_dir / f"cover{suffix}"
    total_bytes = 0
    try:
        with cover_path.open("wb") as output:
            while True:
                chunk = await cover.read(512 * 1024)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > 10 * 1024 * 1024:
                    cover_path.unlink(missing_ok=True)
                    await cover.close()
                    return None
                output.write(chunk)
    finally:
        await cover.close()
    return f"{_request_public_base_url(request)}/uploads/model-shares/{share_id}/{cover_path.name}"


def _open_in_file_manager(path: Path) -> None:
    system = platform.system()
    if system == "Darwin":
        subprocess.Popen(["open", str(path)])
    elif system == "Windows":
        os.startfile(str(path))  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", str(path)])


def create_default_app(output_dir: Path) -> FastAPI:
    return create_app(ReceiverConfig(output_dir=output_dir))
