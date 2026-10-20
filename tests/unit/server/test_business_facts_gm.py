"""GM 历史业务事实结构化存储的独立、离线保真验证。"""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from types import ModuleType
from typing import Any

import pytest
from sqlalchemy import JSON, Table, create_engine, select

from axile.server.business_facts import assets, inputs, orders, trades
from scripts.business_facts_contract import business_evidence

_DOMAINS = {"assets": assets, "inputs": inputs, "orders": orders, "trades": trades}


def _domain(name: str) -> ModuleType:
    """仅导入存储模块，避免依赖 GM SDK 或执行器初始化。"""
    return _DOMAINS[name]


def _persist_and_decode(
    module: ModuleType,
    batch: Mapping[Table, list[dict[str, object]]],
    decode: Callable[[dict[str, list[dict[str, Any]]]], dict[str, object]],
) -> tuple[dict[str, object], dict[str, list[dict[str, Any]]]]:
    """经真实 SQLite 列类型和 JSON 编码后还原事实，检查 SQL 往返损失。"""
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            metadata = {table.metadata for table in module.TABLES}
            for item in metadata:
                item.create_all(connection)
            for table, rows in batch.items():
                for row in rows:
                    connection.execute(table.insert().values(**row))
            loaded = {
                table.name: [dict(row) for row in connection.execute(select(table)).mappings()]
                for table in module.TABLES
            }
        return decode(loaded), loaded
    finally:
        engine.dispose()


def _roundtrip(name: str, payload: dict[str, object]) -> tuple[dict[str, object], dict[str, list[dict[str, Any]]]]:
    module = _domain(name)
    return _persist_and_decode(
        module, getattr(module, f"encode_{name}")(240, payload), getattr(module, f"decode_{name}")
    )


def _gm_assets() -> dict[str, object]:
    """资产与原生现金、持仓证据来自同一时点，但字段语义分别保留。"""
    return {
        "available_cash": 6000.0,
        "total_asset": 20100.0,
        "market_value": 14000.0,
        "currency": "CNY",
        "source": "gm",
        "update_time": "2026-10-20T09:30:01.000001+08:00",
        "positions": [
            {
                "symbol": "600000.SH",
                "volume": 1000,
                "available_volume": 700,
                "market_value": 14000.0,
                "direction": "多头",
                "extra": {
                    "channel_type": "gm",
                    "gm_symbol": "SHSE.600000",
                    "position_attributes": {
                        "volume": 1000,
                        "today_volume": 300,
                        "available": 1000,
                        "available_now": 700,
                        "available_today": 0,
                        "frozen": 300,
                        "frozen_today": 0,
                        "position_price": 13.5,
                        "last_price": 14.0,
                        "float_profit": 500.0,
                        "created_at": "2026-10-19T14:59:59.000000+08:00",
                        "updated_at": "2026-10-20T09:30:01.000001+08:00",
                        "change_event_id": "event-240",
                    },
                    "raw_position_data": {
                        "symbol": "SHSE.600000",
                        "volume": 1000,
                        "available_now": 700,
                        "order_frozen": 300,
                        "vwap": 13.5,
                        "fpnl": 500.0,
                        "native_optional": None,
                    },
                },
            },
            {
                "symbol": "600000.SH",
                "volume": 0,
                "available_volume": 0,
                "market_value": 0.0,
                "direction": "多头",
                "avg_price": None,
                "extra": {"position_attributes": {}},
            },
            {
                "symbol": "000001.SZ",
                "volume": 0.0,
                "available_volume": 0.0,
                "market_value": None,
                "direction": "多头",
                "avg_price": 0,
                "extra": {},
            },
        ],
        "extra": {
            "channel_type": "gm",
            "total_asset_source": "gm.cash.nav",
            "gm_cash": {
                "nav": 20100.0,
                "available": 6000.0,
                "order_frozen": 100.0,
                "market_value": 14000.0,
                "updated_at": "2026-10-20T09:30:01.000001+08:00",
            },
        },
    }


def _gm_order() -> dict[str, object]:
    return {
        "order_id": "gm-client-240",
        "symbol": "600000.SH",
        "direction": "BUY",
        "order_type": "LIMIT",
        "volume": 300,
        "price": 10.01,
        "status": "PARTIALLY_FILLED",
        "filled_volume": 100,
        "avg_price": 10.0,
        "create_time": "2026-10-20T09:30:00.000001+08:00",
        "update_time": "2026-10-20T09:30:00.999999+08:00",
        "extra": {
            "channel_type": "gm",
            "gm_symbol": "SHSE.600000",
            "cl_ord_id": "gm-client-240",
            "exchange_order_id": "exchange-240",
            "position_effect": 1,
            "position_side": 1,
            "ord_rej_reason": 0,
            "ord_rej_reason_detail": "",
            "raw_order_data": {"cl_ord_id": "gm-client-240", "filled_vwap": 10.0, "native_optional": None},
        },
    }


def _gm_trade() -> dict[str, object]:
    return {
        "trade_id": "gm-fill-240",
        "order_id": "gm-client-240",
        "symbol": "600000.SH",
        "trade_volume": 100,
        "trade_price": 10.0,
        "trade_value": 1002.4,
        "trade_time": "2026-10-20T09:30:00.999999+08:00",
        "extra": {
            "channel_type": "gm",
            "gm_symbol": "SHSE.600000",
            "cl_ord_id": "gm-client-240",
            "exchange_order_id": "exchange-240",
            "commission": 0,
            "tax": None,
            "raw_trade_data": {"exec_id": "gm-fill-240", "created_at": "2026-10-20 09:30:00.999999+08:00"},
        },
    }


def test_gm_asset_roundtrip_retains_native_fields_without_inventing_average_price() -> None:
    payload = {"account_assets": _gm_assets()}
    decoded, loaded = _roundtrip("assets", payload)

    assert business_evidence(decoded) == business_evidence(payload)
    positions = decoded["account_assets"]["positions"]
    assert business_evidence([item["symbol"] for item in positions]) == business_evidence(
        ["600000.SH", "600000.SH", "000001.SZ"]
    )
    assert "avg_price" not in positions[0]
    assert positions[1]["avg_price"] is None
    assert business_evidence(positions[2]["avg_price"]) == business_evidence(0)
    assert isinstance(positions[0]["volume"], (int, float)) and not isinstance(positions[0]["volume"], bool)
    assert type(positions[2]["volume"]) is float
    assert business_evidence(positions[0]["available_volume"]) == business_evidence(700)
    assert business_evidence(positions[0]["extra"]["position_attributes"]["available"]) == business_evidence(1000)
    assert business_evidence(positions[0]["extra"]["position_attributes"]["frozen"]) == business_evidence(300)
    assert any(row.get("available_volume") == 700 for rows in loaded.values() for row in rows)


@pytest.mark.parametrize("source", ["real", "gm", "assumed", "unavailable", "error"])
def test_gm_zero_and_degraded_snapshot_sources_remain_distinct(source: str) -> None:
    payload = {
        "account_assets": {
            "available_cash": 0,
            "total_asset": 0,
            "market_value": 0,
            "positions": [],
            "source": source,
        },
    }
    decoded, _ = _roundtrip("assets", payload)
    assert business_evidence(decoded) == business_evidence(payload)


def test_gm_pre_order_and_terminal_assets_are_independent_observations() -> None:
    module = _domain("assets")
    before = {"account_assets": _gm_assets()}
    after = copy.deepcopy(before)
    after["account_assets"]["update_time"] = "2026-10-20T09:30:02.000001+08:00"
    after["account_assets"]["positions"][0]["available_volume"] = 600

    before_decoded, before_rows = _persist_and_decode(
        module, module.encode_input_assets("gm-input-240", before), module.decode_assets
    )
    after_decoded, after_rows = _roundtrip("assets", after)

    assert business_evidence(before_decoded) == business_evidence({"before_account_assets": before["account_assets"]})
    assert business_evidence(after_decoded) == business_evidence(after)
    assert any(row.get("input_id") == "gm-input-240" for rows in before_rows.values() for row in rows)
    assert any(row.get("record_id") == 240 for rows in after_rows.values() for row in rows)


def test_gm_order_roundtrip_preserves_occurrences_and_native_identity() -> None:
    first = _gm_order()
    empty_id = {**copy.deepcopy(first), "order_id": "", "status": "REJECTED", "filled_volume": 0}
    nested = {**copy.deepcopy(first), "filled_volume": 300, "status": "FILLED"}
    payload = {"orders": [first, empty_id, first], "symbol_results": {"600000.SH": {"orders": [nested]}}}

    decoded, loaded = _roundtrip("orders", payload)

    assert business_evidence(decoded) == business_evidence(payload)
    assert business_evidence([order["order_id"] for order in decoded["orders"]]) == business_evidence(
        ["gm-client-240", "", "gm-client-240"]
    )
    assert business_evidence(decoded["symbol_results"]["600000.SH"]["orders"][0]["filled_volume"]) == business_evidence(
        300
    )
    assert isinstance(decoded["orders"][0]["volume"], (int, float)) and not isinstance(
        decoded["orders"][0]["volume"], bool
    )
    assert business_evidence(sum("order_id" in row for rows in loaded.values() for row in rows)) == business_evidence(4)


def test_gm_fill_roundtrip_does_not_recompute_value_or_infer_missing_fee() -> None:
    first = _gm_trade()
    nested = copy.deepcopy(first)
    nested["extra"].pop("commission")
    nested["extra"]["tax"] = 0
    payload = {"trades": [first, first], "symbol_results": {"600000.SH": {"trades": [nested]}}}

    decoded, loaded = _roundtrip("trades", payload)

    assert business_evidence(decoded) == business_evidence(payload)
    assert business_evidence(decoded["trades"][0]["trade_value"]) == business_evidence(1002.4)
    assert "commission" not in decoded["symbol_results"]["600000.SH"]["trades"][0]["extra"]
    assert isinstance(decoded["trades"][0]["trade_volume"], (int, float)) and not isinstance(
        decoded["trades"][0]["trade_volume"], bool
    )
    assert business_evidence(sum("trade_id" in row for rows in loaded.values() for row in rows)) == business_evidence(3)


def test_gm_input_roundtrip_keeps_strategy_account_and_quantity_targets_separate() -> None:
    module = _domain("inputs")
    payload = {
        "channel_type": "gm",
        "execution_timeout": 300,
        "curr_target": {"600000.SH": 0.1, "000001.SZ": 0.0},
        "last_target": {"000001.SZ": 0.2, "600000.SH": 0},
        "strategy_target": {"600000.SH": 0.25, "000001.SZ": 0.0},
        "account_target": {"600000.SH": 0.1, "000001.SZ": 0.0},
        "target_volume": {"600000.SH": 300, "000001.SZ": 0},
        "forbidden_symbols": ["000001.SZ"],
        "risk_symbols": [],
        "algorithm": {"method": "SINGLE-MAKER", "params": {"max_wait_seconds": 10}},
        "trade_rules": {"600000.SH": {"volume_multiple": 100}},
        "extra": {"strategy_name": "gm synthetic fixture", "native_optional": None},
    }
    batch = module.encode_inputs(
        "gm-input-241", payload, account_id=24, execution_id="gm-exec-241", created_at="2026-10-20T09:29:59+08:00"
    )
    decoded, loaded = _persist_and_decode(module, batch, module.decode_inputs)

    assert business_evidence(decoded) == business_evidence(payload)
    assert business_evidence(list(decoded["last_target"])) == business_evidence(["000001.SZ", "600000.SH"])
    assert decoded["strategy_target"]["600000.SH"] != decoded["curr_target"]["600000.SH"]
    assert isinstance(decoded["plan"]["quantities"]["600000.SH"], (int, float)) and not isinstance(
        decoded["plan"]["quantities"]["600000.SH"], bool
    )
    typed_values = [
        row[column.name]
        for table in module.TABLES
        for column in table.c
        if not isinstance(column.type, JSON)
        for row in loaded[table.name]
        if column.name in row
    ]
    assert 300 in typed_values
