from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import yaml


SUPPORTED_ACTION_TYPES = {"goto", "click", "upload", "wait_for_user"}
POINTCOSM_BASE_URL = "https://www.pointcosm.cn/"


@dataclass(frozen=True)
class ActionSpec:
    type: str
    selector: Optional[str] = None
    url: Optional[str] = None
    file: Optional[str] = None
    prompt: Optional[str] = None


@dataclass(frozen=True)
class ObserveSpec:
    success_any: list[dict[str, Any]]
    failure_any: list[dict[str, Any]]
    timeout_seconds: int
    on_unknown: str


@dataclass(frozen=True)
class AutomationStep:
    step_id: str
    action: ActionSpec
    observe: ObserveSpec


@dataclass(frozen=True)
class AutomationFlow:
    platform: str
    base_url: str
    browser_engine: str
    headed: bool
    profile_dir: str
    steps: list[AutomationStep]


def load_flow(path: Path) -> AutomationFlow:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if payload.get("platform") != "pointcosm":
        raise ValueError("platform must be pointcosm")
    if payload.get("baseUrl") != POINTCOSM_BASE_URL:
        raise ValueError("baseUrl must be https://www.pointcosm.cn/")

    browser = payload.get("browser") or {}
    steps = [_parse_step(item) for item in payload.get("steps") or []]
    if not steps:
        raise ValueError("steps must not be empty")

    return AutomationFlow(
        platform="pointcosm",
        base_url=POINTCOSM_BASE_URL,
        browser_engine=str(browser.get("engine", "firefox")),
        headed=bool(browser.get("headed", True)),
        profile_dir=str(browser.get("profileDir", "profile")),
        steps=steps,
    )


def _parse_step(item: dict[str, Any]) -> AutomationStep:
    step_id = str(item.get("id") or "")
    if not step_id:
        raise ValueError("step id is required")
    action = _parse_action(item.get("action") or {})
    observe = _parse_observe(item.get("observe") or {})
    return AutomationStep(step_id=step_id, action=action, observe=observe)


def _parse_action(payload: dict[str, Any]) -> ActionSpec:
    action_type = str(payload.get("type") or "")
    if action_type not in SUPPORTED_ACTION_TYPES:
        raise ValueError(f"unsupported action type: {action_type}")
    return ActionSpec(
        type=action_type,
        selector=payload.get("selector"),
        url=payload.get("url"),
        file=payload.get("file"),
        prompt=payload.get("prompt"),
    )


def _parse_observe(payload: dict[str, Any]) -> ObserveSpec:
    timeout_seconds = int(
        payload.get("timeoutSeconds") or payload.get("timeoutMinutes", 1) * 60
    )
    return ObserveSpec(
        success_any=list(payload.get("successAny") or []),
        failure_any=list(payload.get("failureAny") or []),
        timeout_seconds=timeout_seconds,
        on_unknown=str(payload.get("onUnknown") or "pause_for_user"),
    )
