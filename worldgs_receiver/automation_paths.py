from pathlib import Path


def automation_root(output_dir: Path) -> Path:
    return output_dir / "automations" / "pointcosm"


def pointcosm_flow_path(output_dir: Path) -> Path:
    return automation_root(output_dir) / "pointcosm_flow.yaml"


def pointcosm_profile_dir(output_dir: Path) -> Path:
    return automation_root(output_dir) / "profile"


def pointcosm_record_dir(output_dir: Path, record_session_id: str) -> Path:
    return automation_root(output_dir) / "records" / record_session_id


def pointcosm_run_dir(output_dir: Path, automation_run_id: str) -> Path:
    return automation_root(output_dir) / "runs" / automation_run_id


def platform_automation_root(output_dir: Path, platform_id: str) -> Path:
    return output_dir / "automations" / "platforms" / _safe_segment(platform_id)


def platform_profile_dir(output_dir: Path, platform_id: str) -> Path:
    return platform_automation_root(output_dir, platform_id) / "profile"


def platform_record_dir(output_dir: Path, platform_id: str, record_session_id: str) -> Path:
    return platform_automation_root(output_dir, platform_id) / "records" / _safe_segment(record_session_id)


def platform_run_dir(output_dir: Path, platform_id: str, automation_run_id: str) -> Path:
    return platform_automation_root(output_dir, platform_id) / "runs" / _safe_segment(automation_run_id)


def ensure_inside_output(output_dir: Path, path: Path) -> Path:
    return _ensure_inside(output_dir, path, "path is outside receiver output_dir")


def ensure_inside_automation_root(output_dir: Path, path: Path) -> Path:
    return _ensure_inside(
        automation_root(output_dir),
        path,
        "path is outside pointcosm automation root",
    )


def _ensure_inside(root: Path, path: Path, message: str) -> Path:
    root_resolved = root.resolve()
    path_resolved = path.resolve()
    try:
        path_resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError(message) from exc
    return path_resolved


def _safe_segment(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value).strip("._")
    if not cleaned:
        raise ValueError("path segment is empty")
    return cleaned
