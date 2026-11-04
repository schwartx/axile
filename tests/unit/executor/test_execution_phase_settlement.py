"""复用已有账户查询统一阶段与收尾结果，保持各渠道正常调度路径。"""

import json
from unittest.mock import Mock

import pytest

from axile.common.trade_channel import TradeChannel
from axile.executor.algorithms.utils.outcome import TARGET_NOT_REACHED, summarize_outcome
from axile.executor.constants.order_status import OrderStatus
from axile.executor.execution_engine import ExecutionEngine
from axile.executor.models.execution_result import AlgorithmResult, ExecutionStatus
from axile.executor.models.unified_account_assets import PositionDirection
from axile.executor.models.unified_input import UnifiedStandardInput
from axile.executor.models.unified_order import OrderDirection, OrderType, TradeRecord, UnifiedOrder
from tests.unit.executor.test_base_executor_symbol_dispatch import _assets, _TestExecutor

SELL = "rb2610"
BUY = "ag2612"


def assets(sell=500, buy=0):
    return _assets(
        total_asset=280000,
        positions=[
            (SELL, sell, PositionDirection.LONG),
            (BUY, buy, PositionDirection.LONG),
        ],
    )


def result(
    symbol=SELL,
    start=900,
    observed=500,
    target=500,
    filled=400,
    direction=OrderDirection.SELL,
    status=OrderStatus.FILLED,
    error=None,
):
    order = UnifiedOrder(
        order_id=symbol,
        symbol=symbol,
        direction=direction,
        order_type=OrderType.LIMIT,
        price=100,
        volume=abs(target - start),
        filled_volume=filled,
        status=status,
    )
    return AlgorithmResult(
        **summarize_outcome(start, observed, target, [order], [], explicit_error=error),
        symbol=symbol,
        algorithm="TEST",
        target_volume=target,
        orders=[order],
        memory={"debug": "unchanged"},
    )


def engine(snapshots, channel="ctp"):
    # 验证通用编排契约；各渠道均使用同一个离线替身，不连接柜台。
    owner = _TestExecutor(account_assets_snapshots=snapshots)
    owner.channel_type = TradeChannel(channel)
    owner._normalize_symbol = lambda symbol: symbol
    owner.reconcile_terminal_order = Mock(side_effect=AssertionError("不应新增订单补查"))
    runtime = Mock(memory={})
    runtime.elapsed_seconds.return_value = 1
    return ExecutionEngine(owner, runtime)


def inputs(e, buy=True, sell_target=500):
    targets = {SELL: sell_target / 2800}
    if buy:
        targets[BUY] = 0.5
    return UnifiedStandardInput(channel_type="ctp", account_config=e._owner.account_config, curr_target=targets)


def buy_result():
    return result(symbol=BUY, start=0, observed=1400, target=1400, filled=1400, direction=OrderDirection.BUY)


@pytest.mark.parametrize("channel", ["gm", "ctp", "tq", "external-plugin"])
@pytest.mark.parametrize("observed", [500, 900])
def test_two_phase_snapshot_reuse_preserves_normal_query_order(monkeypatch, channel, observed):
    e = engine([assets(sell=900), assets(), assets(buy=1400), assets(sell=1)], channel)
    trace = []
    read = e._owner.get_account_assets
    cancel = e._owner.cancel_all_orders
    monkeypatch.setattr(e._owner, "get_account_assets", lambda: trace.append("assets") or read())
    monkeypatch.setattr(e._owner, "cancel_all_orders", lambda: trace.append("cancel") or cancel())
    reduction = result(observed=observed)
    original = reduction.model_dump()
    sizing = []

    def dispatch(tasks):
        trace.append([task.symbol for task in tasks])
        sizing.extend(task.sizing for task in tasks)
        return [reduction] if tasks[0].symbol == SELL else [buy_result()]

    monkeypatch.setattr(e, "_run_prepared_symbol_algorithms", dispatch)
    output = e.run(inputs(e))
    assert trace == ["assets", "cancel", [SELL], "assets", [BUY], "assets"]
    assert e._owner.account_assets_calls == 3
    assert e._owner.market_data_requests == [[SELL, BUY], [BUY]]
    assert sizing[1].current_quantity == 0 and sizing[1].target_quantity == 1400
    assert output.status == ExecutionStatus.SUCCEEDED
    assert output.symbol_results[SELL].final_volume == 500
    assert output.symbol_results[BUY].final_volume == 1400
    assert output.symbol_results[SELL].account_assets is output.account_assets
    assert output.account_assets.get_position(BUY).volume == 1400
    assert reduction.model_dump() == original
    assert output.symbol_results[SELL].memory == {"debug": "unchanged"}
    assert output.symbol_results[SELL].outcome_reason is None
    e._owner.reconcile_terminal_order.assert_not_called()
    e._runtime.sleep_or_terminate.assert_not_called()
    decision = next(call.kwargs for call in e._runtime.emit_audit_event.call_args_list if call.kwargs["symbol"] == SELL)
    assert decision["details"]["decision"]["status"] == "SUCCEEDED"


def test_old_order_fill_during_cancellation_does_not_invalidate_completed_reduction(monkeypatch):
    e = engine([assets()])
    state = {"sell": 900, "buy": 0}
    read = Mock(side_effect=lambda: assets(**state))
    monkeypatch.setattr(e._owner, "get_account_assets", read)
    monkeypatch.setattr(e._owner, "cancel_all_orders", lambda: state.update(sell=800))
    dispatched = []

    def dispatch(tasks):
        dispatched.append([task.symbol for task in tasks])
        if tasks[0].symbol == SELL:
            state["sell"] = 500
            return [result(start=800, filled=300)]
        state["buy"] = 1400
        return [buy_result()]

    monkeypatch.setattr(e, "_run_prepared_symbol_algorithms", dispatch)
    output = e.run(inputs(e))
    assert dispatched == [[SELL], [BUY]]
    assert output.status == ExecutionStatus.SUCCEEDED
    assert output.account_assets.source == "real"
    assert output.symbol_results[SELL].final_volume == 500
    assert read.call_count == 3


@pytest.mark.parametrize("error", ["报单失败", "撤单失败", "账户控制受限"])
def test_real_error_is_preserved_and_blocks_open_even_when_target_is_reached(monkeypatch, error):
    e = engine([assets(sell=900), assets()])
    reduction = result(observed=900, error=error)
    dispatch = Mock(return_value=[reduction])
    monkeypatch.setattr(e, "_run_prepared_symbol_algorithms", dispatch)
    output = e.run(inputs(e))
    assert dispatch.call_count == 1
    assert output.symbol_results[SELL].status == reduction.status
    assert output.symbol_results[SELL].final_volume == 500
    assert output.symbol_results[SELL].error == error
    assert output.symbol_results[SELL].reason_code == reduction.reason_code
    assert output.symbol_results[BUY].status == ExecutionStatus.BLOCKED
    assert e._owner.account_assets_calls == 2


def test_confirmed_unreached_target_stays_blocked_without_polling(monkeypatch):
    e = engine([assets(sell=900), assets(sell=700)])
    dispatch = Mock(return_value=[result(observed=900, filled=200, status=OrderStatus.CANCELED)])
    monkeypatch.setattr(e, "_run_prepared_symbol_algorithms", dispatch)
    output = e.run(inputs(e))
    assert dispatch.call_count == 1
    assert output.symbol_results[SELL].reason_code == TARGET_NOT_REACHED
    assert output.symbol_results[SELL].final_volume == 700
    assert output.symbol_results[BUY].status == ExecutionStatus.BLOCKED
    assert e._owner.account_assets_calls == 2
    e._runtime.sleep_or_terminate.assert_not_called()


def test_unknown_terminal_does_not_erase_another_completed_symbol(monkeypatch):
    e = engine([assets(sell=900, buy=900), assets(buy=500)])
    unknown = result(status=OrderStatus.UNKNOWN)
    completed = result(symbol=BUY)
    monkeypatch.setattr(e, "_run_prepared_symbol_algorithms", lambda tasks: [unknown, completed])
    request = inputs(e)
    request.curr_target[BUY] = 500 / 2800
    output = e.run(request)
    assert output.status == ExecutionStatus.PARTIAL
    assert output.symbol_results[SELL].error == unknown.error
    assert output.symbol_results[SELL].orders[0].status == OrderStatus.UNKNOWN
    assert output.symbol_results[BUY].status == ExecutionStatus.SUCCEEDED
    assert output.symbol_results[BUY].final_volume == 500
    assert output.account_assets.source == "real"
    assert e._owner.account_assets_calls == 2
    e._owner.reconcile_terminal_order.assert_not_called()


def test_query_failure_preserves_existing_evidence_and_blocks_open(monkeypatch):
    e = engine([assets()])
    read = Mock(side_effect=[assets(sell=900), RuntimeError("account query failed")])
    monkeypatch.setattr(e._owner, "get_account_assets", read)
    dispatch = Mock(return_value=[result()])
    monkeypatch.setattr(e, "_run_prepared_symbol_algorithms", dispatch)
    output = e.run(inputs(e))
    assert dispatch.call_count == 1
    assert read.call_count == 2
    assert output.account_assets.source == "unavailable"
    assert output.symbol_results[SELL].final_volume == 500
    assert output.symbol_results[SELL].orders[0].filled_volume == 400
    assert output.symbol_results[BUY].status == ExecutionStatus.BLOCKED
    assert output.error


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), float("-inf")])
def test_invalid_position_blocks_open_and_preserves_serializable_evidence(monkeypatch, invalid):
    bad = assets()
    bad.positions[0].volume = invalid
    e = engine([assets(sell=900), bad])
    reduction = result()
    reduction.trades.append(
        TradeRecord.create(
            trade_id="confirmed-fill",
            symbol=SELL,
            order_id=SELL,
            trade_volume=400,
            trade_price=100,
        )
    )
    original = reduction.model_dump()
    dispatch = Mock(side_effect=[[reduction], [buy_result()]])
    monkeypatch.setattr(e, "_run_prepared_symbol_algorithms", dispatch)

    output = e.run(inputs(e))

    assert dispatch.call_count == 1
    assert output.account_assets.source == "unavailable"
    assert output.symbol_results[BUY].status == ExecutionStatus.BLOCKED
    assert output.symbol_results[BUY].error == "最终持仓尚未确认"
    assert output.symbol_results[SELL].final_volume == 500
    assert output.symbol_results[SELL].orders == reduction.orders
    assert output.symbol_results[SELL].trades == reduction.trades
    assert reduction.model_dump() == original
    assert e._owner.account_assets_calls == 2
    e._runtime.sleep_or_terminate.assert_not_called()
    e._owner.reconcile_terminal_order.assert_not_called()
    json.dumps(output.model_dump(mode="json"), allow_nan=False)


def test_final_snapshot_also_corrects_a_previously_successful_result(monkeypatch):
    e = engine([assets(sell=900), assets()])
    monkeypatch.setattr(e._owner, "_place_order_impl", Mock(side_effect=AssertionError("原本无需下单")))
    output = e.run(inputs(e, buy=False, sell_target=900))
    assert output.status == ExecutionStatus.PARTIAL
    assert output.symbol_results[SELL].final_volume == 500
    assert output.symbol_results[SELL].reason_code == TARGET_NOT_REACHED
    assert e._owner.account_assets_calls == 2


def test_snapshot_does_not_leak_into_a_second_run(monkeypatch):
    e = engine([assets(sell=900), assets(), assets(), assets(sell=900)])

    def dispatch(tasks):
        if not tasks:
            return []
        if tasks[0].algorithm_input.target_volume == 500:
            return [result()]
        return [result(start=500, observed=900, target=900, direction=OrderDirection.BUY)]

    monkeypatch.setattr(e, "_run_prepared_symbol_algorithms", dispatch)
    assert e.run(inputs(e, buy=False)).symbol_results[SELL].final_volume == 500
    output = e.run(inputs(e, buy=False, sell_target=900))
    assert output.status == ExecutionStatus.SUCCEEDED
    assert output.symbol_results[SELL].final_volume == 900
    assert e._owner.account_assets_calls == 4
