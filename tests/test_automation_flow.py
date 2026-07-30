from pathlib import Path

import pytest

from worldgs_receiver.automation_flow import load_flow


def test_loads_pointcosm_flow(tmp_path: Path) -> None:
    flow_file = tmp_path / "pointcosm_flow.yaml"
    flow_file.write_text(
        """
platform: pointcosm
baseUrl: https://www.pointcosm.cn/
browser:
  engine: firefox
  headed: true
  profileDir: profile
inputs:
  packagePath: "{package_path}"
  extractedPath: "{extracted_path}"
steps:
  - id: open_home
    action:
      type: goto
      url: https://www.pointcosm.cn/
    observe:
      successAny:
        - urlContains: pointcosm.cn
      timeoutSeconds: 3
      onUnknown: pause_for_user
  - id: upload_package
    action:
      type: upload
      selector: "input[type=file]"
      file: "{package_path}"
    observe:
      successAny:
        - selector: "text=上传完成"
      failureAny:
        - selector: "text=上传失败"
      timeoutSeconds: 5
      onUnknown: pause_for_user
""",
        encoding="utf-8",
    )

    flow = load_flow(flow_file)

    assert flow.platform == "pointcosm"
    assert flow.base_url == "https://www.pointcosm.cn/"
    assert flow.browser_engine == "firefox"
    assert flow.headed is True
    assert [step.step_id for step in flow.steps] == ["open_home", "upload_package"]
    assert flow.steps[1].action.type == "upload"
    assert flow.steps[1].observe.timeout_seconds == 5


def test_rejects_non_pointcosm_base_url(tmp_path: Path) -> None:
    flow_file = tmp_path / "bad.yaml"
    flow_file.write_text(
        """
platform: pointcosm
baseUrl: https://example.com/
steps: []
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="baseUrl must be https://www.pointcosm.cn/"):
        load_flow(flow_file)


def test_rejects_unknown_action_type(tmp_path: Path) -> None:
    flow_file = tmp_path / "bad.yaml"
    flow_file.write_text(
        """
platform: pointcosm
baseUrl: https://www.pointcosm.cn/
steps:
  - id: bad
    action:
      type: drag
    observe:
      successAny:
        - selector: "text=ok"
      timeoutSeconds: 1
      onUnknown: pause_for_user
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unsupported action type"):
        load_flow(flow_file)
