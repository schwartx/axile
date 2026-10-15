"""当前委托生命周期、撤单幂等及未知结果回归，不连接柜台。"""

import asyncio
from types import SimpleNamespace

import pytest

from axile.executor.models.unified_order import UnifiedOrder
from axile.server import account_orders as service
from axile.server.account_orders_runtime import public_order, run_order_command
from axile.server.db.models.account_order import OrderCancelRequest
from tests.unit.server._execution_test_support import build_account


def order(**changes):
    values = dict(
        order_id="native-1",
        symbol="rb2610",
        direction="BUY",
        order_type="LIMIT",
        volume=3,
        price=3500,
        filled_volume=1,
        status="部分成交",
        create_time="2026-10-15T10:04:00",
        extra={"offset_flag": "0", "order_ref": "12", "raw_order_data": {"password": "secret"}},
    )
    values.update(changes)
    return UnifiedOrder(**values)


@pytest.fixture(autouse=True)
def clear_state():
    service._states.clear()
    yield
    service._states.clear()


def test_public_order_has_only_whitelisted_native_identity():
    result = public_order(order())
    assert result["remaining_volume"] == 2
    assert result["native_identity"] == {"order_ref": "12"}
    assert "secret" not in str(result)
    assert "raw_order_data" not in result
    assert result["execution_id"] is None


def test_query_is_account_wide_and_cancel_requires_exact_symbol_and_identity():
    calls = []
    executor = SimpleNamespace(
        get_pending_orders=lambda symbol: (
            calls.append(symbol) or [order(), order(order_id="external", symbol="ag2612")]
        ),
        get_account_control_guard=lambda: None,
        cancel_order=lambda *key: calls.append(key) or True,
    )
    snapshot = run_order_command(executor, {"command": "query"})
    assert {row["order_id"] for row in snapshot["data"]} == {"native-1", "external"}
    assert calls == [None]
    result = run_order_command(executor, {"command": "cancel", "symbol": "ag2612", "order_id": "native-1"})
    assert result["state"] == "unknown"
    assert all(not isinstance(call, tuple) for call in calls)


def test_fill_before_cancel_is_terminal_and_never_sends_cancel():
    executor = SimpleNamespace(
        get_pending_orders=lambda _: [],
        get_account_control_guard=lambda: None,
        reconcile_terminal_order=lambda *_: order(status="已成交", filled_volume=3),
        cancel_order=lambda *_: pytest.fail("已成交订单不应撤单"),
    )
    result = run_order_command(executor, {"command": "cancel", "symbol": "rb2610", "order_id": "native-1"})
    assert result["state"] == "terminal"
    assert result["order"]["filled_volume"] == 3


def test_unknown_order_is_visible_and_cannot_be_canceled():
    executor = SimpleNamespace(
        get_pending_orders=lambda _: [order(status="状态未知")],
        cancel_order=lambda *_: pytest.fail("未知状态不能提交撤单"),
    )
    snapshot = run_order_command(executor, {"command": "query"})
    assert snapshot["data"][0]["uncertain"] is True
    result = run_order_command(executor, {"command": "cancel", "symbol": "rb2610", "order_id": "native-1"})
    assert result["state"] == "unknown"


def test_cancel_accepted_does_not_mean_canceled():
    executor = SimpleNamespace(get_pending_orders=lambda _: [order()], cancel_order=lambda *_: True)
    result = run_order_command(executor, {"command": "cancel", "symbol": "rb2610", "order_id": "native-1"})
    assert result["state"] == "submitted"
    assert result["order"]["status"] == "部分成交"
    assert result["order"]["filled_volume"] == 1


def test_async_cancel_rejection_uses_original_guard_and_preserves_live_order(monkeypatch):
    from axile.server import account_orders_runtime as runtime

    event = {
        "event_uid": "cancel-r1",
        "operation": "cancel_order",
        "symbol": "rb2610",
        "outcome": "submitted",
        "metadata": {"order_id": "native-1"},
    }
    guard = SimpleNamespace(monitor_snapshot=lambda: {"records": [event]})
    executor = SimpleNamespace(
        get_pending_orders=lambda _: [order()], get_account_control_guard=lambda: guard, cancel_order=lambda *_: True
    )
    assert (
        runtime.run_order_command(executor, {"command": "cancel", "symbol": "rb2610", "order_id": "native-1"})["state"]
        == "submitted"
    )
    event["outcome"] = "broker_rejected"
    event["metadata"]["error_msg"] = "柜台不允许撤单"
    persisted = []

    async def persist(events):
        persisted.extend(events)

    monkeypatch.setattr(runtime, "_persist_cancel_outcomes", persist)
    result = runtime.run_order_command(executor, {"command": "query", "tracked": [["rb2610", "native-1"]]})
    assert result["data"][0]["remaining_volume"] == 2
    assert result["cancel_failures"] == [{"symbol": "rb2610", "order_id": "native-1", "message": "柜台不允许撤单"}]
    assert persisted[0]["event_uid"] == "cancel-r1"


def test_gm_current_order_keeps_native_fill_and_close_semantics():
    from axile.executor.gm.common import convert_gm_order_to_unified

    native = convert_gm_order_to_unified(
        {
            "symbol": "SHSE.600000",
            "cl_ord_id": "gm1",
            "side": 2,
            "order_type": 1,
            "volume": 3,
            "filled_volume": 1,
            "filled_vwap": 12,
            "position_effect": 2,
            "price": 12,
            "status": 2,
        }
    )
    result = public_order(native)
    assert result["filled_volume"] == 1 and result["remaining_volume"] == 2
    assert result["offset"] == "CLOSE"


def test_duplicate_cancel_across_concurrent_clients_is_sent_once(monkeypatch):
    calls = []

    async def command(_account, payload):
        calls.append(payload)
        return {"state": "submitted", "message": "等待回报", "order": public_order(order())}

    monkeypatch.setattr(service, "_command", command)

    async def run():
        account = build_account()
        one = OrderCancelRequest(request_id="r1", order_id="native-1", symbol="rb2610")
        two = one.model_copy(update={"request_id": "r2"})
        receipts = await asyncio.gather(
            service.cancel_account_order(account, one), service.cancel_account_order(account, two)
        )
        assert receipts[0].request_id == receipts[1].request_id == "r1"
        await service.cancel_account_order(account, one)
        with pytest.raises(ValueError):
            await service.cancel_account_order(account, one.model_copy(update={"symbol": "other"}))

    asyncio.run(run())
    assert len(calls) == 1


def test_timeout_keeps_unknown_and_does_not_retry(monkeypatch):
    calls = []

    async def command(_account, payload):
        calls.append(payload)
        raise TimeoutError("发送后超时")

    monkeypatch.setattr(service, "_command", command)

    async def run():
        account = build_account()
        payload = OrderCancelRequest(request_id="r1", order_id="native-1", symbol="rb2610")
        first = await service.cancel_account_order(account, payload)
        second = await service.cancel_account_order(account, payload.model_copy(update={"request_id": "new"}))
        assert first.state == second.state == "unknown"

    asyncio.run(run())
    assert len(calls) == 1


def test_query_preserves_partial_fill_until_actual_terminal_after_execution(monkeypatch):
    resolved = []
    data = [public_order(order())]

    async def command(_account, payload):
        if payload["command"] == "cancel":
            return {"state": "submitted", "message": "等待回报", "order": public_order(order())}
        return {"data": list(data), "resolved": list(resolved), "sampled_at": "2026-10-15T10:05:00"}

    monkeypatch.setattr(service, "_command", command)

    async def run():
        account = build_account()
        assert (await service.query_account_orders(account)).count == 1
        await service.cancel_account_order(
            account, OrderCancelRequest(request_id="r1", order_id="native-1", symbol="rb2610")
        )
        data.clear()
        missing = await service.query_account_orders(account, refresh=True)
        assert missing.count is None and not missing.complete
        assert missing.data[0].uncertain and missing.data[0].remaining_volume == 2
        resolved.append(public_order(order(status="已撤销", filled_volume=1)))
        final = await service.query_account_orders(account, refresh=True)
        assert final.complete and final.count == 0 and final.data == []
        assert final.cancellations[0].state == "terminal"
        assert final.cancellations[0].order.filled_volume == 1
        assert final.cancellations[0].order.status == "已撤销"

    asyncio.run(run())


def test_configuration_change_does_not_leak_old_orders_or_cancel_receipts(monkeypatch):
    async def command(_account, payload):
        if payload["command"] == "cancel":
            return {"state": "submitted", "message": "等待回报", "order": public_order(order())}
        return {"data": [], "resolved": [], "sampled_at": "2026-10-15T10:06:00"}

    monkeypatch.setattr(service, "_command", command)

    async def run():
        account = build_account()
        await service.cancel_account_order(
            account, OrderCancelRequest(request_id="r1", order_id="native-1", symbol="rb2610")
        )
        account.account_config = {**account.account_config, "investor_id": "different"}
        response = await service.query_account_orders(account)
        assert response.count == 0 and response.cancellations == []

    asyncio.run(run())


def test_queries_coalesce_and_failures_do_not_replace_last_snapshot(monkeypatch):
    calls = []
    fail = False

    async def command(_account, _payload):
        calls.append(1)
        if fail:
            raise RuntimeError("柜台不可用")
        return {"data": [public_order(order())], "resolved": [], "sampled_at": "2026-10-15T10:06:00"}

    monkeypatch.setattr(service, "_command", command)

    async def run():
        nonlocal fail
        account = build_account()
        results = await asyncio.gather(*(service.query_account_orders(account) for _ in range(3)))
        assert all(result.count == 1 for result in results)
        fail = True
        with pytest.raises(RuntimeError):
            await service.query_account_orders(account, refresh=True)
        assert service._state(account).cached.data[0].order_id == "native-1"

    asyncio.run(run())
    assert len(calls) == 2
