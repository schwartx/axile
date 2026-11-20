"""GM 非成交回报不能经回调或查询变成成交记录。"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytest.importorskip("gm.api")
from gm.api import ExecType_CancelRejected, ExecType_Trade
from gm.model import DictLikeExecRpt

from axile.executor.gm.common import convert_gm_trade_to_trade_record
from axile.executor.gm.core import gm_strategy
from axile.executor.gm.gm_execute import GMExecutor
from axile.executor.models.unified_input import GMAccountConfig
from tests.fixtures.algorithm_fakes import ClockStub


def report(**changes):
    data = dict(
        account_id="test",
        exec_type=ExecType_Trade,
        symbol="SZSE.002281",
        exec_id="fill",
        cl_ord_id="client-order",
        order_id="exchange-order",
        volume=100,
        price=92.2,
        created_at=datetime(2026, 11, 3, 14, 57),
    )
    data.update(changes)
    return data


def instance():
    return GMExecutor(
        GMAccountConfig(connection_mode="service", account_id="test", token="test", serv_addr="127.0.0.1:7001"),
        connect=False,
    )


@pytest.mark.parametrize(
    ("symbol", "step"),
    [("159981.SZ", 0.001), ("SZSE.158001", 0.001), ("513880.SH", 0.001), ("000001.SZ", 0.01)],
)
def test_gm_stock_and_etf_price_steps(symbol, step):
    assert instance().get_tick_size(symbol) == step


@pytest.mark.parametrize("position_seconds", [0.75, 2.5])
def test_gm_assets_share_timeout_between_position_and_cash(monkeypatch, position_seconds):
    clock = ClockStub()
    executor = instance()
    calls = []
    monkeypatch.setattr(
        executor,
        "get_active_execution_runtime",
        lambda: SimpleNamespace(deadline_remaining_seconds=lambda: 2.5 - clock.time()),
    )

    def bridge(operation, *, timeout, **_):
        calls.append((operation, timeout))
        if operation == "get_position":
            clock.sleep(position_seconds)
            return []
        return {"nav": 900, "available": 900}

    monkeypatch.setattr(executor, "_call_bridge", bridge)
    if position_seconds < 2.5:
        assert executor.get_account_assets().total_asset == 900
        assert calls == [("get_position", 2.5), ("get_cash", 1.75)]
    else:
        with pytest.raises(TimeoutError):
            executor.get_account_assets()
        assert calls == [("get_position", 2.5)]


@pytest.mark.parametrize("kind", [None, 0, 1, 5, 8, 16, 17, ExecType_CancelRejected, 999])
def test_non_trade_reports_are_not_trades(kind):
    assert convert_gm_trade_to_trade_record(report(exec_type=kind)) is None


@pytest.mark.parametrize("field", ["price", "volume"])
@pytest.mark.parametrize("value", [0, -1, None, True, float("nan"), float("inf"), "bad"])
def test_malformed_fills_are_not_trades(field, value):
    assert convert_gm_trade_to_trade_record(report(**{field: value})) is None


@pytest.mark.parametrize("exec_id", ["fill", ""])
def test_callback_and_query_have_same_identity_and_normalized_symbol(monkeypatch, exec_id):
    data = report(exec_id=exec_id)
    callback = gm_strategy._convert_execrpt_to_trade(DictLikeExecRpt(**data))
    executor = instance()
    monkeypatch.setattr(executor, "_call_bridge", Mock(return_value=[data]))
    queried = executor._query_trade_records()
    assert len(queried) == 1
    assert callback == queried[0]
    assert callback.symbol == "002281.SZ"
    assert callback.trade_volume == 100


def test_live_and_query_paths_filter_cancel_rejection_and_keep_original_order_state(monkeypatch):
    data = report(exec_type=ExecType_CancelRejected, price=0)
    dispatcher = Mock()
    context = SimpleNamespace(dispatcher=dispatcher, stats={"execution_report_received": 0})
    monkeypatch.setattr(gm_strategy, "_is_stopped", lambda: False)
    monkeypatch.setattr(gm_strategy, "_get_bridge_context", lambda: context)
    gm_strategy.on_execution_report(None, DictLikeExecRpt(**data))
    dispatcher.dispatch_trade_record.assert_not_called()
    dispatcher.dispatch_order_update.assert_not_called()
    executor = instance()
    monkeypatch.setattr(executor, "_call_bridge", Mock(return_value=[data, report(), report(account_id="other")]))
    assert len(executor._query_trade_records()) == 1
