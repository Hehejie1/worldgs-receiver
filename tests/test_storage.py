import json
import zipfile
from pathlib import Path

from worldgs_receiver.storage import save_package


def test_save_package_writes_package_extracts_and_report(tmp_path: Path) -> None:
    package = tmp_path / "input.zip"
    with zipfile.ZipFile(package, "w") as zip_file:
        zip_file.writestr("worldgs_task/manifest.json", '{"jobId":"job-001","jobName":"room"}')
        zip_file.writestr("worldgs_task/images/frame_000001.jpg", "image")

    result = save_package(
        output_dir=tmp_path / "imports",
        filename="worldgs_job-001.zip",
        content=package.read_bytes(),
        expected_sha256=None,
        device_name="android-test",
    )

    assert result.package_path.is_file()
    assert result.extracted_dir.is_dir()
    report = json.loads(result.report_path.read_text())
    assert report["sha256"] == result.sha256
    assert report["deviceName"] == "android-test"
    assert report["ok"] is True


def test_save_package_rejects_missing_manifest(tmp_path: Path) -> None:
    package = tmp_path / "input.zip"
    with zipfile.ZipFile(package, "w") as zip_file:
        zip_file.writestr("worldgs_task/images/frame_000001.jpg", "image")

    try:
        save_package(
            output_dir=tmp_path / "imports",
            filename="worldgs_job-001.zip",
            content=package.read_bytes(),
            expected_sha256=None,
            device_name="android-test",
        )
    except ValueError as exc:
        assert "manifest" in str(exc)
    else:
        raise AssertionError("missing manifest should fail")


def test_save_package_rejects_zip_path_traversal(tmp_path: Path) -> None:
    package = tmp_path / "bad.zip"
    with zipfile.ZipFile(package, "w") as zip_file:
        zip_file.writestr("worldgs_task/manifest.json", "{}")
        zip_file.writestr("../escape.txt", "bad")

    try:
        save_package(
            output_dir=tmp_path / "imports",
            filename="bad.zip",
            content=package.read_bytes(),
            expected_sha256=None,
            device_name="android-test",
        )
    except ValueError as exc:
        assert "invalid zip member path" in str(exc)
    else:
        raise AssertionError("zip path traversal should fail")


def test_save_package_rejects_too_many_zip_members(tmp_path: Path) -> None:
    package = tmp_path / "too-many.zip"
    with zipfile.ZipFile(package, "w") as zip_file:
        zip_file.writestr("worldgs_task/manifest.json", "{}")
        for index in range(5001):
            zip_file.writestr(f"worldgs_task/images/frame_{index}.jpg", "x")

    try:
        save_package(
            output_dir=tmp_path / "imports",
            filename="too-many.zip",
            content=package.read_bytes(),
            expected_sha256=None,
            device_name="android-test",
        )
    except ValueError as exc:
        assert "too many zip members" in str(exc)
    else:
        raise AssertionError("zip member count limit should fail")
