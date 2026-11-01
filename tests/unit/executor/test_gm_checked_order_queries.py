"""GM 订单集合只有原生查询成功后才可用于缺席确认，不连接账户。"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from gm.api import trade
from gm.api._errors import GmError
from gm.csdk import c_sdk
from gm.model import storage
from gm.pb.account_pb2 import Orders
from gm.pb.trade_pb2 import GetOrdersReq, GetUnfinishedOrdersReq

from axile.executor.gm.core import api_bridge
from axile.executor.gm.gm_execute import GMExecutor
from axile.executor.models.unified_input_accounts import GMAccountConfig


@pytest.fixture
def accounts(monkeypatch):
    context = SimpleNamespace(accounts={name: SimpleNamespace(id=name) for name in ("target", "other")})
    monkeypatch.setattr(storage, "context", context)
    monkeypatch.setattr(trade, "context", context)


@pytest.mark.parametrize("operation", ["get_orders", "get_unfinished_orders"])
@pytest.mark.parametrize("failed_account", ["target", "other"])
def test_native_failure_cannot_return_an_authoritative_empty_or_partial_set(
    monkeypatch, accounts, operation, failed_account
):
    request_type = GetUnfinishedOrdersReq if operation == "get_unfinished_orders" else GetOrdersReq
    payload = Orders()
    payload.data.add(cl_ord_id="one", account_id="target", symbol="SHSE.600000")

    def query(raw):
        request = request_type()
        request.ParseFromString(raw)
        return (1000, b"") if request.account_id == failed_account else (0, payload.SerializeToString())

    native_name = f"py_gmi_{operation}"
    monkeypatch.setattr(c_sdk, native_name, query)
    monkeypatch.setattr(trade, native_name, query)
    # 官方包装器把失败吞成空／部分集合，新的桥接必须让原生错误可见。
    swallowed = getattr(trade, operation)()
    assert len(swallowed) <= 1
    with pytest.raises(GmError):
        api_bridge.GMApiBridge.execute_request(operation)


@pytest.mark.parametrize("operation", ["get_orders", "get_unfinished_orders"])
@pytest.mark.parametrize("empty", [False, True])
def test_success_keeps_sdk_rows_defaults_properties_and_account_filter(monkeypatch, accounts, operation, empty):
    request_type = GetUnfinishedOrdersReq if operation == "get_unfinished_orders" else GetOrdersReq

    def query(raw):
        request = request_type()
        request.ParseFromString(raw)
        orders = Orders()
        if not empty:
            order = orders.data.add(cl_ord_id=request.account_id + "-order", account_id=request.account_id)
            order.properties["origin_module"] = "synthetic"
        return 0, orders.SerializeToString()

    native_name = f"py_gmi_{operation}"
    monkeypatch.setattr(c_sdk, native_name, query)
    monkeypatch.setattr(trade, native_name, query)
    expected = [row for row in getattr(trade, operation)() if row["account_id"] == "target"]
    actual = api_bridge.GMApiBridge.execute_request(operation, account_id="target")
    assert actual == expected
    if empty:
        assert actual == []
    else:
        assert actual[0]["cl_ord_id"] == "target-order"
        assert actual[0]["origin_module"] == "synthetic"
        assert "price" in actual[0]


@pytest.mark.parametrize("operation", ["get_orders", "get_unfinished_orders"])
def test_failed_query_never_delivers_a_complete_empty_set_to_recorder(monkeypatch, accounts, operation):
    monkeypatch.setattr(c_sdk, f"py_gmi_{operation}", lambda _request: (1000, b""))
    owner = GMExecutor(
        GMAccountConfig(connection_mode="service", account_id="target", token="offline", serv_addr="127.0.0.1:7001"),
        connect=False,
    )
    received, finished = [], []
    owner._replay_observer = lambda topic, data: received.append((topic, data))
    owner._replay_sync_generation = 1
    token = {"sync_generation": 1, "query_basis": {"start_seq": 1, "request_revision": 1}}
    owner._replay_recorder = SimpleNamespace(begin_query=lambda _scope: token, finish_query=finished.append)
    monkeypatch.setattr(
        owner,
        "_call_bridge_impl",
        lambda method, **kwargs: api_bridge.GMApiBridge.execute_request(method, account_id=kwargs["account_id"]),
    )
    with pytest.raises(GmError):
        owner._call_bridge(operation)
    assert received == []
    assert finished == [token]


@pytest.mark.parametrize("operation", ["get_orders", "get_unfinished_orders"])
@pytest.mark.parametrize("partial", [False, True])
def test_real_sdk_empty_or_partial_account_cache_cannot_confirm_target_absence(monkeypatch, operation, partial):
    monkeypatch.setattr(storage.context, "inside_accounts", {"other": SimpleNamespace(id="other")} if partial else {})
    monkeypatch.setattr(storage, "py_gmi_get_accounts", lambda: (1000, b""))
    native = Mock(return_value=(0, b""))
    monkeypatch.setattr(c_sdk, f"py_gmi_{operation}", native)
    with pytest.raises(RuntimeError, match="账户查询范围无法确认"):
        api_bridge.GMApiBridge.execute_request(operation, account_id="target")
    native.assert_not_called()


@pytest.mark.parametrize("operation", ["get_orders", "get_unfinished_orders"])
def test_target_query_does_not_depend_on_another_accounts_query_success(monkeypatch, accounts, operation):
    request_type = GetUnfinishedOrdersReq if operation == "get_unfinished_orders" else GetOrdersReq
    seen = []

    def query(raw):
        request = request_type()
        request.ParseFromString(raw)
        seen.append(request.account_id)
        assert request.account_id == "target"
        return 0, b""

    monkeypatch.setattr(c_sdk, f"py_gmi_{operation}", query)
    assert api_bridge.GMApiBridge.execute_request(operation, account_id="target") == []
    assert seen == ["target"]
