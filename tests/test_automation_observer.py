from worldgs_receiver.automation_runner import NetworkEvent, evaluate_observation


def test_observation_succeeds_on_text_selector() -> None:
    result = evaluate_observation(
        success_any=[{"selector": "text=上传完成"}],
        failure_any=[],
        page_text="文件上传完成，可以开始训练",
        current_url="https://www.pointcosm.cn/task/1",
        network_events=[],
    )

    assert result.status == "succeeded"
    assert result.reason == "matched success selector text=上传完成"


def test_observation_fails_on_failure_text() -> None:
    result = evaluate_observation(
        success_any=[{"selector": "text=上传完成"}],
        failure_any=[{"selector": "text=上传失败"}],
        page_text="上传失败，请重试",
        current_url="https://www.pointcosm.cn/upload",
        network_events=[],
    )

    assert result.status == "failed"
    assert result.reason == "matched failure selector text=上传失败"


def test_observation_succeeds_on_network_event() -> None:
    result = evaluate_observation(
        success_any=[{"network": {"urlContains": "upload", "status": 200}}],
        failure_any=[],
        page_text="",
        current_url="https://www.pointcosm.cn/upload",
        network_events=[NetworkEvent(url="https://www.pointcosm.cn/api/upload", status=200)],
    )

    assert result.status == "succeeded"


def test_observation_unknown_when_nothing_matches() -> None:
    result = evaluate_observation(
        success_any=[{"urlContains": "task"}],
        failure_any=[],
        page_text="处理中",
        current_url="https://www.pointcosm.cn/upload",
        network_events=[],
    )

    assert result.status == "unknown"
