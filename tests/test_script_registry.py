import json
import io
from pathlib import Path
from zipfile import ZipFile

import pytest

from worldgs_receiver.script_registry import ScriptRegistry


def make_script_zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with ZipFile(buffer, "w") as archive:
        for relative_path, content in files.items():
            archive.writestr(relative_path, content)
    return buffer.getvalue()


def test_script_registry_creates_and_lists_scripts(tmp_path: Path) -> None:
    registry = ScriptRegistry(tmp_path)

    created = registry.create_script(
        name="知天下脚本",
        description="自动提交知天下训练",
        script_type="platform",
        filename="run_explorerglobal.sh",
        content=b"#!/bin/sh\nexit 0\n",
    )

    assert created["name"] == "知天下脚本"
    assert created["scriptType"] == "platform"
    assert Path(str(created["entryFile"])).is_file()
    assert created["entryFileRelative"] == "run_explorerglobal.sh"
    assert created["customActions"] == []
    assert registry.list_scripts(enabled_only=True)[0]["scriptId"] == created["scriptId"]


def test_script_registry_updates_enabled_state_and_file(tmp_path: Path) -> None:
    registry = ScriptRegistry(tmp_path)
    created = registry.create_script(
        name="本地训练",
        description="初始描述",
        script_type="local_training",
        filename="run_local_training.sh",
        content=b"#!/bin/sh\nexit 0\n",
    )

    updated = registry.update_script(
        created["scriptId"],
        description="更新后的描述",
        enabled=False,
        filename="run_local_training.py",
        content=b"print('ok')\n",
    )

    assert updated["description"] == "更新后的描述"
    assert updated["enabled"] is False
    assert str(updated["entryFile"]).endswith("run_local_training.py")
    assert updated["entryFileRelative"] == "run_local_training.py"
    assert registry.list_scripts(enabled_only=True) == []


def test_script_registry_updates_custom_actions(tmp_path: Path) -> None:
    registry = ScriptRegistry(tmp_path)
    created = registry.create_script(
        name="知天下",
        description="",
        script_type="platform",
        filename="run_explorerglobal.sh",
        content=b"#!/bin/sh\nexit 0\n",
    )

    updated = registry.update_script(
        created["scriptId"],
        custom_actions=[
            {"name": "登录", "command": "run_explorerglobal.sh --login"},
            {"name": "检查登录", "command": "run_explorerglobal.sh --check"},
        ],
    )

    assert [action["name"] for action in updated["customActions"]] == ["登录", "检查登录"]
    assert updated["customActions"][0]["command"] == "run_explorerglobal.sh --login"
    assert updated["customActions"][0]["actionId"].startswith("action_")


def test_script_registry_creates_zip_bundle_with_entry_file(tmp_path: Path) -> None:
    registry = ScriptRegistry(tmp_path)

    created = registry.create_script(
        name="知天下项目脚本",
        description="包含 src 目录",
        script_type="platform",
        filename="explorerglobal.zip",
        content=make_script_zip(
            {
                "run_explorerglobal.sh": b"#!/bin/sh\npython3 ./src/main.py\n",
                "src/main.py": b"print('ok')\n",
            }
        ),
        entry_file="run_explorerglobal.sh",
    )

    entry_path = Path(str(created["entryFile"]))
    assert entry_path.is_file()
    assert (entry_path.parent / "src" / "main.py").is_file()


def test_script_registry_rejects_zip_without_entry_file(tmp_path: Path) -> None:
    registry = ScriptRegistry(tmp_path)

    with pytest.raises(ValueError, match="entry file is required"):
        registry.create_script(
            name="坏 zip 脚本",
            description="",
            script_type="platform",
            filename="broken.zip",
            content=make_script_zip({"run.sh": b"#!/bin/sh\nexit 0\n"}),
        )


def test_script_registry_deletes_script(tmp_path: Path) -> None:
    registry = ScriptRegistry(tmp_path)
    created = registry.create_script(
        name="删除测试",
        description="删除脚本",
        script_type="generic",
        filename="run.sh",
        content=b"#!/bin/sh\nexit 0\n",
    )

    registry.delete_script(created["scriptId"])

    assert registry.list_scripts() == []
    with pytest.raises(FileNotFoundError):
        registry.get_script(created["scriptId"])


def test_script_registry_rejects_invalid_extension(tmp_path: Path) -> None:
    registry = ScriptRegistry(tmp_path)

    with pytest.raises(ValueError, match="script file must use"):
        registry.create_script(
            name="坏脚本",
            description="",
            script_type="generic",
            filename="bad.txt",
            content=b"hello",
        )
