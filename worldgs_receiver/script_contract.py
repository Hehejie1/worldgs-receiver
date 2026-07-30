import json
import shlex
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any


SCRIPT_TYPE_PLATFORM = "platform"
SCRIPT_TYPE_LOCAL_TRAINING = "local_training"
SCRIPT_TYPE_GENERIC = "generic"
SCRIPT_TYPES = {
    SCRIPT_TYPE_PLATFORM,
    SCRIPT_TYPE_LOCAL_TRAINING,
    SCRIPT_TYPE_GENERIC,
}
ACTIVE_SCRIPT_RUN_STATUSES = {"queued", "running"}
SCRIPT_ENTRY_SUFFIXES = {".sh", ".bash", ".py"}
SCRIPT_UPLOAD_SUFFIXES = SCRIPT_ENTRY_SUFFIXES | {".zip"}


@dataclass(frozen=True)
class ScriptCustomAction:
    action_id: str
    name: str
    command: str

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["actionId"] = payload.pop("action_id")
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ScriptCustomAction":
        return cls(
            action_id=str(payload.get("actionId") or payload.get("action_id") or ""),
            name=str(payload.get("name") or ""),
            command=str(payload.get("command") or ""),
        )


@dataclass(frozen=True)
class ScriptDefinition:
    script_id: str
    name: str
    description: str
    script_type: str
    entry_file: str
    custom_actions: list[ScriptCustomAction]
    enabled: bool
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "scriptId": self.script_id,
            "name": self.name,
            "description": self.description,
            "scriptType": self.script_type,
            "entryFile": self.entry_file,
            "customActions": [action.to_dict() for action in self.custom_actions],
            "enabled": self.enabled,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ScriptDefinition":
        return cls(
            script_id=str(payload.get("scriptId") or ""),
            name=str(payload.get("name") or ""),
            description=str(payload.get("description") or ""),
            script_type=str(payload.get("scriptType") or SCRIPT_TYPE_GENERIC),
            entry_file=str(payload.get("entryFile") or ""),
            custom_actions=validate_script_custom_actions(payload.get("customActions")),
            enabled=bool(payload.get("enabled", True)),
            created_at=str(payload.get("createdAt") or now_iso()),
            updated_at=str(payload.get("updatedAt") or now_iso()),
        )


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_script_type(script_type: str) -> str:
    normalized = (script_type or SCRIPT_TYPE_GENERIC).strip().lower()
    if normalized not in SCRIPT_TYPES:
        raise ValueError(f"unsupported script type: {normalized}")
    return normalized


def validate_script_name(name: str) -> str:
    cleaned = (name or "").strip()
    if not cleaned:
        raise ValueError("script name is required")
    if len(cleaned) > 80:
        raise ValueError("script name is too long")
    return cleaned


def validate_script_description(description: str) -> str:
    cleaned = (description or "").strip()
    if len(cleaned) > 500:
        raise ValueError("script description is too long")
    return cleaned


def validate_uploaded_script_filename(filename: str) -> str:
    clean_name = Path(filename or "").name.strip()
    if not clean_name:
        raise ValueError("script file is required")
    if clean_name in {".", ".."}:
        raise ValueError("invalid script file name")
    if Path(clean_name).suffix.lower() not in SCRIPT_UPLOAD_SUFFIXES:
        raise ValueError("script file must use .sh, .bash, .py or .zip")
    return clean_name


def validate_script_entry_file(entry_file: str) -> str:
    raw_value = (entry_file or "").strip().replace("\\", "/")
    if not raw_value:
        raise ValueError("entry file is required for script package")
    normalized = PurePosixPath(raw_value)
    normalized_text = str(normalized)
    if normalized.is_absolute() or normalized_text in {".", ""}:
        raise ValueError("entry file must be a relative path inside the script package")
    if any(part in {"", ".", ".."} for part in normalized.parts):
        raise ValueError("entry file must be a relative path inside the script package")
    if normalized.suffix.lower() not in SCRIPT_ENTRY_SUFFIXES:
        raise ValueError("entry file must use .sh, .bash or .py")
    return normalized_text


def validate_script_custom_action_name(name: str) -> str:
    cleaned = (name or "").strip()
    if not cleaned:
        raise ValueError("custom action name is required")
    if len(cleaned) > 80:
        raise ValueError("custom action name is too long")
    return cleaned


def validate_script_command(command: str) -> str:
    raw_value = (command or "").strip()
    if not raw_value:
        raise ValueError("custom action command is required")
    try:
        parts = shlex.split(raw_value)
    except ValueError as exc:
        raise ValueError(f"invalid custom action command: {exc}") from exc
    if not parts:
        raise ValueError("custom action command is required")
    entry = validate_script_entry_file(parts[0])
    return shlex.join([entry, *parts[1:]])


def validate_script_custom_actions(raw_actions: Any) -> list[ScriptCustomAction]:
    if raw_actions in (None, ""):
        return []
    if not isinstance(raw_actions, list):
        raise ValueError("custom actions must be a list")
    actions: list[ScriptCustomAction] = []
    seen_ids: set[str] = set()
    for raw_action in raw_actions:
        if not isinstance(raw_action, dict):
            raise ValueError("custom actions must be objects")
        action_id = str(raw_action.get("actionId") or raw_action.get("action_id") or "").strip()
        if action_id:
            if not action_id.startswith("action_"):
                raise ValueError("custom action id is invalid")
        else:
            action_id = f"action_{uuid.uuid4().hex}"
        if action_id in seen_ids:
            raise ValueError("custom action id must be unique")
        seen_ids.add(action_id)
        actions.append(
            ScriptCustomAction(
                action_id=action_id,
                name=validate_script_custom_action_name(raw_action.get("name", "")),
                command=validate_script_command(raw_action.get("command", "")),
            )
        )
    return actions


def read_json_file(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))
