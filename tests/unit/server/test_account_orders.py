"""当前委托生命周期、撤单幂等及未知结果回归，不连接柜台。"""

import asyncio
from types import SimpleNamespace

import pytest

from axile.executor.models.unified_order import UnifiedOrder
from axile.server import account_orders as service
from axile.server.account_orders_runtime import public_order, run_order_command
from axile.server.db.models.account_order import OrderCancelReceipt, OrderCancelRequest
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


def test_same_order_id_on_different_symbols_keeps_both_live_orders():
    snapshot = service._merge_snapshot(
        service._OrdersState(),
        {"data": [public_order(order(symbol=s)) for s in ("rb2610", "ag2612")], "sampled_at": "2026-10-15T10:05:00"},
    )
    assert snapshot.complete and snapshot.count == 2
    assert {row.symbol for row in snapshot.data} == {"rb2610", "ag2612"}


def test_same_order_id_terminal_receipts_resolve_each_symbol_independently():
    state = service._OrdersState()
    for index, symbol in enumerate(("rb2610", "ag2612")):
        state.receipts[str(index)] = OrderCancelReceipt(
            request_id=str(index),
            order_id="native-1",
            symbol=symbol,
            state="submitted",
            message="等待回报",
            requested_at="2026-10-15T10:04:00",
        )
    snapshot = service._merge_snapshot(
        state,
        {
            "data": [],
            "resolved": [public_order(order(symbol=s, status="已撤销")) for s in ("rb2610", "ag2612")],
            "sampled_at": "2026-10-15T10:05:00",
        },
    )
    assert snapshot.complete and snapshot.count == 0
    assert all(receipt.state == "terminal" for receipt in snapshot.cancellations)
    assert all(receipt.order.symbol == receipt.symbol for receipt in snapshot.cancellations)


def test_missing_cancellation_does_not_overwrite_same_id_live_order_on_another_symbol():
    state = service._OrdersState()
    state.receipts["pending"] = OrderCancelReceipt(
        request_id="pending",
        order_id="native-1",
        symbol="rb2610",
        state="submitted",
        message="等待回报",
        requested_at="2026-10-15T10:04:00",
        order=public_order(order()),
    )
    snapshot = service._merge_snapshot(
        state, {"data": [public_order(order(symbol="ag2612"))], "sampled_at": "2026-10-15T10:05:00"}
    )
    assert not snapshot.complete and snapshot.count is None
    assert {row.symbol: row.uncertain for row in snapshot.data} == {"rb2610": True, "ag2612": False}


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


@pytest.mark.parametrize(("status", "filled"), [("已撤销", 1), ("已成交", 3)])
def test_reused_receipt_keeps_original_identity_when_later_query_confirms_terminal(monkeypatch, status, filled):
    calls = []

    async def command(_account, payload):
        calls.append(payload)
        if payload["command"] == "cancel":
            return {"state": "submitted", "message": "等待回报", "order": public_order(order())}
        return {
            "data": [],
            "resolved": [public_order(order(status=status, filled_volume=filled))],
            "sampled_at": "2026-10-15T10:05:00",
        }

    monkeypatch.setattr(service, "_command", command)

    async def run():
        account = build_account()
        first_request = OrderCancelRequest(request_id="r1", order_id="native-1", symbol="rb2610")
        first = await service.cancel_account_order(account, first_request)
        reused = await service.cancel_account_order(account, first_request.model_copy(update={"request_id": "r2"}))
        assert reused.request_id == first.request_id == "r1"
        snapshot = await service.query_account_orders(account, refresh=True)
        assert snapshot.complete and snapshot.count == 0 and snapshot.data == []
        assert len(snapshot.cancellations) == 1
        terminal = snapshot.cancellations[0]
        assert terminal.request_id == "r1" and terminal.requested_at == first.requested_at
        assert terminal.state == "terminal"
        assert terminal.order is not None and terminal.order.status == status and terminal.order.filled_volume == filled

    asyncio.run(run())
    assert [call["command"] for call in calls] == ["cancel", "query"]


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
        assert (await service.query_account_orders(account, refresh=True)).count == 1
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
        assert response.count is None and not response.complete and response.cancellations == []

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
        results = await asyncio.gather(*(service.query_account_orders(account, refresh=True) for _ in range(3)))
        assert all(result.count == 1 for result in results)
        fail = True
        with pytest.raises(RuntimeError):
            await service.query_account_orders(account, refresh=True)
        assert service._state(account).cached.data[0].order_id == "native-1"

    asyncio.run(run())
    assert len(calls) == 2


def test_terminal_verification_reads_only_tracked_order_even_when_book_is_incomplete():
    from axile.executor.account_orders import AccountOrderBook

    book = AccountOrderBook()
    book.apply(order())
    calls = []
    executor = SimpleNamespace(
        account_orders=book,
        get_pending_orders=lambda *_: pytest.fail("单笔核实不能查询全账户"),
        get_account_control_guard=lambda: None,
        reconcile_terminal_order=lambda *key: calls.append(key) or order(status="已撤销"),
    )
    result = run_order_command(executor, {"command": "verify", "tracked": [("rb2610", "native-1")]})
    assert calls == [("rb2610", "native-1")]
    assert result["resolved"][0]["status"] == "已撤销"
    assert not result["complete"]  # 单笔终态不能补足缺失的账户基线。


def test_fill_callback_during_cancel_validation_prevents_native_cancel():
    from axile.executor.account_orders import AccountOrderBook

    book = AccountOrderBook()
    book.apply(order())

    def query(_symbol):
        book.apply(order(status="已成交", filled_volume=3))
        return [order()]

    executor = SimpleNamespace(
        account_orders=book,
        get_pending_orders=query,
        get_account_control_guard=lambda: None,
        reconcile_terminal_order=lambda *_: order(status="已成交", filled_volume=3),
        cancel_order=lambda *_: pytest.fail("查询期间已确认成交，不能发送撤单"),
    )
    result = run_order_command(executor, {"command": "cancel", "symbol": "rb2610", "order_id": "native-1"})
    assert result["state"] == "terminal" and result["order"]["filled_volume"] == 3


def test_requested_terminal_is_returned_even_after_recent_terminal_window_moves_on():
    from axile.executor.account_orders import AccountOrderBook

    book = AccountOrderBook()
    book.replace([], 0)
    for index in range(513):
        book.apply(order(order_id=str(index), status="已撤销"))
    executor = SimpleNamespace(
        account_orders=book,
        get_account_control_guard=lambda: None,
        reconcile_terminal_order=lambda *_: order(order_id="0", status="已撤销"),
    )
    result = run_order_command(executor, {"command": "verify", "tracked": [("rb2610", "0")]})
    assert any(row["order_id"] == "0" for row in result["resolved"])


def test_one_shot_cancel_verification_keeps_unknown_without_resending(monkeypatch):
    calls = []

    async def command(_account, payload):
        calls.append(payload)
        return {"data": [public_order(order())], "sampled_at": "2026-10-15T10:05:00"}

    async def elapsed(_seconds):
        pass

    monkeypatch.setattr(service, "_command", command)
    monkeypatch.setattr(service.asyncio, "sleep", elapsed)

    async def run():
        account = build_account()
        state = service._state(account)
        state.receipts["r1"] = OrderCancelReceipt(
            request_id="r1",
            order_id="native-1",
            symbol="rb2610",
            state="submitted",
            message="等待回报",
            requested_at="2026-10-15T10:04:00",
            order=public_order(order()),
        )
        await service._verify_cancel(account, state, "r1")
        assert state.receipts["r1"].state == "unknown"
        assert state.receipts["r1"].order.remaining_volume == 2

    asyncio.run(run())
    assert calls == [{"command": "verify", "tracked": [("rb2610", "native-1")]}]


def test_sse_reads_do_not_wait_for_cancel_and_terminal_callback_wins_late_submission(monkeypatch):
    from axile.server import orders_live

    async def run():
        account = build_account()
        started, release = asyncio.Event(), asyncio.Event()
        current = {"data": [public_order(order())], "sampled_at": "2026-10-15T10:05:00"}
        monkeypatch.setattr(orders_live, "read_orders_snapshot", lambda _: current)
        monkeypatch.setattr(service, "_link_executions", lambda *args: asyncio.sleep(0))

        async def command(_account, _payload):
            started.set()
            await release.wait()
            return {"state": "submitted", "message": "等待回报", "order": public_order(order())}

        monkeypatch.setattr(service, "_command", command)
        await service.query_account_orders(account)
        cancel = asyncio.create_task(
            service.cancel_account_order(
                account, OrderCancelRequest(request_id="r1", symbol="rb2610", order_id="native-1")
            )
        )
        await started.wait()
        current = {"data": [], "resolved": [public_order(order(status="已撤销"))], "sampled_at": "2026-10-15T10:06:00"}
        snapshot = await asyncio.wait_for(service.query_account_orders(account), 0.2)
        assert snapshot.cancellations[0].state == "terminal"
        release.set()
        assert (await cancel).state == "terminal"

    asyncio.run(run())
