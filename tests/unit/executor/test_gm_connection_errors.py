"""GM 已知错误码的展示与启动异常传播保持一致。"""

import pytest
from gm.api._errors import GmError

from axile.executor.connection_state import ConnectionMonitor
from axile.executor.gm.connection import report_runtime_failure
from axile.executor.gm.core.strategy_bridge import GMAuthenticationError, GMBridgeStartupError, _normalize_startup_error


def test_token_update_failure_survives_startup_wrapping_with_sdk_evidence():
    observations = []
    monitor = ConnectionMonitor()
    monitor.bind(observations.append)
    try:
        try:
            raise GmError(1026, "更新令牌错误", "run")
        except GmError as error:
            report_runtime_failure(monitor, error)
            wrapped = _normalize_startup_error(error)
            assert type(wrapped) is GMBridgeStartupError
            assert wrapped.execution_error == "GM 令牌更新失败（错误码 1026）"
            raise wrapped from error
    except GMBridgeStartupError as error:
        # 驻留连接边界再次上报时，必须保留最初的分类、SDK 原文和传播栈。
        monitor.failed(error)

    current = observations[-1]
    assert current.label == "令牌更新失败"
    assert current.message == "GM 令牌更新失败（错误码 1026）"
    assert current.presentation["title"] == "令牌更新失败"
    assert "尚无法确认" in current.presentation["summary"]
    facts = {field["label"]: field["value"] for field in current.presentation["sections"][0]["fields"]}
    assert facts == {"失败阶段": "令牌更新", "错误码": "1026", "SDK 说明": "更新令牌错误", "调用": "run"}
    assert current.error["status"] == 1026
    assert current.error["exception_type"] == "GmError"
    assert "更新令牌错误" in current.error["technical_detail"]
    assert "GmError" in current.error["traceback"]
    assert "GMBridgeStartupError" in current.error["propagation_traceback"]
    assert "具体原因未确认" not in current.message
    assert "Token 无效" not in str(current.presentation)


@pytest.mark.parametrize(
    ("status", "message", "label", "startup_message", "error_type"),
    [
        (1000, "无效的 token", "Token 无效", "GM token 无效或已失效", GMAuthenticationError),
        (9876, "未知错误", "交易运行时异常", "GM 启动失败，具体原因未确认", GMBridgeStartupError),
    ],
)
def test_authentication_and_unknown_errors_keep_their_classification(
    status, message, label, startup_message, error_type
):
    observations = []
    monitor = ConnectionMonitor()
    monitor.bind(observations.append)
    error = GmError(status, message, "run")
    report_runtime_failure(monitor, error)
    assert observations[-1].label == label
    normalized = _normalize_startup_error(error)
    assert type(normalized) is error_type
    assert normalized.execution_error == startup_message


def test_token_update_text_without_error_code_does_not_infer_sdk_failure():
    observations = []
    monitor = ConnectionMonitor()
    monitor.bind(observations.append)
    report_runtime_failure(monitor, RuntimeError("更新令牌错误，status=1026"))
    assert observations[-1].label == "交易运行时异常"
