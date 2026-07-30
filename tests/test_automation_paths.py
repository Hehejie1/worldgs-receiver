from pathlib import Path

import pytest

from worldgs_receiver.automation_paths import (
    automation_root,
    ensure_inside_automation_root,
    ensure_inside_output,
    pointcosm_flow_path,
    pointcosm_profile_dir,
    pointcosm_record_dir,
    pointcosm_run_dir,
)


def test_pointcosm_paths_live_under_output_dir(tmp_path: Path) -> None:
    assert automation_root(tmp_path) == tmp_path / "automations" / "pointcosm"
    assert pointcosm_flow_path(tmp_path) == tmp_path / "automations" / "pointcosm" / "pointcosm_flow.yaml"
    assert pointcosm_profile_dir(tmp_path) == tmp_path / "automations" / "pointcosm" / "profile"
    assert pointcosm_record_dir(tmp_path, "rec-1") == tmp_path / "automations" / "pointcosm" / "records" / "rec-1"
    assert pointcosm_run_dir(tmp_path, "run-1") == tmp_path / "automations" / "pointcosm" / "runs" / "run-1"


def test_rejects_paths_outside_output_dir(tmp_path: Path) -> None:
    allowed = tmp_path / "2026-06-23" / "task" / "package.zip"
    allowed.parent.mkdir(parents=True)
    allowed.write_text("zip")

    assert ensure_inside_output(tmp_path, allowed) == allowed.resolve()

    with pytest.raises(ValueError, match="path is outside receiver output_dir"):
        ensure_inside_output(tmp_path, tmp_path.parent / "escape.zip")


def test_rejects_paths_outside_automation_root(tmp_path: Path) -> None:
    allowed = pointcosm_record_dir(tmp_path, "rec-1") / "record_session.json"
    allowed.parent.mkdir(parents=True)
    allowed.write_text("{}")

    assert ensure_inside_automation_root(tmp_path, allowed) == allowed.resolve()

    with pytest.raises(ValueError, match="path is outside pointcosm automation root"):
        ensure_inside_automation_root(tmp_path, tmp_path / "2026-06-23" / "task")
