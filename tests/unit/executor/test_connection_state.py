"""连接观察只接受显式事实，不从普通业务异常推断。"""

import pytest

from axile.executor.connection_state import ConnectionMonitor, connection_error_scope, connection_observer_scope


def test_latest_connection_result_replays_without_old_error():
    observations = []
    with connection_observer_scope(observations.append):
        monitor = ConnectionMonitor()
    error = RuntimeError("连接初始化失败：原始原因")
    monitor.failed(error)
    assert connection_error_scope(error) == "account_connection"
    assert observations[-1].error["technical_detail"] == str(error)
    monitor.succeeded()
    replayed = []
    monitor.bind(replayed.append)
    assert replayed == [observations[-1]]
    assert replayed[0].error is None
    monitor.close()
    monitor.failed(RuntimeError("主动关闭后的迟到回调"))
    assert len(replayed) == 1


def test_only_connection_boundary_marks_errors_and_wrapping_keeps_scope():
    monitor = ConnectionMonitor()
    business = ValueError("资金查询业务拒绝")
    assert connection_error_scope(business) is None
    with pytest.raises(ValueError):
        with monitor.connecting():
            raise business
    try:
        raise RuntimeError("上层包装") from business
    except RuntimeError as wrapped:
        assert connection_error_scope(wrapped) == "account_connection"


def test_observer_failure_does_not_replace_connection_failure():
    monitor = ConnectionMonitor()

    def broken(_observation):
        raise OSError("状态写入失败")

    monitor.bind(broken)
    with pytest.raises(ConnectionError, match="原始连接错误"):
        with monitor.connecting():
            raise ConnectionError("原始连接错误")


def test_account_access_restores_connection_before_business_processing():
    monitor = ConnectionMonitor()
    observations = []
    monitor.bind(observations.append)
    monitor.failed(RuntimeError("旧连接故障"))
    with monitor.accessing():
        pass
    assert observations[-1].status == "ok"
    assert observations[-1].error is None
    with pytest.raises(ValueError):
        with monitor.accessing():
            raise ValueError("普通业务拒绝")
    assert observations[-1].status == "ok"


def test_late_account_response_cannot_override_new_disconnect():
    monitor = ConnectionMonitor()
    observations = []
    monitor.bind(observations.append)
    with monitor.accessing():
        monitor.failed(ConnectionError("查询期间断线"))
    assert observations[-1].status == "error"
    with monitor.accessing():
        pass
    assert observations[-1].status == "ok"


@pytest.mark.parametrize("label", [None, "", "  ", "\n\t"])
def test_missing_and_blank_labels_stay_optional(label):
    observations = []
    monitor = ConnectionMonitor()
    monitor.bind(observations.append)
    monitor.failed(RuntimeError("故障"), label=label)
    assert observations[-1].label is None


def test_custom_content_is_a_snapshot_and_success_clears_all_failure_fields():
    from axile.executor.connection_state import ConnectionObservation

    content = {"title": "测试渠道新文案", "sections": [{"steps": ["检查本次目标"]}]}
    observations = []
    monitor = ConnectionMonitor()
    monitor.bind(observations.append)
    monitor.failed(RuntimeError("原始错误"), label="  任意渠道的新标签  ", presentation=content)
    content["sections"][0]["steps"].append("稍后改动")
    assert observations[-1].label == "任意渠道的新标签"
    assert observations[-1].presentation["sections"][0]["steps"] == ["检查本次目标"]
    # 既有四个位置参数仍然分别是 status/time/message/error。
    old = ConnectionObservation("error", "time", "message", {"technical_detail": "raw"})
    assert old.message == "message" and old.error == {"technical_detail": "raw"}
    assert old.label is None and old.presentation is None
    monitor.succeeded()
    assert all(getattr(observations[-1], key) is None for key in ("label", "presentation", "message", "error"))


def test_wrapped_failure_and_outer_connecting_keep_specific_snapshot_but_new_error_replaces_it():
    from axile.executor.connection_state import preserve_connection_failure

    monitor = ConnectionMonitor()
    observations = []
    monitor.bind(observations.append)
    root = RuntimeError("最初的 SDK 证据")
    with pytest.raises(RuntimeError):
        with monitor.connecting(label="笼统失败", presentation={"title": "上层说明"}):
            monitor.failed(root, label="明确阶段失败", presentation={"title": "本次故障说明"})
            original = observations[-1]
            wrapped = RuntimeError("上层包装")
            preserve_connection_failure(wrapped, root)
            raise wrapped from root
    assert observations[-1].observed_at == original.observed_at
    assert observations[-1].presentation == original.presentation
    assert observations[-1].error["traceback"] == original.error["traceback"]
    assert "上层包装" in observations[-1].error["propagation_traceback"]
    assert "test_wrapped_failure" in observations[-1].error["propagation_traceback"]
    assert observations[-1].error["technical_detail"] == "最初的 SDK 证据"
    assert observations[-1].label == "明确阶段失败"
    monitor.failed(RuntimeError("新的独立故障"))
    assert observations[-1].label is None and observations[-1].presentation is None
    assert observations[-1].error["technical_detail"] == "新的独立故障"


@pytest.mark.parametrize(
    "content", [{}, {"sections": ["invalid"]}, {"title": 123}, {"sections": [{"fields": [{"value": object()}]}]}]
)
def test_invalid_presentation_does_not_hide_original_failure(content):
    observations = []
    monitor = ConnectionMonitor()
    monitor.bind(observations.append)
    monitor.failed(RuntimeError("连接原文"), label="自定义标签", presentation=content)
    assert observations[-1].presentation is None
    assert observations[-1].label == "自定义标签"
    assert observations[-1].error["technical_detail"] == "连接原文"


def test_presentation_builder_failure_and_racing_success_keep_connection_evidence():
    monitor = ConnectionMonitor()
    observations = []
    monitor.bind(observations.append)

    def broken():
        raise ValueError("展示构造错误")

    with monitor.accessing():
        monitor.failed(RuntimeError("查询期间断开"), label="新断线", presentation=broken)
    assert observations[-1].status == "error"
    assert observations[-1].label == "新断线" and observations[-1].presentation is None
    assert observations[-1].message == "查询期间断开"
