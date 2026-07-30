from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml


@dataclass(frozen=True)
class PlatformForm:
    title_placeholder: str
    title_value_template: str
    upload_text: str
    upload_source: str
    camera_type_trigger_text: str
    camera_type_option_text: str
    submit_text: str


@dataclass(frozen=True)
class PlatformLogin:
    unauthenticated_text: list[str]
    on_unauthenticated: str


@dataclass(frozen=True)
class PlatformNetworkSuccess:
    host: str
    path_contains: list[str]
    status: int


@dataclass(frozen=True)
class PlatformObserve:
    success_network: PlatformNetworkSuccess
    failure_text: list[str]


@dataclass(frozen=True)
class AutomationPlatform:
    platform_id: str
    display_name: str
    entry_url: str
    min_image_count_exclusive: int
    allowed_domains: list[str]
    profile_dir_name: str
    login: PlatformLogin
    form: PlatformForm
    observe: PlatformObserve


def load_platform_from_file(path: Path) -> AutomationPlatform:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return _platform_from_payload(payload)


def _platform_from_payload(payload: dict[str, Any]) -> AutomationPlatform:
    entry_url = str(payload.get("entryUrl") or "")
    parsed = urlparse(entry_url)
    if parsed.scheme != "https":
        raise ValueError("entryUrl must use https")

    allowed_domains = [str(item) for item in payload.get("allowedDomains") or [] if str(item)]
    if not allowed_domains:
        raise ValueError("allowedDomains is required")
    if parsed.hostname not in allowed_domains:
        raise ValueError("entryUrl host must be listed in allowedDomains")

    form = payload.get("form") or {}
    title = form.get("title") or {}
    upload = form.get("imageFolderUpload") or {}
    camera = form.get("cameraType") or {}
    submit = form.get("submit") or {}
    login = payload.get("login") or {}
    observe = payload.get("observe") or {}
    success = (observe.get("success") or {}).get("network") or {}

    return AutomationPlatform(
        platform_id=str(payload["platformId"]),
        display_name=str(payload["displayName"]),
        entry_url=entry_url,
        min_image_count_exclusive=int(payload["minImageCountExclusive"]),
        allowed_domains=allowed_domains,
        profile_dir_name=str(payload["profileDirName"]),
        login=PlatformLogin(
            unauthenticated_text=[str(item) for item in login.get("unauthenticatedText") or []],
            on_unauthenticated=str(login.get("onUnauthenticated") or "pause_for_user"),
        ),
        form=PlatformForm(
            title_placeholder=str(title["placeholder"]),
            title_value_template=str(title["valueTemplate"]),
            upload_text=str(upload["text"]),
            upload_source=str(upload["source"]),
            camera_type_trigger_text=str(camera["triggerText"]),
            camera_type_option_text=str(camera["optionText"]),
            submit_text=str(submit["text"]),
        ),
        observe=PlatformObserve(
            success_network=PlatformNetworkSuccess(
                host=str(success["host"]),
                path_contains=[str(item) for item in success.get("pathContains") or []],
                status=int(success["status"]),
            ),
            failure_text=[str(item) for item in observe.get("failureText") or []],
        ),
    )
