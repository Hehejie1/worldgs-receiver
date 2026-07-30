from pathlib import Path

import pytest

from worldgs_receiver.automation_platforms import load_platform_from_file


def test_loads_explorerglobal_platform_config() -> None:
    path = Path("worldgs_receiver/automation_platform_configs/explorerglobal.yaml")

    platform = load_platform_from_file(path)

    assert platform.platform_id == "explorerglobal"
    assert platform.display_name == "知天下"
    assert platform.entry_url == "https://3d.explorerglobal.cn/compute"
    assert platform.min_image_count_exclusive == 88
    assert platform.allowed_domains == ["3d.explorerglobal.cn", "model-api.explorerglobal.cn"]
    assert platform.form.title_placeholder == "请输入作品名称"
    assert platform.form.upload_text == "文件夹(照片)"
    assert platform.form.camera_type_trigger_text == "请选择镜头类型"
    assert platform.form.camera_type_option_text == "透视镜头"
    assert platform.form.submit_text == "上传计算"


def test_rejects_non_https_entry_url(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(
        """
platformId: bad
displayName: Bad
entryUrl: http://example.com
minImageCountExclusive: 88
allowedDomains:
  - example.com
profileDirName: bad
login:
  unauthenticatedText:
    - 请先登录
  onUnauthenticated: pause_for_user
form:
  title:
    placeholder: 请输入作品名称
    valueTemplate: "{taskName}"
  imageFolderUpload:
    text: 文件夹(照片)
    source: "{datasetImages}"
  cameraType:
    triggerText: 请选择镜头类型
    optionText: 透视镜头
  submit:
    text: 上传计算
observe:
  success:
    network:
      host: example.com
      pathContains:
        - /submit
      status: 200
  failureText:
    - 上传失败
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="entryUrl must use https"):
        load_platform_from_file(path)


def test_rejects_platform_without_allowed_domains(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(
        """
platformId: bad
displayName: Bad
entryUrl: https://example.com
minImageCountExclusive: 88
allowedDomains: []
profileDirName: bad
login:
  unauthenticatedText:
    - 请先登录
  onUnauthenticated: pause_for_user
form:
  title:
    placeholder: 请输入作品名称
    valueTemplate: "{taskName}"
  imageFolderUpload:
    text: 文件夹(照片)
    source: "{datasetImages}"
  cameraType:
    triggerText: 请选择镜头类型
    optionText: 透视镜头
  submit:
    text: 上传计算
observe:
  success:
    network:
      host: example.com
      pathContains:
        - /submit
      status: 200
  failureText:
    - 上传失败
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="allowedDomains is required"):
        load_platform_from_file(path)
