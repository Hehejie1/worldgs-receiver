import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from .automation_context import AutomationTaskContext
from .automation_flow import AutomationStep, load_flow
from .automation_paths import platform_profile_dir, pointcosm_profile_dir, pointcosm_record_dir, pointcosm_run_dir
from .automation_platforms import AutomationPlatform
from .automation_store import (
    AutomationStore,
    UploadForAutomation,
    append_run_log,
    update_run_summary,
)


@dataclass(frozen=True)
class NetworkEvent:
    url: str
    status: int


@dataclass(frozen=True)
class ObservationResult:
    status: str
    reason: str


class PointCosmRecorder:
    def __init__(self, output_dir: Path, base_url: str) -> None:
        self.output_dir = output_dir
        self.base_url = base_url
        self._sessions: dict[str, dict[str, Any]] = {}

    def start(self, record_session_id: str) -> None:
        sync_playwright = _sync_playwright()
        playwright = sync_playwright().start()
        context = playwright.firefox.launch_persistent_context(
            user_data_dir=str(pointcosm_profile_dir(self.output_dir)),
            headless=False,
        )
        page = context.pages[0] if context.pages else context.new_page()
        record_dir = pointcosm_record_dir(self.output_dir, record_session_id)
        network_log = record_dir / "network_events.jsonl"

        def handle_response(response: Any) -> None:
            _append_jsonl(
                network_log,
                {
                    "occurredAt": _now_iso(),
                    "url": response.url,
                    "status": response.status,
                },
            )

        page.on("response", handle_response)
        context.tracing.start(screenshots=True, snapshots=True, sources=False)
        page.goto(self.base_url)
        self._sessions[record_session_id] = {
            "playwright": playwright,
            "context": context,
            "page": page,
            "recordDir": record_dir,
        }
        _capture_page(page, record_dir, "record-start")

    def stop(self, record_session_id: str) -> None:
        session = self._sessions.pop(record_session_id, None)
        if not session:
            return
        page = session["page"]
        context = session["context"]
        playwright = session["playwright"]
        record_dir = session["recordDir"]
        try:
            _capture_page(page, record_dir, "record-stop")
            context.tracing.stop(path=str(record_dir / "trace.zip"))
        finally:
            context.close()
            playwright.stop()


class PointCosmAutomationRunner:
    def __init__(self, store: AutomationStore, flow_path: Path) -> None:
        self.store = store
        self.flow_path = flow_path
        self._controls: dict[str, dict[str, threading.Event]] = {}
        self._threads: dict[str, threading.Thread] = {}

    def start_background(self, run_id: str, upload: UploadForAutomation) -> None:
        control = {
            "continue": threading.Event(),
            "cancel": threading.Event(),
        }
        self._controls[run_id] = control
        thread = threading.Thread(
            target=self._run,
            args=(run_id, upload, control),
            daemon=True,
            name=f"pointcosm-run-{run_id[:8]}",
        )
        self._threads[run_id] = thread
        thread.start()

    def has_live_run(self, run_id: str) -> bool:
        thread = self._threads.get(run_id)
        return thread is not None and thread.is_alive()

    def request_continue(self, run_id: str) -> None:
        control = self._controls.get(run_id)
        if control:
            control["continue"].set()

    def request_cancel(self, run_id: str) -> None:
        control = self._controls.get(run_id)
        if control:
            control["cancel"].set()
        update_run_summary(self.store, run_id, status="cancelled", message="用户取消自动化运行")

    def _run(
        self,
        run_id: str,
        upload: UploadForAutomation,
        control: dict[str, threading.Event],
    ) -> None:
        recent_network_events: list[NetworkEvent] = []
        playwright = None
        context = None
        try:
            flow = load_flow(self.flow_path)
            sync_playwright = _sync_playwright()
            playwright = sync_playwright().start()
            context = playwright.firefox.launch_persistent_context(
                user_data_dir=str(pointcosm_profile_dir(self.store.output_dir)),
                headless=False,
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.on(
                "response",
                lambda response: recent_network_events.append(
                    NetworkEvent(url=response.url, status=response.status)
                ),
            )
            update_run_summary(self.store, run_id, status="running", message="Firefox 已启动")

            for step in flow.steps:
                if control["cancel"].is_set():
                    update_run_summary(self.store, run_id, status="cancelled", currentStepId=step.step_id)
                    return
                update_run_summary(
                    self.store,
                    run_id,
                    status="running",
                    currentStepId=step.step_id,
                    message=f"正在执行 {step.step_id}",
                )
                append_run_log(self.store, run_id, "step_started", {"stepId": step.step_id})
                _capture_run_page(page, self.store.output_dir, run_id, step.step_id, "start")
                self._execute_action(page, step, upload, control)
                outcome = self._observe_step(page, run_id, step, recent_network_events, control)
                if outcome in {"failed", "cancelled"}:
                    return

            update_run_summary(
                self.store,
                run_id,
                status="succeeded",
                pointcosmUrl=page.url,
                message="PointCosm 自动化已完成",
            )
            append_run_log(self.store, run_id, "run_succeeded", {"url": page.url})
        except Exception as exc:
            update_run_summary(self.store, run_id, status="failed", error=str(exc), message=str(exc))
            append_run_log(self.store, run_id, "run_failed", {"error": str(exc)})
        finally:
            self._controls.pop(run_id, None)
            self._threads.pop(run_id, None)
            if context is not None:
                context.close()
            if playwright is not None:
                playwright.stop()

    def _execute_action(
        self,
        page: Any,
        step: AutomationStep,
        upload: UploadForAutomation,
        control: dict[str, threading.Event],
    ) -> None:
        action = step.action
        if action.type == "goto":
            page.goto(_replace_inputs(action.url or "", upload))
        elif action.type == "click":
            page.click(action.selector or "")
        elif action.type == "upload":
            file_path = _replace_inputs(action.file or "{package_path}", upload)
            page.set_input_files(action.selector or "", file_path)
        elif action.type == "wait_for_user":
            update_run_summary(
                self.store,
                _current_run_id_from_control(self._controls, control),
                status="paused",
                currentStepId=step.step_id,
                message=action.prompt or "请在 Firefox 中完成操作后点击继续",
            )
            _wait_for_continue_or_cancel(control)
        else:
            raise ValueError(f"unsupported action type: {action.type}")

    def _observe_step(
        self,
        page: Any,
        run_id: str,
        step: AutomationStep,
        recent_network_events: list[NetworkEvent],
        control: dict[str, threading.Event],
    ) -> str:
        while True:
            deadline = time.monotonic() + step.observe.timeout_seconds
            while time.monotonic() < deadline:
                if control["cancel"].is_set():
                    update_run_summary(self.store, run_id, status="cancelled", currentStepId=step.step_id)
                    append_run_log(self.store, run_id, "run_cancelled", {"stepId": step.step_id})
                    return "cancelled"
                page_text = _body_text(page)
                result = evaluate_observation(
                    success_any=step.observe.success_any,
                    failure_any=step.observe.failure_any,
                    page_text=page_text,
                    current_url=page.url,
                    network_events=recent_network_events[-100:],
                )
                if result.status == "succeeded":
                    latest = _capture_run_page(page, self.store.output_dir, run_id, step.step_id, "succeeded")
                    update_run_summary(
                        self.store,
                        run_id,
                        status="running",
                        currentStepId=step.step_id,
                        message=result.reason,
                        latestScreenshot=latest,
                    )
                    append_run_log(self.store, run_id, "step_succeeded", {"stepId": step.step_id, "reason": result.reason})
                    return "succeeded"
                if result.status == "failed":
                    latest = _capture_run_page(page, self.store.output_dir, run_id, step.step_id, "failed")
                    update_run_summary(
                        self.store,
                        run_id,
                        status="failed",
                        currentStepId=step.step_id,
                        error=result.reason,
                        message=result.reason,
                        latestScreenshot=latest,
                    )
                    append_run_log(self.store, run_id, "step_failed", {"stepId": step.step_id, "reason": result.reason})
                    return "failed"
                page.wait_for_timeout(1000)

            latest = _capture_run_page(page, self.store.output_dir, run_id, step.step_id, "paused")
            update_run_summary(
                self.store,
                run_id,
                status="paused",
                currentStepId=step.step_id,
                message="状态未知，等待用户接管后继续",
                latestScreenshot=latest,
            )
            append_run_log(self.store, run_id, "step_paused", {"stepId": step.step_id})
            _wait_for_continue_or_cancel(control)


class ExplorerGlobalPageDriver:
    def __init__(self, platform: AutomationPlatform) -> None:
        self.platform = platform

    def open(self, page: Any, context: AutomationTaskContext) -> None:
        page.goto(context.entry_url)

    def fill_title(self, page: Any, context: AutomationTaskContext) -> None:
        candidates = [
            ("placeholder", lambda: page.get_by_placeholder(self.platform.form.title_placeholder)),
            ("label", lambda: page.get_by_label("作品名称")),
            (
                "partial-placeholder",
                lambda: _first_locator(page.locator("input[placeholder*='作品'], textarea[placeholder*='作品']")),
            ),
            ("text-input", lambda: _first_locator(page.locator("input[type='text'], textarea"))),
        ]
        errors = []
        for name, locator_factory in candidates:
            try:
                locator_factory().fill(context.task_name, timeout=5000)
                return
            except Exception as exc:
                errors.append(f"{name}: {exc}")
        raise RuntimeError("未找到作品名称输入框，已尝试 placeholder、label 和文本输入框。")

    def upload_images(self, page: Any, context: AutomationTaskContext) -> None:
        files = sorted(
            str(path)
            for path in context.images_dir.iterdir()
            if path.is_file() and not path.name.startswith(".")
        )
        try:
            with page.expect_file_chooser(timeout=3000) as chooser_info:
                page.get_by_text(self.platform.form.upload_text, exact=True).click()
            chooser_info.value.set_files(files)
            return
        except Exception:
            pass
        page.set_input_files("input[type=file]", files)

    def select_camera_type(self, page: Any) -> None:
        try:
            page.locator("select").select_option(
                label=self.platform.form.camera_type_option_text,
                timeout=5000,
            )
            return
        except Exception:
            pass

        page.get_by_text(self.platform.form.camera_type_trigger_text, exact=True).click(timeout=5000)
        try:
            page.get_by_role("option", name=self.platform.form.camera_type_option_text).click(timeout=5000)
            return
        except Exception:
            pass
        page.get_by_text(self.platform.form.camera_type_option_text, exact=True).click(timeout=5000)

    def submit(self, page: Any) -> None:
        page.get_by_text(self.platform.form.submit_text, exact=True).click()


class PlatformAutomationRunner:
    def __init__(self, store: AutomationStore, platform: AutomationPlatform) -> None:
        self.store = store
        self.platform = platform
        self._controls: dict[str, dict[str, threading.Event]] = {}
        self._threads: dict[str, threading.Thread] = {}

    def start_background(self, run_id: str, task_context: AutomationTaskContext) -> None:
        control = {
            "continue": threading.Event(),
            "cancel": threading.Event(),
        }
        self._controls[run_id] = control
        thread = threading.Thread(
            target=self._run,
            args=(run_id, task_context, control),
            daemon=True,
            name=f"{self.platform.platform_id}-run-{run_id[:8]}",
        )
        self._threads[run_id] = thread
        thread.start()

    def has_live_run(self, run_id: str) -> bool:
        thread = self._threads.get(run_id)
        return thread is not None and thread.is_alive()

    def request_continue(self, run_id: str) -> None:
        control = self._controls.get(run_id)
        if control:
            control["continue"].set()

    def request_cancel(self, run_id: str) -> None:
        control = self._controls.get(run_id)
        if control:
            control["cancel"].set()
        update_run_summary(self.store, run_id, status="cancelled", message="用户取消自动化运行")

    def _run(
        self,
        run_id: str,
        task_context: AutomationTaskContext,
        control: dict[str, threading.Event],
    ) -> None:
        recent_network_events: list[NetworkEvent] = []
        playwright = None
        context = None
        driver = ExplorerGlobalPageDriver(self.platform)
        try:
            profile_dir = platform_profile_dir(self.store.output_dir, self.platform.platform_id)
            _wait_for_profile_available(profile_dir)
            sync_playwright = _sync_playwright()
            playwright = sync_playwright().start()
            try:
                context = playwright.firefox.launch_persistent_context(
                    user_data_dir=str(profile_dir),
                    headless=False,
                )
            except Exception as exc:
                playwright.stop()
                playwright = None
                raise RuntimeError(_playwright_launch_error_message(exc)) from exc
            page = context.pages[0] if context.pages else context.new_page()
            page.on(
                "response",
                lambda response: recent_network_events.append(
                    NetworkEvent(url=response.url, status=response.status)
                ),
            )
            page.on("dialog", lambda dialog: dialog.accept())
            update_run_summary(self.store, run_id, status="running", message="Firefox 已启动")

            driver.open(page, task_context)
            if not self._ensure_logged_in(page, driver, task_context, run_id, control):
                return

            update_run_summary(self.store, run_id, status="running", currentStepId="fill_title", message="正在填写作品名称")
            driver.fill_title(page, task_context)
            update_run_summary(self.store, run_id, currentStepId="upload_images", message="正在上传照片到知天下，请不要关闭 Firefox。")
            driver.upload_images(page, task_context)
            update_run_summary(self.store, run_id, currentStepId="select_camera_type", message="正在选择透视镜头")
            driver.select_camera_type(page)
            update_run_summary(self.store, run_id, currentStepId="submit", message="正在提交计算")
            driver.submit(page)

            self._observe_submit(page, run_id, recent_network_events, control)
        except Exception as exc:
            update_run_summary(self.store, run_id, status="failed", error=str(exc), message=str(exc))
            append_run_log(self.store, run_id, "run_failed", {"error": str(exc)})
        finally:
            self._controls.pop(run_id, None)
            self._threads.pop(run_id, None)
            if context is not None:
                context.close()
            if playwright is not None:
                playwright.stop()

    def _ensure_logged_in(
        self,
        page: Any,
        driver: ExplorerGlobalPageDriver,
        task_context: AutomationTaskContext,
        run_id: str,
        control: dict[str, threading.Event],
    ) -> bool:
        while True:
            page.wait_for_timeout(1500)
            if not is_platform_logged_out(self.platform, _body_text(page)):
                return True
            latest = _capture_run_page(page, self.store.output_dir, run_id, "login", "paused")
            update_run_summary(
                self.store,
                run_id,
                status="paused",
                currentStepId="login",
                message=f"请先在打开的 Firefox 中登录{self.platform.display_name}，登录完成后点击继续。",
                latestScreenshot=latest,
            )
            _wait_for_continue_or_cancel(control)
            if control["cancel"].is_set():
                update_run_summary(self.store, run_id, status="cancelled", currentStepId="login")
                return False
            driver.open(page, task_context)

    def _observe_submit(
        self,
        page: Any,
        run_id: str,
        recent_network_events: list[NetworkEvent],
        control: dict[str, threading.Event],
    ) -> None:
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            if control["cancel"].is_set():
                update_run_summary(self.store, run_id, status="cancelled", currentStepId="submit")
                append_run_log(self.store, run_id, "run_cancelled", {"stepId": "submit"})
                return
            result = evaluate_platform_observation(
                platform=self.platform,
                page_text=_body_text(page),
                network_events=recent_network_events[-200:],
            )
            if result.status == "succeeded":
                latest = _capture_run_page(page, self.store.output_dir, run_id, "submit", "succeeded")
                update_run_summary(
                    self.store,
                    run_id,
                    status="succeeded",
                    currentStepId="submit",
                    message=result.reason,
                    latestScreenshot=latest,
                    pointcosmUrl=page.url,
                )
                append_run_log(self.store, run_id, "run_succeeded", {"reason": result.reason, "url": page.url})
                return
            if result.status == "failed":
                latest = _capture_run_page(page, self.store.output_dir, run_id, "submit", "failed")
                update_run_summary(
                    self.store,
                    run_id,
                    status="failed",
                    currentStepId="submit",
                    error=result.reason,
                    message=result.reason,
                    latestScreenshot=latest,
                )
                append_run_log(self.store, run_id, "run_failed", {"reason": result.reason})
                return
            page.wait_for_timeout(1000)

        latest = _capture_run_page(page, self.store.output_dir, run_id, "submit", "paused")
        update_run_summary(
            self.store,
            run_id,
            status="paused",
            currentStepId="submit",
            message="页面状态未确认，请在 Firefox 中检查后点击继续或取消。",
            latestScreenshot=latest,
        )
        append_run_log(self.store, run_id, "run_paused", {"stepId": "submit"})


class PlatformProfileLoginLauncher:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self._sessions: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def open(self, platform_id: str, url: str) -> str:
        with self._lock:
            self._close_locked(platform_id)
            profile_dir = platform_profile_dir(self.output_dir, platform_id)
            if _profile_has_active_firefox_process(profile_dir):
                return "already_open"
            _remove_stale_profile_locks(profile_dir)
            sync_playwright = _sync_playwright()
            playwright = sync_playwright().start()
            try:
                context = playwright.firefox.launch_persistent_context(
                    user_data_dir=str(profile_dir),
                    headless=False,
                )
            except Exception as exc:
                playwright.stop()
                if _profile_has_active_firefox_process(profile_dir):
                    return "already_open"
                raise RuntimeError(_playwright_launch_error_message(exc)) from exc
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(url)
            self._sessions[platform_id] = {
                "playwright": playwright,
                "context": context,
                "page": page,
            }
            return "opened"

    def close(self, platform_id: str) -> None:
        with self._lock:
            self._close_locked(platform_id)

    def _close_locked(self, platform_id: str) -> None:
        session = self._sessions.pop(platform_id, None)
        if not session:
            return
        context = session["context"]
        playwright = session["playwright"]
        try:
            context.close()
        finally:
            playwright.stop()


def is_platform_logged_out(platform: AutomationPlatform, page_text: str) -> bool:
    if "退出登录" in page_text:
        return False
    if platform.form.upload_text in page_text or platform.form.submit_text in page_text:
        return False
    return any(text and len(text) >= 3 and text in page_text for text in platform.login.unauthenticated_text)


def evaluate_platform_observation(
    platform: AutomationPlatform,
    page_text: str,
    network_events: list[NetworkEvent],
) -> ObservationResult:
    for text in platform.observe.failure_text:
        if text and text in page_text:
            return ObservationResult(status="failed", reason=f"页面出现失败提示：{text}")

    expected = platform.observe.success_network
    for event in network_events:
        parsed = urlparse(event.url)
        if parsed.hostname != expected.host:
            continue
        if event.status != expected.status:
            continue
        if all(part in parsed.path for part in expected.path_contains):
            return ObservationResult(status="succeeded", reason=f"检测到 submit 成功请求：{parsed.path}")

    return ObservationResult(status="unknown", reason="尚未检测到平台成功或失败信号")


def evaluate_observation(
    success_any: list[dict[str, Any]],
    failure_any: list[dict[str, Any]],
    page_text: str,
    current_url: str,
    network_events: list[NetworkEvent],
) -> ObservationResult:
    failure_reason = _match_any(failure_any, page_text, current_url, network_events, "failure")
    if failure_reason:
        return ObservationResult(status="failed", reason=failure_reason)
    success_reason = _match_any(success_any, page_text, current_url, network_events, "success")
    if success_reason:
        return ObservationResult(status="succeeded", reason=success_reason)
    return ObservationResult(status="unknown", reason="no observe condition matched")


def _match_any(
    conditions: list[dict[str, Any]],
    page_text: str,
    current_url: str,
    network_events: list[NetworkEvent],
    kind: str,
) -> Optional[str]:
    for condition in conditions:
        selector = condition.get("selector")
        if isinstance(selector, str) and selector.startswith("text="):
            text = selector.removeprefix("text=")
            if text in page_text:
                return f"matched {kind} selector {selector}"

        url_contains = condition.get("urlContains")
        if isinstance(url_contains, str) and url_contains in current_url:
            return f"matched {kind} urlContains {url_contains}"

        network = condition.get("network")
        if isinstance(network, dict) and _matches_network(network, network_events):
            return f"matched {kind} network"

    return None


def _matches_network(condition: dict[str, Any], network_events: list[NetworkEvent]) -> bool:
    url_contains = condition.get("urlContains")
    expected_status = condition.get("status")
    for event in network_events:
        if isinstance(url_contains, str) and url_contains not in event.url:
            continue
        if expected_status is not None and int(expected_status) != event.status:
            continue
        return True
    return False


def _sync_playwright() -> Any:
    browsers_path = _resolve_playwright_browsers_path()
    if browsers_path is not None:
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(browsers_path))
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("Playwright 未安装，请先运行 python -m pip install -r requirements.txt") from exc
    if browsers_path is not None:
        _ensure_playwright_firefox_alias(browsers_path)
    return sync_playwright


def _profile_has_active_firefox_process(profile_dir: Path) -> bool:
    try:
        result = subprocess.run(
            ["ps", "-axo", "command="],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except Exception:
        return False
    profile_text = str(profile_dir)
    return any(
        profile_text in line and ("firefox" in line.lower() or "nightly.app" in line.lower())
        for line in result.stdout.splitlines()
    )


def _remove_stale_profile_locks(profile_dir: Path) -> None:
    for name in (".parentlock", "lock"):
        lock_path = profile_dir / name
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass


def _wait_for_profile_available(profile_dir: Path, timeout_seconds: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while _profile_has_active_firefox_process(profile_dir):
        if time.monotonic() >= deadline:
            raise RuntimeError("知天下登录窗口仍在关闭中，请稍等几秒后重新点击开始训练。")
        time.sleep(0.2)
    _remove_stale_profile_locks(profile_dir)


def _playwright_launch_error_message(exc: Exception) -> str:
    message = str(exc)
    if "Executable doesn't exist" in message:
        return "未找到内置 Firefox 浏览器资源，请重新安装最新 WorldGS 桌面包。"
    if "Failed to launch the browser process" in message:
        return "打开 Firefox 登录窗口失败，请关闭已有知天下登录窗口后重试。"
    return message


def _resolve_playwright_browsers_path() -> Optional[Path]:
    configured = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if configured:
        return Path(configured)
    resource_root = _sidecar_resource_root()
    if resource_root is None:
        return None
    browsers_root = resource_root / "playwright-browsers"
    if browsers_root.exists():
        return browsers_root
    return None


def _ensure_playwright_firefox_alias(browsers_root: Path) -> None:
    expected_revision = _playwright_firefox_revision()
    if not expected_revision:
        return
    expected_dir = browsers_root / f"firefox-{expected_revision}"
    if expected_dir.exists():
        return
    existing_dirs = sorted(
        path for path in browsers_root.glob("firefox-*") if path.is_dir() and path.name != expected_dir.name
    )
    if not existing_dirs:
        return
    source_dir = existing_dirs[-1]
    try:
        expected_dir.symlink_to(source_dir.name)
    except OSError:
        return


def _playwright_firefox_revision() -> Optional[str]:
    try:
        import playwright
    except ImportError:
        return None
    browsers_json = Path(playwright.__file__).resolve().parent / "driver" / "package" / "browsers.json"
    if not browsers_json.exists():
        return None
    try:
        payload = json.loads(browsers_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    for browser in payload.get("browsers") or []:
        if browser.get("name") == "firefox":
            revision = browser.get("revision")
            if revision is not None:
                return str(revision)
    return None


def _sidecar_resource_root() -> Optional[Path]:
    if not getattr(sys, "frozen", False):
        return None
    meipass = getattr(sys, "_MEIPASS", None)
    if not meipass:
        return None
    return Path(meipass)


def _capture_page(page: Any, base_dir: Path, name: str) -> str:
    screenshot_path = base_dir / "screenshots" / f"{name}.png"
    dom_path = base_dir / "dom_snapshots" / f"{name}.txt"
    screenshot_path.parent.mkdir(parents=True, exist_ok=True)
    dom_path.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(screenshot_path), full_page=True)
    dom_path.write_text(_body_text(page)[:20000], encoding="utf-8")
    return str(screenshot_path)


def _capture_run_page(page: Any, output_dir: Path, run_id: str, step_id: str, phase: str) -> str:
    run_dir = pointcosm_run_dir(output_dir, run_id)
    return _capture_page(page, run_dir, f"{step_id}-{phase}")


def _first_locator(locator: Any) -> Any:
    return getattr(locator, "first", locator)


def _body_text(page: Any) -> str:
    try:
        return page.locator("body").inner_text(timeout=1000)
    except Exception:
        return ""


def _replace_inputs(value: str, upload: UploadForAutomation) -> str:
    return (
        value.replace("{package_path}", str(upload.package_path))
        .replace("{extracted_path}", str(upload.extracted_path))
    )


def _wait_for_continue_or_cancel(control: dict[str, threading.Event]) -> None:
    while not control["cancel"].is_set():
        if control["continue"].wait(timeout=0.5):
            control["continue"].clear()
            return


def _current_run_id_from_control(
    controls: dict[str, dict[str, threading.Event]],
    control: dict[str, threading.Event],
) -> str:
    for run_id, item in controls.items():
        if item is control:
            return run_id
    return ""


def _append_jsonl(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
