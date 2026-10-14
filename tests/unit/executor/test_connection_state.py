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
