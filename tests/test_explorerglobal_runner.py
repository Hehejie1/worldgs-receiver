import threading
from pathlib import Path
from typing import Optional

import worldgs_receiver.automation_runner as automation_runner
from worldgs_receiver.automation_context import AutomationTaskContext
from worldgs_receiver.automation_platforms import load_platform_from_file
from worldgs_receiver.automation_store import AutomationStore, create_platform_run_summary, read_run_summary
from worldgs_receiver.automation_runner import (
    ExplorerGlobalPageDriver,
    NetworkEvent,
    PlatformAutomationRunner,
    PlatformProfileLoginLauncher,
    evaluate_platform_observation,
    is_platform_logged_out,
)


ROOT = Path(__file__).resolve().parents[1]


class FakeLocator:
    def __init__(self, should_fail: bool = False) -> None:
        self.should_fail = should_fail
        self.filled: list[str] = []
        self.clicked = 0
        self.selected_labels: list[str] = []

    def fill(self, value: str, timeout: Optional[int] = None) -> None:
        if self.should_fail:
            raise RuntimeError("locator not found")
        self.filled.append(value)

    def click(self, timeout: Optional[int] = None) -> None:
        if self.should_fail:
            raise RuntimeError("locator not found")
        self.clicked += 1

    def select_option(self, label: str, timeout: Optional[int] = None) -> None:
        if self.should_fail:
            raise RuntimeError("locator not found")
        self.selected_labels.append(label)


class FakePage:
    def __init__(self) -> None:
        self.goto_urls: list[str] = []
        self.placeholders: dict[str, FakeLocator] = {}
        self.labels: dict[str, FakeLocator] = {}
        self.texts: dict[str, FakeLocator] = {}
        self.locators: dict[str, FakeLocator] = {}
        self.uploaded_files: list[str] = []

    def goto(self, url: str) -> None:
        self.goto_urls.append(url)

    def get_by_placeholder(self, text: str) -> FakeLocator:
        return self.placeholders.setdefault(text, FakeLocator())

    def get_by_label(self, text: str) -> FakeLocator:
        return self.labels.setdefault(text, FakeLocator())

    def get_by_text(self, text: str, exact: bool = False) -> FakeLocator:
        return self.texts.setdefault(text, FakeLocator())

    def locator(self, selector: str) -> FakeLocator:
        return self.locators.setdefault(selector, FakeLocator())

    def set_input_files(self, selector: str, files: list[str]) -> None:
        self.uploaded_files = files


def _platform():
    return load_platform_from_file(ROOT / "worldgs_receiver" / "automation_platform_configs" / "explorerglobal.yaml")


def _context(tmp_path: Path) -> AutomationTaskContext:
    images_dir = tmp_path / "dataset" / "images"
    images_dir.mkdir(parents=True)
    for index in range(3):
        (images_dir / f"frame_{index:06d}.jpg").write_bytes(b"jpg")
    return AutomationTaskContext(
        upload_id="upload-1",
        task_name="job-1782265913849",
        task_dir=tmp_path,
        dataset_path=tmp_path / "dataset",
        images_dir=images_dir,
        image_count=120,
        platform_id="explorerglobal",
        platform_name="知天下",
        entry_url="https://3d.explorerglobal.cn/compute",
    )


def test_explorerglobal_driver_fills_title_and_uploads_images(tmp_path: Path) -> None:
    page = FakePage()
    platform = _platform()
    context = _context(tmp_path)
    driver = ExplorerGlobalPageDriver(platform)

    driver.open(page, context)
    driver.fill_title(page, context)
    driver.upload_images(page, context)
    driver.select_camera_type(page)
    driver.submit(page)

    assert page.goto_urls == ["https://3d.explorerglobal.cn/compute"]
    assert page.placeholders["请输入作品名称"].filled == ["job-1782265913849"]
    assert len(page.uploaded_files) == 3
    assert page.uploaded_files[0].endswith("frame_000000.jpg")
    assert page.locators["select"].selected_labels == ["透视镜头"]
    assert page.texts["上传计算"].clicked == 1


def test_explorerglobal_driver_falls_back_when_title_placeholder_changes(tmp_path: Path) -> None:
    page = FakePage()
    platform = _platform()
    context = _context(tmp_path)
    page.placeholders["请输入作品名称"] = FakeLocator(should_fail=True)
    page.labels["作品名称"] = FakeLocator()

    ExplorerGlobalPageDriver(platform).fill_title(page, context)

    assert page.labels["作品名称"].filled == ["job-1782265913849"]


def test_explorerglobal_driver_falls_back_to_clicking_custom_camera_dropdown() -> None:
    page = FakePage()
    platform = _platform()
    page.locators["select"] = FakeLocator(should_fail=True)

    ExplorerGlobalPageDriver(platform).select_camera_type(page)

    assert page.texts["请选择镜头类型"].clicked == 1
    assert page.texts["透视镜头"].clicked == 1


def test_is_platform_logged_out_detects_login_prompt() -> None:
    platform = _platform()

    assert is_platform_logged_out(platform, "请先登录后再继续") is True
    assert is_platform_logged_out(platform, "免费计算3DGS作品 上传计算") is False


def test_platform_observation_succeeds_on_submit_network_response() -> None:
    platform = _platform()
    result = evaluate_platform_observation(
        platform=platform,
        page_text="上传计算",
        network_events=[
            NetworkEvent(
                url="https://model-api.explorerglobal.cn/api/user/models/abc/submit",
                status=200,
            )
        ],
    )

    assert result.status == "succeeded"
    assert "submit" in result.reason


def test_platform_observation_fails_on_failure_text() -> None:
    platform = _platform()
    result = evaluate_platform_observation(
        platform=platform,
        page_text="上传失败，请稍后重试",
        network_events=[],
    )

    assert result.status == "failed"
    assert result.reason == "页面出现失败提示：上传失败"


def test_resolves_playwright_browsers_path_from_frozen_sidecar_resource_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    resource_root = tmp_path / "sidecar-resources"
    browsers_root = resource_root / "playwright-browsers"
    browsers_root.mkdir(parents=True)
    monkeypatch.setattr(automation_runner.sys, "frozen", True, raising=False)
    monkeypatch.setattr(automation_runner.sys, "_MEIPASS", str(resource_root), raising=False)
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)

    assert automation_runner._resolve_playwright_browsers_path() == browsers_root


def test_ensure_playwright_firefox_alias_links_expected_revision_to_existing_bundle(
    tmp_path: Path,
    monkeypatch,
) -> None:
    browsers_root = tmp_path / "playwright-browsers"
    browsers_root.mkdir()
    (browsers_root / "firefox-1532").mkdir()
    monkeypatch.setattr(automation_runner, "_playwright_firefox_revision", lambda: "1522")

    automation_runner._ensure_playwright_firefox_alias(browsers_root)

    alias = browsers_root / "firefox-1522"
    assert alias.is_symlink()
    assert alias.resolve() == (browsers_root / "firefox-1532").resolve()


def test_login_launcher_treats_external_firefox_profile_session_as_already_open(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class FakeFirefox:
        def launch_persistent_context(self, **kwargs):
            raise RuntimeError("Failed to launch the browser process")

    class FakePlaywright:
        def __init__(self) -> None:
            self.firefox = FakeFirefox()
            self.stopped = False

        def stop(self) -> None:
            self.stopped = True

    fake_playwright = FakePlaywright()

    class FakeSyncPlaywright:
        started = False

        def __call__(self) -> "FakeSyncPlaywright":
            return self

        def start(self) -> FakePlaywright:
            self.started = True
            return fake_playwright

    fake_sync_playwright = FakeSyncPlaywright()
    monkeypatch.setattr(automation_runner, "_sync_playwright", lambda: fake_sync_playwright)
    monkeypatch.setattr(automation_runner, "_profile_has_active_firefox_process", lambda profile_dir: True)

    status = PlatformProfileLoginLauncher(tmp_path).open("explorerglobal", "https://3d.explorerglobal.cn/")

    assert status == "already_open"
    assert fake_sync_playwright.started is False
    assert fake_playwright.stopped is False


def test_login_launcher_removes_stale_profile_lock_before_opening_browser(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class FakePage:
        def __init__(self) -> None:
            self.urls: list[str] = []

        def goto(self, url: str) -> None:
            self.urls.append(url)

    class FakeContext:
        def __init__(self) -> None:
            self.pages = [FakePage()]

    class FakeFirefox:
        def __init__(self) -> None:
            self.context = FakeContext()
            self.user_data_dir = ""

        def launch_persistent_context(self, **kwargs):
            self.user_data_dir = str(kwargs["user_data_dir"])
            assert not (Path(self.user_data_dir) / ".parentlock").exists()
            return self.context

    class FakePlaywright:
        def __init__(self) -> None:
            self.firefox = FakeFirefox()

        def stop(self) -> None:
            return None

    fake_playwright = FakePlaywright()

    class FakeSyncPlaywright:
        def __call__(self) -> "FakeSyncPlaywright":
            return self

        def start(self) -> FakePlaywright:
            return fake_playwright

    profile_dir = automation_runner.platform_profile_dir(tmp_path, "explorerglobal")
    profile_dir.mkdir(parents=True)
    (profile_dir / ".parentlock").write_text("stale", encoding="utf-8")
    monkeypatch.setattr(automation_runner, "_sync_playwright", lambda: FakeSyncPlaywright())
    monkeypatch.setattr(automation_runner, "_profile_has_active_firefox_process", lambda profile_dir: False)

    status = PlatformProfileLoginLauncher(tmp_path).open("explorerglobal", "https://3d.explorerglobal.cn/")

    assert status == "opened"
    assert fake_playwright.firefox.context.pages[0].urls == ["https://3d.explorerglobal.cn/"]
    assert not (profile_dir / ".parentlock").exists()


def test_platform_runner_removes_stale_profile_lock_before_opening_browser(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class FakeFirefox:
        def launch_persistent_context(self, **kwargs):
            user_data_dir = Path(str(kwargs["user_data_dir"]))
            assert not (user_data_dir / ".parentlock").exists()
            raise RuntimeError("Failed to launch the browser process\nBrowser logs: firefox exited")

    class FakePlaywright:
        firefox = FakeFirefox()

        def stop(self) -> None:
            return None

    class FakeSyncPlaywright:
        def __call__(self) -> "FakeSyncPlaywright":
            return self

        def start(self) -> FakePlaywright:
            return FakePlaywright()

    store = AutomationStore(output_dir=tmp_path)
    context = _context(tmp_path)
    summary = create_platform_run_summary(store, context)
    profile_dir = automation_runner.platform_profile_dir(tmp_path, "explorerglobal")
    profile_dir.mkdir(parents=True)
    (profile_dir / ".parentlock").write_text("stale", encoding="utf-8")
    monkeypatch.setattr(automation_runner, "_sync_playwright", lambda: FakeSyncPlaywright())
    monkeypatch.setattr(automation_runner, "_profile_has_active_firefox_process", lambda profile_dir: False)

    PlatformAutomationRunner(store, _platform())._run(
        summary.automation_run_id,
        context,
        {"continue": threading.Event(), "cancel": threading.Event()},
    )

    payload = read_run_summary(store, summary.automation_run_id)
    assert payload["status"] == "failed"
    assert payload["message"] == "打开 Firefox 登录窗口失败，请关闭已有知天下登录窗口后重试。"
    assert not (profile_dir / ".parentlock").exists()


def test_login_launcher_raises_friendly_error_when_browser_resource_is_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class FakeFirefox:
        def launch_persistent_context(self, **kwargs):
            raise RuntimeError("Executable doesn't exist at /missing/firefox")

    class FakePlaywright:
        firefox = FakeFirefox()

        def stop(self) -> None:
            return None

    class FakeSyncPlaywright:
        def __call__(self) -> "FakeSyncPlaywright":
            return self

        def start(self) -> FakePlaywright:
            return FakePlaywright()

    monkeypatch.setattr(automation_runner, "_sync_playwright", lambda: FakeSyncPlaywright())
    monkeypatch.setattr(automation_runner, "_profile_has_active_firefox_process", lambda profile_dir: False)

    try:
        PlatformProfileLoginLauncher(tmp_path).open("explorerglobal", "https://3d.explorerglobal.cn/")
    except RuntimeError as exc:
        assert str(exc) == "未找到内置 Firefox 浏览器资源，请重新安装最新 WorldGS 桌面包。"
    else:
        raise AssertionError("expected RuntimeError")
