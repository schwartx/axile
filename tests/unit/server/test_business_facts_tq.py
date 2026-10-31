"""天勤业务事实的离线存储回归；不创建 SDK、渠道或账户连接。"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from scripts.business_facts_contract import business_evidence
from tests.unit.server._business_facts_test_support import (
    freeze_input,
    persist_result,
    read_before_assets,
    read_result,
    seed_account,
)
from tests.unit.server._native_read_views import read_view

_NIGHT_TIME = "2026-10-19T21:00:00.123456+08:00"
_NIGHT_NANO = 1792414800123456789


def _sqlite_round_trip(
    path,
    domain: str,
    payload: Mapping[str, object],
    *,
    input_owner: bool = False,
) -> dict[str, object]:
    """通过完整原生事务及显式业务读取检查领域事实。"""
    if domain == "inputs" or input_owner:
        receipt = freeze_input(
            path,
            payload if not input_owner else {},
            **({"before_assets": payload["account_assets"]} if input_owner else {}),
        )
        if input_owner:
            return {"before_account_assets": read_before_assets(path, receipt["input_id"])}
        return read_view(path, "read_execution_input", {"input_id": receipt["input_id"]})
    return read_result(path, persist_result(path, payload))


def tq_historical_payloads() -> tuple[dict[str, object], dict[str, object]]:
    """提供供迁移验收复用的合成天勤历史记录。"""
    raw_input: dict[str, object] = {
        "channel_type": "tq",
        "curr_target": {"rb2701": -2, "ag2612": 0.0},
        "last_target": {},
        "algorithm": {"method": "TARGET-POS-TASK", "params": {"max_wait_seconds": 60}},
        "trade_rules": {"rb2701": {"sizing_mode": "lots", "price": "PASSIVE", "offset_priority": "昨今"}},
        "forbidden_symbols": [],
        "risk_symbols": None,
    }
    raw_result: dict[str, object] = {
        "channel_type": "tq",
        "status": "PARTIAL",
        "task_status": "PARTIAL",
        "execution_time": 0.0,
        "account_assets": _tq_assets(),
        "symbol_results": {
            "rb2701": {
                "symbol": "rb2701",
                "algorithm": "TARGET-POS-TASK",
                "status": "SUCCEEDED",
                "target_volume": -2,
                "final_volume": -2.0,
                "orders": [_tq_order()],
                "trades": [_tq_trade()],
                "first_tick": _tq_tick(),
                "sizing": _tq_sizing(),
                "memory": {"target_pos_task": {"closed_session": False}},
            },
            "ag2612": {
                "symbol": "ag2612",
                "status": "BLOCKED",
                "reason_code": "COMMON.SESSION.CLOSED",
                "target_volume": 0.0,
                "orders": [],
                "trades": [],
            },
        },
        "target_volume": {"rb2701": -3, "ag2612": 0},
        "first_ticks": {},
        "orders": [{**_tq_order(), "status": "PENDING", "filled_volume": 0}],
        "trades": [],
        "memory": {"session_closed_symbols": ["ag2612"]},
    }
    return raw_input, raw_result


def _tq_assets() -> dict[str, object]:
    return {
        "available_cash": 0.0,
        "total_asset": 100000,
        "market_value": None,
        "currency": "CNY",
        "update_time": _NIGHT_TIME,
        "extra": {"account_id": "synthetic-kq"},
        "positions": [
            {
                "symbol": "rb2701",
                "volume": 2,
                "available_volume": 1.0,
                "market_value": 64000.0,
                "direction": "LONG",
                "avg_price": 3190.0,
                "extra": {
                    "tq_symbol": "SHFE.rb2701",
                    "long_td": 1,
                    "long_yd": 1,
                    "short_td": 0,
                    "short_yd": 1,
                    "net_position": 1,
                    "position_attributes": {
                        "today_volume": 1,
                        "yesterday_volume": 1,
                        "open_price": 3190.0,
                        "position_price": 3188.0,
                        "float_profit": 0.0,
                        "position_profit": -20.0,
                        "margin": 6400.0,
                        "frozen": 1,
                        "frozen_today": 1,
                        "frozen_yesterday": 0,
                        "exchange_id": "SHFE",
                        "native_symbol": "rb2701",
                    },
                    "market_value_provenance": {
                        "source": "tq_last_price",
                        "price": 3200.0,
                        "multiplier": 10,
                        "quote_time": _NIGHT_NANO,
                        "price_status": "reference",
                    },
                },
            },
            {
                "symbol": "rb2701",
                "volume": 1.0,
                "available_volume": 1,
                "market_value": None,
                "direction": "SHORT",
                "avg_price": None,
                "extra": {
                    "tq_symbol": "SHFE.rb2701",
                    "position_cost": 3100,
                    "position_attributes": {"today_volume": 0, "yesterday_volume": 1, "float_profit": -10.0},
                    "market_value_provenance": {
                        "source": "unavailable",
                        "reason": "missing_quote",
                        "price": None,
                        "multiplier": None,
                        "quote_time": None,
                    },
                },
            },
        ],
    }


def _tq_order() -> dict[str, object]:
    return {
        "order_id": "same-native-order",
        "symbol": "rb2701",
        "direction": "SELL",
        "order_type": "LIMIT",
        "status": "FILLED",
        "volume": 2,
        "price": 3200.0,
        "filled_volume": 2.0,
        "avg_price": 3200,
        "create_time": _NIGHT_TIME,
        "update_time": "2026-10-19T21:00:00.123457+08:00",
        "extra": {
            "insert_date_time": _NIGHT_NANO,
            "exchange_id": "SHFE",
            "exchange_order_id": " 000023 ",
            "offset": "CLOSETODAY",
            "tq_symbol": "SHFE.rb2701",
            "is_error": False,
            "last_msg": "",
            "offset_leg2": None,
            "time_condition": "GFD",
            "volume_condition": "ANY",
        },
    }


def _tq_trade() -> dict[str, object]:
    return {
        "trade_id": "same-native-trade",
        "order_id": "same-native-order",
        "symbol": "rb2701",
        "trade_time": _NIGHT_TIME,
        "trade_volume": 2,
        "trade_price": 3200.0,
        "trade_value": 64000.0,
        "value_conversion": {"price_currency": "CNY", "value_currency": "CNY", "rate": 1.0, "reference": {}},
        "extra": {
            "exchange_id": "SHFE",
            "exchange_trade_id": " 0023 ",
            "direction": "SELL",
            "offset": "CLOSETODAY",
            "tq_symbol": "SHFE.rb2701",
            "trade_date_time": _NIGHT_NANO,
        },
    }


def _tq_tick() -> dict[str, object]:
    return {
        "symbol": "rb2701",
        "last_price": 3200.0,
        "bid_price": 3199,
        "ask_price": 3200,
        "bid_volume": 0,
        "ask_volume": 2,
        "timestamp": 1792414800123,
        "update_time": _NIGHT_TIME,
        "book_valid": True,
        "extra": {"tq_symbol": "SHFE.rb2701", "volume_multiple": 10, "price_tick": 1},
    }


def _tq_sizing() -> dict[str, object]:
    return {
        "symbol": "rb2701",
        "sizing_mode": "weight",
        "status": "SIZED",
        "reason_code": "COMMON.SIZING.QUANTIZED",
        "account_weight": -0.65,
        "equity": 100000.0,
        "reference_price": 3200.0,
        "unit_multiplier": 10,
        "unit_notional": 32000.0,
        "target_notional": 65000.0,
        "raw_quantity": -2.03125,
        "target_quantity": -2.0,
        "current_quantity": 1,
        "quantity_step": 1.0,
        "min_quantity": 1,
        "min_notional": None,
        "value_conversion": None,
    }


def test_tq_assets_preserve_native_sides_unknowns_and_valuation_evidence(native_database) -> None:
    payload = {"account_assets": _tq_assets()}
    restored = _sqlite_round_trip(native_database, "assets", payload)

    assert business_evidence(restored) == business_evidence(payload)
    long, short = restored["account_assets"]["positions"]
    assert long["symbol"] == short["symbol"] == "rb2701"
    assert business_evidence([long["direction"], short["direction"]]) == business_evidence(["LONG", "SHORT"])
    assert short["market_value"] is None
    assert short["avg_price"] is None
    assert "realized_profit" not in long["extra"]["position_attributes"]
    assert "margin" not in short["extra"]["position_attributes"]
    assert type(long["extra"]["market_value_provenance"]["quote_time"]) is int


def test_tq_input_keeps_lots_weight_empty_and_null_roles_separate(native_database) -> None:
    payload, _ = tq_historical_payloads()
    payload.update({"strategy_target": {}, "account_target": None, "execution_timeout": 60})

    assert business_evidence(_sqlite_round_trip(native_database, "inputs", payload)) == business_evidence(payload)


def test_tq_before_assets_keep_their_own_observation_time_and_source(native_database) -> None:
    observation = {**_tq_assets(), "update_time": "2026-10-19T20:59:58+08:00", "source": "worker_pre_execution"}

    restored = _sqlite_round_trip(native_database, "assets", {"account_assets": observation}, input_owner=True)
    assert business_evidence(restored["before_account_assets"], context="account_assets") == business_evidence(
        observation, context="account_assets"
    )


def test_tq_symbols_keep_multiplier_night_time_and_independent_root_views(native_database) -> None:
    payload = {
        "symbol_results": {
            "rb2701": {
                "symbol": "rb2701",
                "target_volume": -2,
                "final_volume": -2.0,
                "sizing": _tq_sizing(),
                "first_tick": _tq_tick(),
                "memory": {},
            },
            "ag2612": {"symbol": "ag2612", "target_volume": 0.0, "first_tick": None, "sizing": None},
        },
        "target_volume": {"rb2701": -3, "ag2612": 0},
        "first_ticks": {},
    }

    restored = _sqlite_round_trip(native_database, "symbols", payload)
    assert business_evidence(restored) == business_evidence(payload)
    assert isinstance(restored["symbol_results"]["rb2701"]["target_volume"], (int, float)) and not isinstance(
        restored["symbol_results"]["rb2701"]["target_volume"], bool
    )
    assert type(restored["symbol_results"]["ag2612"]["target_volume"]) is float


def test_tq_order_occurrences_keep_native_ids_offsets_and_nanoseconds(native_database) -> None:
    original = _tq_order()
    blank_id = {**original, "order_id": "", "extra": {"offset": "CLOSE", "is_error": None}}
    payload = {
        "orders": [{**original, "status": "PENDING", "filled_volume": 0}, blank_id, blank_id],
        "symbol_results": {"rb2701": {"orders": [original]}},
    }

    assert business_evidence(_sqlite_round_trip(native_database, "orders", payload)) == business_evidence(payload)


@pytest.mark.parametrize("commission", [pytest.param(None, id="unknown"), 0, -0.5])
def test_tq_trade_preserves_explicit_fee_without_inventing_missing_commission(
    native_database, commission: object
) -> None:
    trade = _tq_trade()
    known_fee = {**trade, "extra": {**trade["extra"], "commission": commission}}
    payload = {
        "trades": [trade, known_fee],
        "symbol_results": {"rb2701": {"trades": [{**trade, "trade_value": 123.0}]}},
    }

    restored = _sqlite_round_trip(native_database, "trades", payload)
    assert business_evidence(restored) == business_evidence(payload)
    assert "commission" not in restored["trades"][0]["extra"]
    assert business_evidence(restored["symbol_results"]["rb2701"]["trades"][0]["trade_value"]) == business_evidence(
        123.0
    )


@pytest.mark.parametrize("state", [{}, None, []])
def test_tq_positions_preserve_empty_null_and_degraded_container(native_database, state: object) -> None:
    payload = {"account_assets": {"positions": state, "market_value": None}}

    assert business_evidence(_sqlite_round_trip(native_database, "assets", payload)) == business_evidence(payload)


def test_tq_closed_session_retains_task_and_business_status_without_defaults(native_database) -> None:
    payload = {
        "channel_type": "tq",
        "task_status": "SKIPPED",
        "status": "BLOCKED",
        "outcome": "blocked",
        "reason_code": "COMMON.SESSION.CLOSED",
        "execution_time": 0,
    }

    assert business_evidence(_sqlite_round_trip(native_database, "head", payload)) == business_evidence(payload)


def test_tq_canonical_detail_matches_historical_evidence_across_transactions(native_database) -> None:
    raw_input, raw_result = tq_historical_payloads()
    seed_account(native_database, 23)
    record = persist_result(
        native_database,
        raw_result,
        raw_input=raw_input,
        account_id=23,
        execution_id="tq-canonical-roundtrip",
        created_at=_NIGHT_TIME,
    )
    assert record["input_id"] is not None
    assert business_evidence(
        read_view(native_database, "read_execution_input", {"input_id": record["input_id"]})
    ) == business_evidence(raw_input)
    identity = {"account_id": 23, "record_id": record["id"]}
    assert business_evidence(read_view(native_database, "read_execution_result", identity)) == business_evidence(
        raw_result
    )
    detail = read_view(native_database, "read_execution_detail", identity)
    assert business_evidence(detail["input"]) == business_evidence(raw_input)
    assert business_evidence(detail["result"]) == business_evidence(raw_result)
    assert business_evidence(detail["account_id"]) == business_evidence(23)
