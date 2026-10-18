"""Issue #53:期货渠道意图翻译层回归.

算法层只表达渠道无关的意图词汇(position_side 标记平仓),开平标志由
CTP/TQ 的 place_order 翻译层推导:显式 offset_flag 优先,position_side
推导平仓,全无则开仓缺省。读不出持仓时算法层拒绝猜测(见 test_order_helper)。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from openctp_ctp import thosttraderapi as td

from axile.executor.models.unified_order import OrderDirection, OrderType
from tests.unit.executor.test_futures_order_intent import _assets


def _started_ctp_executor(monkeypatch):
    from axile.executor.ctp import ctp_execute
    from axile.executor.models.unified_input import CTPAccountConfig
    from tests.unit.executor.ctp.test_requests import _submit_point_executor

    config = CTPAccountConfig(
        app_id="app-id",
        auth_code="authcode",
        broker_id="9999",
        investor_id="100001",
        password="secret",
        td_front="tcp://td:10001",
        md_front="tcp://md:10002",
    )
    monkeypatch.setattr(
        ctp_execute,
        "build_order_insert",
        lambda _config, **kwargs: SimpleNamespace(
            InstrumentID=kwargs["symbol"], LimitPrice=kwargs["price"], OrderPriceType="2", **kwargs
        ),
    )
    executor = _submit_point_executor(config)
    monkeypatch.setattr(executor, "_get_ctp_session_block_reason", lambda _symbol: None)
    monkeypatch.setattr(executor, "_validate_order_quote", lambda *args: None)
    monkeypatch.setattr(executor, "get_account_assets", lambda: _assets("ag2612", "LONG", 0, 1))
    return SimpleNamespace(trader=executor._trader_api), executor


def test_ctp_place_order_defaults_open_without_offset_semantics(monkeypatch):
    """CTP 渠道无任何开平语义时按开仓缺省处理(平仓意图由算法层以 position_side 显式标记)."""
    broker, executor = _started_ctp_executor(monkeypatch)
    try:
        order = executor._place_order_impl("ag2612", OrderDirection.SELL, OrderType.LIMIT, 1, 9000)
        assert order.extra["offset_flag"] == td.THOST_FTDC_OF_Open
        broker.trader.ReqOrderInsert.assert_called_once()
    finally:
        executor.close()


def test_ctp_place_order_derives_close_from_position_side(monkeypatch):
    """CTP 渠道缺失 offset_flag 但携带 position_side 时推导为平仓."""
    broker, executor = _started_ctp_executor(monkeypatch)
    try:
        order = executor._place_order_impl(
            "ag2612", OrderDirection.SELL, OrderType.LIMIT, 1, 9000, position_side="LONG"
        )
        assert order.extra["offset_flag"] == td.THOST_FTDC_OF_CloseYesterday
    finally:
        executor.close()


def _started_tq_executor(monkeypatch):
    from axile.executor.models.unified_input import TQAccountConfig
    from axile.executor.tq.tq_execute import TQExecutor, TQTradingTimeCheck, TQTradingTimeStatus
    from tests.unit.executor.tq.test_executor import FakeApi

    api = FakeApi()
    monkeypatch.setattr(TQExecutor, "_build_api", staticmethod(lambda _config: api))
    executor = TQExecutor(TQAccountConfig(account_mode="kq", tq_username="u", tq_password="p"))
    monkeypatch.setattr(
        executor,
        "_check_tq_symbol_trading_time",
        lambda _api, _sessions, _now: TQTradingTimeCheck(TQTradingTimeStatus.OPEN),
    )
    return executor, api


def test_tq_place_order_defaults_open_without_offset_semantics(monkeypatch):
    """TQ 渠道无任何开平语义时按 OPEN 缺省处理(平仓意图由算法层以 position_side 显式标记)."""
    executor, api = _started_tq_executor(monkeypatch)
    try:
        order = executor._place_order_impl("rb2610", OrderDirection.BUY, OrderType.LIMIT, 1, 3200)
        assert api.insert_args["offset"] == "OPEN"
        assert order.extra["offset_flag"] == "0"
    finally:
        executor.close()


def test_tq_place_order_derives_close_from_position_side(monkeypatch):
    """TQ 渠道缺失 offset_flag 但携带 position_side 时推导为 CLOSE."""
    executor, api = _started_tq_executor(monkeypatch)
    try:
        order = executor._place_order_impl(
            "rb2610", OrderDirection.SELL, OrderType.LIMIT, 1, 3200, position_side="LONG"
        )
        assert api.insert_args["offset"] == "CLOSE"
        assert order.symbol == "rb2610"
    finally:
        executor.close()


def test_tq_offset_map_accepts_ctp_semantic_flags():
    """算法层按 CTP 语义传递的 open/close/close_today 标志须被 TQ 正确映射."""
    from axile.executor.tq.tq_execute import _OFFSET_MAP

    assert _OFFSET_MAP["open"] == "OPEN"
    assert _OFFSET_MAP["close"] == "CLOSE"
    assert _OFFSET_MAP["close_today"] == "CLOSETODAY"
    assert _OFFSET_MAP["close_yesterday"] == "CLOSE"


@pytest.mark.parametrize("channel", ["ctp", "tq"])
@pytest.mark.parametrize(
    "direction,side,expected",
    [
        (OrderDirection.BUY, "LONG", "0"),
        (OrderDirection.SELL, "SHORT", "0"),
        (OrderDirection.SELL, "LONG", "3"),
        (OrderDirection.BUY, "SHORT", "3"),
    ],
)
def test_direction_and_side_translate_using_today_position(monkeypatch, channel, direction, side, expected):
    if channel == "ctp":
        _, executor = _started_ctp_executor(monkeypatch)
        symbol = "ag2612"
    else:
        executor, _ = _started_tq_executor(monkeypatch)
        symbol = "rb2610"
    monkeypatch.setattr(executor, "get_account_assets", lambda: _assets(symbol, side, 2, 0))
    try:
        order = executor._place_order_impl(symbol, direction, OrderType.LIMIT, 1, 3200, position_side=side)
        actual = order.extra["offset_flag"]
        assert actual in ({"0", "open"} if expected == "0" else {"3", "close_today"})
    finally:
        executor.close()


@pytest.mark.parametrize("channel", ["ctp", "tq"])
def test_mixed_position_plan_and_single_order_guard(monkeypatch, channel):
    if channel == "ctp":
        _, executor = _started_ctp_executor(monkeypatch)
        symbol = "ag2612"
    else:
        executor, _ = _started_tq_executor(monkeypatch)
        symbol = "rb2610"
    assets = _assets(symbol, "LONG", 2, 1)
    monkeypatch.setattr(executor, "get_account_assets", lambda: assets)
    try:
        assert executor.plan_close_orders(symbol, OrderDirection.SELL, 3, assets) == [
            (1, {"offset_flag": "close_yesterday"}),
            (2, {"offset_flag": "close_today"}),
        ]
        with pytest.raises(ValueError, match="拆单"):
            executor._place_order_impl(symbol, OrderDirection.SELL, OrderType.LIMIT, 3, 3200, position_side="LONG")
        # 显式标志始终优先，原生算法无需重新规划。
        order = executor._place_order_impl(
            symbol, OrderDirection.SELL, OrderType.LIMIT, 1, 3200, position_side="LONG", offset_flag="open"
        )
        assert order.extra["offset_flag"] in ("0", "open")
    finally:
        executor.close()
