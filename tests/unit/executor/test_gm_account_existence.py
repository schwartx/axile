"""账户不存在必须在运行策略前失败，查询异常不能伪装为不存在。"""

from unittest.mock import Mock

import pytest
from gm.api._errors import GmError
from gm.pb.account_pb2 import Accounts

from axile.executor.gm.connection import GMAccountNotFoundError
from axile.executor.gm.core.api_bridge import GMApiBridge
from axile.executor.gm.core.strategy_bridge import GMStrategyBridge, _normalize_startup_error


@pytest.mark.parametrize("ids", [[], ["target", "other"]])
def test_account_list_preserves_successful_empty_and_nonempty_results(monkeypatch, ids):
    from gm.csdk import c_sdk

    accounts = Accounts()
    for account_id in ids:
        accounts.data.add(account_id=account_id)
    monkeypatch.setattr(c_sdk, "py_gmi_get_accounts", lambda: (0, accounts.SerializeToString()))
    assert GMApiBridge.get_account_ids() == ids


@pytest.mark.parametrize("status", [1000, 1010, 1026])
def test_account_query_error_is_not_treated_as_account_not_found(monkeypatch, status):
    from gm.api import _errors
    from gm.csdk import c_sdk

    monkeypatch.setattr(c_sdk, "py_gmi_get_accounts", lambda: (status, b""))

    def check(code):
        raise GmError(code, "SDK 原始错误", "get_accounts")

    monkeypatch.setattr(_errors, "check_gm_status", check)
    with pytest.raises(GmError) as caught:
        GMApiBridge.get_account_ids()
    assert caught.value.status == status


@pytest.fixture
def sdk(monkeypatch):
    import gm.api

    for name in ("set_token", "set_serv_addr", "set_account_id"):
        monkeypatch.setattr(gm.api, name, Mock())
    run = Mock()
    monkeypatch.setattr(gm.api, "run", run)
    monkeypatch.setattr(GMApiBridge, "get_account_ids", Mock(return_value=["valid"]))
    return run


def test_missing_account_fails_before_run_and_survives_startup_wrapping(sdk):
    bridge = GMStrategyBridge(token="synthetic", account_id="missing", callback_dispatcher=Mock())
    observations = []
    bridge.connection_monitor.bind(observations.append)
    bridge._run_strategy_loop()
    sdk.assert_not_called()
    assert not bridge._ready_event.is_set()
    assert isinstance(bridge._startup_error, GMAccountNotFoundError)
    assert observations[-1].label == "账户不存在"
    assert observations[-1].message == "账户不存在"
    normalized = _normalize_startup_error(bridge._startup_error)
    assert normalized.execution_error == "账户不存在"
    bridge.connection_monitor.failed(normalized)
    assert observations[-1].label == "账户不存在"
    assert observations[-1].presentation["title"] == "账户不存在"
    assert "missing" in observations[-1].error["technical_detail"]


def test_existing_account_proceeds_to_sdk_run(sdk):
    bridge = GMStrategyBridge(token="synthetic", account_id="valid", callback_dispatcher=Mock())
    bridge._execute_strategy("gm_strategy.py")
    sdk.assert_called_once()
    assert bridge._startup_error is None


def test_account_query_authentication_failure_keeps_sdk_error_and_never_runs(sdk, monkeypatch):
    error = GmError(1000, "SDK 原始错误", "get_accounts")
    monkeypatch.setattr(GMApiBridge, "get_account_ids", Mock(side_effect=error))
    bridge = GMStrategyBridge(token="synthetic", account_id="valid", callback_dispatcher=Mock())
    observations = []
    bridge.connection_monitor.bind(observations.append)
    bridge._run_strategy_loop()
    sdk.assert_not_called()
    assert bridge._startup_error is error
    assert observations[-1].label == "Token 无效"
