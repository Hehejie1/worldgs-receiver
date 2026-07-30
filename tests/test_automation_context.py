import json
from pathlib import Path

import pytest

from worldgs_receiver.automation_context import build_task_context
from worldgs_receiver.automation_platforms import load_platform_from_file
from worldgs_receiver.automation_store import UploadForAutomation


def _platform():
    return load_platform_from_file(Path("worldgs_receiver/automation_platform_configs/explorerglobal.yaml"))


def test_build_task_context_counts_images_from_dataset_images(tmp_path: Path) -> None:
    task_dir = tmp_path / "2026-06-24" / "job-1_abcd1234"
    images_dir = task_dir / "dataset" / "images"
    scene_dir = task_dir / "dataset" / "sceneDataset"
    images_dir.mkdir(parents=True)
    scene_dir.mkdir(parents=True)
    for index in range(90):
        (images_dir / f"frame_{index:06d}.jpg").write_bytes(b"jpg")
    (images_dir / ".DS_Store").write_text("finder")
    for name in ["cameras.txt", "images.txt", "points3D.txt"]:
        (scene_dir / name).write_text("# colmap")

    upload = UploadForAutomation(
        upload_id="upload-1",
        task_name="job-1",
        package_path=task_dir / "package.zip",
        extracted_path=task_dir / "extracted",
        open_path=task_dir,
        size_bytes=123,
    )

    context = build_task_context(tmp_path, upload, _platform())

    assert context.task_name == "job-1"
    assert context.dataset_path == task_dir / "dataset"
    assert context.images_dir == images_dir
    assert context.image_count == 90
    assert context.platform_id == "explorerglobal"


def test_build_task_context_rejects_not_enough_images(tmp_path: Path) -> None:
    task_dir = tmp_path / "2026-06-24" / "job-1_abcd1234"
    images_dir = task_dir / "dataset" / "images"
    scene_dir = task_dir / "dataset" / "sceneDataset"
    images_dir.mkdir(parents=True)
    scene_dir.mkdir(parents=True)
    for index in range(88):
        (images_dir / f"frame_{index:06d}.jpg").write_bytes(b"jpg")
    for name in ["cameras.txt", "images.txt", "points3D.txt"]:
        (scene_dir / name).write_text("# colmap")

    upload = UploadForAutomation(
        upload_id="upload-1",
        task_name="job-1",
        package_path=task_dir / "package.zip",
        extracted_path=task_dir / "extracted",
        open_path=task_dir,
        size_bytes=123,
    )

    with pytest.raises(ValueError, match="照片数量不足，当前 88 张，至少需要 89 张才可训练。"):
        build_task_context(tmp_path, upload, _platform())


def test_build_task_context_uses_dataset_path_from_report_when_present(tmp_path: Path) -> None:
    task_dir = tmp_path / "2026-06-24" / "job-1_abcd1234"
    dataset_dir = task_dir / "dataset"
    images_dir = dataset_dir / "images"
    scene_dir = dataset_dir / "sceneDataset"
    images_dir.mkdir(parents=True)
    scene_dir.mkdir(parents=True)
    for index in range(89):
        (images_dir / f"frame_{index:06d}.jpg").write_bytes(b"jpg")
    for name in ["cameras.txt", "images.txt", "points3D.txt"]:
        (scene_dir / name).write_text("# colmap")
    (task_dir / "upload_report.json").write_text(
        json.dumps({"datasetPath": str(dataset_dir)}, ensure_ascii=False),
        encoding="utf-8",
    )

    upload = UploadForAutomation(
        upload_id="upload-1",
        task_name="job-1",
        package_path=task_dir / "package.zip",
        extracted_path=task_dir / "extracted",
        open_path=task_dir,
        size_bytes=123,
    )

    context = build_task_context(tmp_path, upload, _platform())

    assert context.dataset_path == dataset_dir
    assert context.image_count == 89
