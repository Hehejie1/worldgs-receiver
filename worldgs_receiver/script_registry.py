import json
import io
import shutil
import uuid
from pathlib import Path
from typing import Optional
from zipfile import ZipFile

from .script_contract import (
    SCRIPT_TYPE_GENERIC,
    ScriptDefinition,
    now_iso,
    validate_script_description,
    validate_script_custom_actions,
    validate_script_entry_file,
    validate_script_name,
    validate_script_type,
    validate_uploaded_script_filename,
)


class ScriptRegistry:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.registry_path = output_dir / "script_registry.json"
        self.scripts_root = output_dir / "scripts"

    def list_scripts(self, *, enabled_only: bool = False) -> list[dict[str, object]]:
        scripts = [self._definition_to_public_dict(definition) for definition in self._read_all()]
        if enabled_only:
            scripts = [item for item in scripts if bool(item.get("enabled", True))]
        return scripts

    def get_script(self, script_id: str) -> dict[str, object]:
        for definition in self._read_all():
            if definition.script_id == script_id:
                return self._definition_to_public_dict(definition)
        raise FileNotFoundError(f"script not found: {script_id}")

    def create_script(
        self,
        *,
        name: str,
        description: str,
        script_type: str,
        filename: str,
        content: bytes,
        entry_file: Optional[str] = None,
        custom_actions: Optional[list[dict[str, object]]] = None,
        enabled: bool = True,
    ) -> dict[str, object]:
        validated_name = validate_script_name(name)
        validated_description = validate_script_description(description)
        validated_type = validate_script_type(script_type or SCRIPT_TYPE_GENERIC)
        validated_filename = validate_uploaded_script_filename(filename)
        validated_custom_actions = validate_script_custom_actions(custom_actions)
        if not content:
            raise ValueError("script file content is empty")

        script_id = f"script_{uuid.uuid4().hex}"
        created_at = now_iso()
        script_dir = self._script_dir(script_id)
        script_dir.mkdir(parents=True, exist_ok=False)
        entry_path = self._materialize_script(
            script_dir=script_dir,
            filename=validated_filename,
            content=content,
            entry_file=entry_file,
        )

        definition = ScriptDefinition(
            script_id=script_id,
            name=validated_name,
            description=validated_description,
            script_type=validated_type,
            entry_file=str(entry_path),
            custom_actions=validated_custom_actions,
            enabled=enabled,
            created_at=created_at,
            updated_at=created_at,
        )
        definitions = self._read_all()
        definitions.append(definition)
        self._write_all(definitions)
        return self._definition_to_public_dict(definition)

    def update_script(
        self,
        script_id: str,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        script_type: Optional[str] = None,
        enabled: Optional[bool] = None,
        filename: Optional[str] = None,
        content: Optional[bytes] = None,
        entry_file: Optional[str] = None,
        custom_actions: Optional[list[dict[str, object]]] = None,
    ) -> dict[str, object]:
        definitions = self._read_all()
        updated: Optional[ScriptDefinition] = None
        for index, definition in enumerate(definitions):
            if definition.script_id != script_id:
                continue
            next_entry_file = definition.entry_file
            if filename is not None or content is not None:
                if filename is None or content is None:
                    raise ValueError("script filename and content must be updated together")
                validated_filename = validate_uploaded_script_filename(filename)
                if not content:
                    raise ValueError("script file content is empty")
                script_dir = self._script_dir(definition.script_id)
                shutil.rmtree(script_dir, ignore_errors=True)
                script_dir.mkdir(parents=True, exist_ok=True)
                entry_path = self._materialize_script(
                    script_dir=script_dir,
                    filename=validated_filename,
                    content=content,
                    entry_file=entry_file,
                )
                next_entry_file = str(entry_path)
            elif entry_file is not None:
                entry_path = self._resolve_existing_entry_file(self._script_dir(definition.script_id), entry_file)
                next_entry_file = str(entry_path)
            updated = ScriptDefinition(
                script_id=definition.script_id,
                name=validate_script_name(name) if name is not None else definition.name,
                description=validate_script_description(description) if description is not None else definition.description,
                script_type=validate_script_type(script_type) if script_type is not None else definition.script_type,
                entry_file=next_entry_file,
                custom_actions=(
                    validate_script_custom_actions(custom_actions)
                    if custom_actions is not None
                    else definition.custom_actions
                ),
                enabled=definition.enabled if enabled is None else bool(enabled),
                created_at=definition.created_at,
                updated_at=now_iso(),
            )
            definitions[index] = updated
            break
        if updated is None:
            raise FileNotFoundError(f"script not found: {script_id}")
        self._write_all(definitions)
        return self._definition_to_public_dict(updated)

    def delete_script(self, script_id: str) -> None:
        definitions = self._read_all()
        kept: list[ScriptDefinition] = []
        deleted: Optional[ScriptDefinition] = None
        for definition in definitions:
            if definition.script_id == script_id:
                deleted = definition
                continue
            kept.append(definition)
        if deleted is None:
            raise FileNotFoundError(f"script not found: {script_id}")
        self._write_all(kept)
        shutil.rmtree(self._script_dir(deleted.script_id), ignore_errors=True)

    def _read_all(self) -> list[ScriptDefinition]:
        if not self.registry_path.is_file():
            return []
        try:
            payload = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        items = payload.get("scripts") if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            return []
        definitions: list[ScriptDefinition] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                definition = ScriptDefinition.from_dict(item)
            except Exception:
                continue
            definitions.append(definition)
        return definitions

    def _write_all(self, definitions: list[ScriptDefinition]) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.scripts_root.mkdir(parents=True, exist_ok=True)
        payload = {"scripts": [definition.to_dict() for definition in definitions]}
        self.registry_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _script_dir(self, script_id: str) -> Path:
        return self.scripts_root / script_id

    def get_script_dir(self, script_id: str) -> Path:
        script_dir = self._script_dir(script_id)
        if not script_dir.exists():
            raise FileNotFoundError(f"script not found: {script_id}")
        return script_dir

    def _definition_to_public_dict(self, definition: ScriptDefinition) -> dict[str, object]:
        payload = definition.to_dict()
        entry_path = Path(definition.entry_file)
        try:
            payload["entryFileRelative"] = str(entry_path.resolve().relative_to(self._script_dir(definition.script_id).resolve()))
        except ValueError:
            payload["entryFileRelative"] = entry_path.name
        return payload

    def _materialize_script(
        self,
        *,
        script_dir: Path,
        filename: str,
        content: bytes,
        entry_file: Optional[str],
    ) -> Path:
        if Path(filename).suffix.lower() == ".zip":
            self._extract_script_package(script_dir, content)
            return self._resolve_existing_entry_file(script_dir, entry_file or "")
        if entry_file and Path(entry_file).name.strip() != filename:
            raise ValueError("entry file is only supported for .zip script packages")
        entry_path = script_dir / filename
        entry_path.write_bytes(content)
        entry_path.chmod(entry_path.stat().st_mode | 0o755)
        return entry_path

    def _extract_script_package(self, script_dir: Path, content: bytes) -> None:
        try:
            archive = ZipFile(io.BytesIO(content))
        except Exception as exc:
            raise ValueError("invalid script package zip") from exc
        extracted_file_count = 0
        with archive:
            for info in archive.infolist():
                raw_name = info.filename.replace("\\", "/").strip("/")
                if not raw_name:
                    continue
                target_path = self._resolve_package_member_path(script_dir, raw_name)
                if info.is_dir():
                    target_path.mkdir(parents=True, exist_ok=True)
                    continue
                target_path.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info, "r") as source, target_path.open("wb") as destination:
                    shutil.copyfileobj(source, destination)
                extracted_file_count += 1
        if extracted_file_count == 0:
            raise ValueError("script package zip is empty")

    def _resolve_existing_entry_file(self, script_dir: Path, entry_file: str) -> Path:
        relative_entry = validate_script_entry_file(entry_file)
        entry_path = self._resolve_package_member_path(script_dir, relative_entry)
        if not entry_path.is_file():
            raise ValueError(f"entry file not found in script package: {relative_entry}")
        entry_path.chmod(entry_path.stat().st_mode | 0o755)
        return entry_path

    def _resolve_package_member_path(self, script_dir: Path, member_path: str) -> Path:
        target_path = (script_dir / member_path).resolve()
        try:
            target_path.relative_to(script_dir.resolve())
        except ValueError as exc:
            raise ValueError("script package contains a path outside the target directory") from exc
        return target_path
