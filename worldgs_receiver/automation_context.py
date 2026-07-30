import json
from dataclasses import dataclass
from pathlib import Path

from .automation_paths import ensure_inside_output
from .automation_platforms import AutomationPlatform
from .automation_store import UploadForAutomation


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}


@dataclass(frozen=True)
class AutomationTaskContext:
    upload_id: str
    task_name: str
    task_dir: Path
    dataset_path: Path
    images_dir: Path
    image_count: int
    platform_id: str
    platform_name: str
    entry_url: str


def build_task_context(
    output_dir: Path,
    upload: UploadForAutomation,
    platform: AutomationPlatform,
) -> AutomationTaskContext:
    task_dir = ensure_inside_output(output_dir, upload.open_path)
    dataset_path = _resolve_dataset_path(output_dir, task_dir)
    images_dir = ensure_inside_output(output_dir, dataset_path / "images")
    scene_dir = ensure_inside_output(output_dir, dataset_path / "sceneDataset")

    if not images_dir.is_dir():
        raise ValueError("未找到照片目录 dataset/images。")
    for name in ["cameras.txt", "images.txt", "points3D.txt"]:
        if not (scene_dir / name).is_file():
            raise ValueError(f"缺少空三文件 dataset/sceneDataset/{name}。")

    image_count = _count_images(images_dir)
    required = platform.min_image_count_exclusive + 1
    if image_count <= platform.min_image_count_exclusive:
        raise ValueError(f"照片数量不足，当前 {image_count} 张，至少需要 {required} 张才可训练。")

    return AutomationTaskContext(
        upload_id=upload.upload_id,
        task_name=upload.task_name,
        task_dir=task_dir,
        dataset_path=dataset_path,
        images_dir=images_dir,
        image_count=image_count,
        platform_id=platform.platform_id,
        platform_name=platform.display_name,
        entry_url=platform.entry_url,
    )


def _resolve_dataset_path(output_dir: Path, task_dir: Path) -> Path:
    report_path = task_dir / "upload_report.json"
    if report_path.is_file():
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            dataset_value = payload.get("datasetPath")
            if dataset_value:
                return ensure_inside_output(output_dir, Path(str(dataset_value)))
        except (OSError, json.JSONDecodeError, ValueError):
            pass
    return ensure_inside_output(output_dir, task_dir / "dataset")


def _count_images(images_dir: Path) -> int:
    return sum(
        1
        for path in images_dir.iterdir()
        if path.is_file() and not path.name.startswith(".") and path.suffix.lower() in IMAGE_SUFFIXES
    )
