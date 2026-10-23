"""CTP 历史事实迁移的离线语义回归，不加载 SDK 或连接柜台。"""

from __future__ import annotations

from copy import deepcopy

import pytest

from scripts.business_facts_contract import business_evidence
from tests.conftest import native_store_call
from tests.unit.server._business_facts_test_support import persist_result, seed_account

_CREATED_AT = "2026-10-16T21:05:01+08:00"
_TRADE_TIME = "2026-10-16T21:05:00+08:00"
_TRADING_DAY = "20261019"
_ORDER_ID = "20261019:7:23:000001"


def _roundtrip(
    path, result: dict[str, object], *, execution_input: dict[str, object] | None = None
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    """每个原生阶段关闭连接后重读，检查真实持久化后的事实。"""
    payload = execution_input if execution_input is not None else {"channel_type": "CTP"}
    seed_account(path, 22)
    record = persist_result(
        path,
        result,
        raw_input=payload,
        account_id=22,
        execution_id="ctp-facts-roundtrip",
        created_at=_CREATED_AT,
    )
    assert record["input_id"] is not None
    identity = {"account_id": 22, "record_id": record["id"]}
    return (
        native_store_call(path, "read_execution_input", {"input_id": record["input_id"]}),
        native_store_call(path, "read_execution_result", identity),
        native_store_call(path, "read_execution_detail", identity),
    )


def _order(*, order_id: str = _ORDER_ID, offset: str = "3") -> dict[str, object]:
    return {
        "order_id": order_id,
        "symbol": "rb2611",
        "direction": "SELL",
        "order_type": "LIMIT",
        "volume": 2.0,
        "price": 3500.0,
        "status": "PARTIALLY_FILLED",
        "filled_volume": 1.0,
        "avg_price": 3499.0,
        "create_time": _TRADE_TIME,
        "update_time": "",
        "extra": {
            "trading_day": _TRADING_DAY,
            "front_id": 7,
            "session_id": 23,
            "order_ref": "000001",
            "exchange_id": "SHFE",
            "order_sys_id": "000001927",
            "offset_flag": offset,
            "hedge_flag": "1",
            "event_date": "20261016",
            "event_clock": "21:05:00",
            "event_time_source": "InsertDate",
            "event_time_status": "native",
        },
    }


def _trade(*, trade_id: str = "00000001", order_id: str = _ORDER_ID) -> dict[str, object]:
    return {
        "trade_id": trade_id,
        "symbol": "rb2611",
        "order_id": order_id,
        "trade_time": _TRADE_TIME,
        "trade_volume": 1.0,
        "trade_price": 3499.0,
        # 迁移保留历史供应金额，不用价格和数量重算并丢失合约乘数。
        "trade_value": 34990.0,
        "value_conversion": {
            "price_currency": "CNY",
            "value_currency": "CNY",
            "rate": 1.0,
            "reference": {"source": "frozen_plan", "contract_multiplier": 10},
        },
        "extra": {
            "trading_day": _TRADING_DAY,
            "exchange_id": "SHFE",
            "order_sys_id": "000001927",
            "order_ref": "000001",
            "direction": "1",
            "offset_flag": "3",
            "hedge_flag": "1",
            "event_date": "20261016",
            "event_clock": "21:05:00",
            "event_time_source": "TradeDate",
            "event_time_status": "native",
        },
    }


def _position() -> dict[str, object]:
    return {
        "symbol": "rb2611",
        "direction": "LONG",
        "volume": 3.0,
        "available_volume": 2.0,
        "market_value": None,
        "avg_price": None,
        "extra": {
            "position_attributes": {"position_profit": -12.0, "commission": 0.0},
            "native_positions": [
                {"volume": 2.0, "today_volume": 1.0, "position_profit": 0.0, "margin": 200.0, "hedge_flag": "1"},
                {"volume": 1.0, "today_volume": 0.0, "position_profit": -12.0, "hedge_flag": "3"},
            ],
            "long_td": 1.0,
            "long_yd": 2.0,
            "frozen": 1.0,
            "volume_multiple": None,
            "last_price": None,
            "quote_timestamp": None,
            "quote_update_time": None,
            "valuation_status": "unavailable",
            "valuation_reason": "missing_multiplier",
            "valuation_source": None,
            "valuation_reference": False,
        },
    }


def test_ctp_execution_preserves_natural_time_identity_offset_and_frozen_values(native_database) -> None:
    result = {
        "channel_type": "CTP",
        "status": "PARTIAL",
        "outcome": "NOT_REACHED",
        "execution_time": 1.25,
        "symbol_results": {
            "rb2611": {
                "symbol": "rb2611",
                "algorithm": "TARGET-POS-TASK",
                "status": "PARTIAL",
                "target_volume": -2,
                "final_volume": 1,
                "orders": [_order()],
                "trades": [_trade()],
                "sizing": {
                    "symbol": "rb2611",
                    "sizing_mode": "weight",
                    "status": "SIZED",
                    "reason_code": "COMMON.SIZING.QUANTIZED",
                    "account_weight": -0.25,
                    "equity": 280000.0,
                    "reference_price": 3500.0,
                    "unit_multiplier": 10.0,
                    "unit_notional": 35000.0,
                    "target_notional": 70000.0,
                    "raw_quantity": -2.0,
                    "target_quantity": -2.0,
                    "current_quantity": 3.0,
                    "quantity_step": 1.0,
                    "min_quantity": 1.0,
                },
            }
        },
    }
    _, restored, detail = _roundtrip(native_database, result)
    assert business_evidence(restored) == business_evidence(result)
    assert business_evidence(detail["result"]) == business_evidence(result)
    symbol = restored["symbol_results"]["rb2611"]
    assert business_evidence(symbol["trades"][0]["trade_value"]) == business_evidence(34990.0)
    assert business_evidence(symbol["orders"][0]["extra"]["offset_flag"]) == business_evidence("3")
    assert symbol["trades"][0]["trade_time"].startswith("2026-10-16")
    assert business_evidence(symbol["trades"][0]["extra"]["trading_day"]) == business_evidence("20261019")


@pytest.mark.parametrize("commission", [None, 0.0, -0.25])
def test_ctp_unknown_zero_and_rebate_commission_do_not_become_each_other(
    native_database, commission: float | None
) -> None:
    trade = _trade()
    if commission is not None:
        trade["extra"].update(commission=commission, commission_currency="CNY")
    result = {"channel_type": "CTP", "trades": [trade]}
    _, restored, _ = _roundtrip(native_database, result)
    assert business_evidence(restored) == business_evidence(result)
    extra = restored["trades"][0]["extra"]
    if commission is None:
        assert "commission" not in extra
        assert "fee" not in restored["trades"][0]
    else:
        assert business_evidence(extra["commission"]) == business_evidence(commission)


def test_ctp_native_position_missing_amounts_are_not_inferred_from_totals(native_database) -> None:
    position = _position()
    result = {
        "channel_type": "CTP",
        "account_assets": {
            "available_cash": 0.0,
            "total_asset": 280000.0,
            "market_value": None,
            "currency": "CNY",
            "source": "real",
            "update_time": _CREATED_AT,
            "positions": [position],
            "extra": {"trading_day": _TRADING_DAY},
        },
    }
    _, restored, _ = _roundtrip(native_database, result)
    assert business_evidence(restored) == business_evidence(result)
    restored_position = restored["account_assets"]["positions"][0]
    attributes = restored_position["extra"]["position_attributes"]
    assert "margin" not in attributes
    assert "position_price" not in attributes
    assert restored_position["market_value"] is None
    assert restored_position["avg_price"] is None
    assert business_evidence(
        [row["hedge_flag"] for row in restored_position["extra"]["native_positions"]]
    ) == business_evidence(["1", "3"])


def test_ctp_targets_keep_lots_zero_short_and_close_policy_input_semantics(native_database) -> None:
    payload = {
        "channel_type": "CTP",
        "execution_timeout": 180,
        "curr_target": {"rb2611": -2, "hc2611": 0},
        "last_target": {"rb2611": 3.0},
        "algorithm": {"method": "TARGET-POS-TASK", "params": {"chase_enabled": False}},
        "trade_rules": {"rb2611": {"sizing_mode": "lots", "offset_priority": "昨今", "contract_multiplier": 10}},
        "futures_close_policy": {"mode": "fee", "rules": {"SHFE.rb": "今昨"}},
        "forbidden_symbols": [],
        "risk_symbols": ["hc2611"],
    }
    restored_input, restored_result, detail = _roundtrip(native_database, {"status": "NOOP"}, execution_input=payload)
    assert business_evidence(restored_input) == business_evidence(payload)
    assert business_evidence(detail["input"]) == business_evidence(payload)
    assert business_evidence(restored_result) == business_evidence({"status": "NOOP"})
    assert business_evidence(restored_input["curr_target"]["hc2611"]) == business_evidence(0)
    assert isinstance(restored_input["curr_target"]["hc2611"], (int, float)) and not isinstance(
        restored_input["curr_target"]["hc2611"], bool
    )
    assert type(restored_input["last_target"]["rb2611"]) is float
    assert business_evidence(restored_input["trade_rules"]["rb2611"]["sizing_mode"]) == business_evidence("lots")


def test_ctp_unknown_quote_time_and_book_validity_remain_explicit(native_database) -> None:
    first_tick = {
        "symbol": "rb2611",
        "last_price": 3500.0,
        "bid_price": 0.0,
        "ask_price": 0.0,
        "bid_volume": 0,
        "ask_volume": 0,
        "timestamp": 0,
        "update_time": "",
        "book_valid": False,
        "extra": {
            "trading_day": _TRADING_DAY,
            "event_date": "",
            "event_clock": "21:05:00",
            "event_time_source": "ActionDay",
            "event_time_status": "unknown",
        },
    }
    result = {"symbol_results": {"rb2611": {"first_tick": first_tick}}}
    _, restored, _ = _roundtrip(native_database, result)
    assert business_evidence(restored) == business_evidence(result)
    assert business_evidence(restored["symbol_results"]["rb2611"]["first_tick"]["timestamp"]) == business_evidence(0)
    assert restored["symbol_results"]["rb2611"]["first_tick"]["book_valid"] is False


def test_ctp_duplicate_and_unresolved_native_ids_preserve_collection_occurrences(native_database) -> None:
    root_order = _order(order_id="")
    nested_order = _order(order_id="", offset="4")
    root_trade = _trade(trade_id="", order_id="")
    nested_trade = deepcopy(root_trade)
    nested_trade["extra"]["exchange_id"] = "DCE"
    nested_trade["trade_time"] = ""
    nested_trade["extra"]["event_time_status"] = "unknown"
    result = {
        "orders": [root_order, deepcopy(root_order)],
        "trades": [root_trade, deepcopy(root_trade)],
        "symbol_results": {
            "rb/26~11": {
                "symbol": "rb2611",
                "orders": [nested_order],
                "trades": [nested_trade],
            }
        },
    }
    _, restored, _ = _roundtrip(native_database, result)
    assert business_evidence(restored) == business_evidence(result)
    assert business_evidence(len(restored["orders"])) == business_evidence(2)
    assert business_evidence(len(restored["trades"])) == business_evidence(2)
    assert business_evidence(restored["symbol_results"]["rb/26~11"]["trades"][0]["order_id"]) == business_evidence("")


def test_ctp_partial_history_preserves_absent_null_and_zero_fields(native_database) -> None:
    result = {
        "status": "TERMINATED",
        "orders": [{"order_id": "", "price": 0, "update_time": None}],
        "trades": [{"trade_id": "", "trade_time": "", "trade_value": None, "extra": {}}],
        "account_assets": {"source": "unavailable", "available_cash": None, "positions": []},
    }
    _, restored, _ = _roundtrip(native_database, result)
    assert business_evidence(restored) == business_evidence(result)
    assert "volume" not in restored["orders"][0]
    assert "trade_volume" not in restored["trades"][0]
    assert business_evidence(restored["orders"][0]["price"]) == business_evidence(0)
    assert isinstance(restored["orders"][0]["price"], (int, float)) and not isinstance(
        restored["orders"][0]["price"], bool
    )
    assert restored["orders"][0].get("update_time") is None
