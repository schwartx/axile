"""品种事实在迁移和实时写入中保持原始证据。"""

import asyncio
from collections.abc import Mapping

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlmodel import SQLModel

from axile.server.business_facts.symbols import (
    TABLES,
    decode_symbols,
    encode_symbols,
    read_outcome_symbols,
    read_sizing_rows,
    read_symbol_rows,
    symbol_result,
    symbol_sizing,
    symbol_tick,
)
from scripts.business_facts_contract import business_evidence


def _loaded(batch):
    return {table.name: rows for table, rows in batch.items()}


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"symbol_results": None, "first_ticks": [], "target_volume": "invalid"},
        {"symbol_results": {}, "first_ticks": {}, "target_volume": {}},
        {"symbol_results": {"": None, "alias/~BTC": [], "A": {}, "B": {"first_tick": None, "sizing": None}}},
        {
            "symbol_results": {
                "map/~B": {
                    "symbol": "native.B",
                    "status": "PARTIAL",
                    "outcome": "not_reached",
                    "target_volume": 0,
                    "final_volume": None,
                    "error": "",
                    "success": False,
                    "memory": {"channel": {"flags": []}},
                    "sizing": {
                        "symbol": "sizing.B",
                        "sizing_mode": "lots",
                        "status": "UNAVAILABLE",
                        "reason_code": "CTP.MISSING_MULTIPLIER",
                        "account_weight": 0.0,
                        "equity": None,
                        "unit_multiplier": None,
                        "target_quantity": -2,
                        "reference_price": "not-numeric",
                        "value_conversion": {
                            "price_currency": "HKD",
                            "value_currency": "CNY",
                            "rate": 0.92,
                            "reference": {"observed_at": "2026-10-19T21:00:00+08:00"},
                        },
                    },
                    "first_tick": {
                        "symbol": "tick.B",
                        "last_price": 0,
                        "bid_price": None,
                        "ask_price": 0.0,
                        "bid_price_5": 1.5,
                        "ask_volume_5": 0,
                        "timestamp": 0,
                        "update_time": "",
                        "timestamp_datetime": "1970-01-01T08:00:00",
                        "book_valid": False,
                        "extra": {"trading_day": "20261020", "event_date": "20261019", "volume_multiple": 10},
                    },
                    "unknown_symbol_metadata": {"enabled": False},
                },
                "A": {"target_volume": "2", "status": 0, "first_tick": {}, "sizing": {"value_conversion": []}},
            },
            "first_ticks": {"map/~B": {"last_price": 20, "timestamp": 42}, "A": None},
            "target_volume": {"map/~B": 17.0, "A": False, "": None, "bad": []},
        },
    ],
)
def test_symbol_roundtrip_preserves_sparse_and_malformed_evidence(payload):
    batch = encode_symbols(3, payload)
    actual = decode_symbols(_loaded(batch))
    assert business_evidence(actual) == business_evidence(payload)
    if isinstance(payload.get("symbol_results"), Mapping):
        assert business_evidence(list(actual["symbol_results"])) == business_evidence(list(payload["symbol_results"]))
    if "target_volume" in payload and isinstance(payload["target_volume"], Mapping):
        for key, value in payload["target_volume"].items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                assert actual["target_volume"][key] == value
            else:
                assert actual["target_volume"][key] is None


def test_symbol_encoder_excludes_delegated_arrays_and_keeps_native_identity():
    payload = {
        "symbol_results": {
            "alias/~B": {
                "symbol": "native.B",
                "orders": [{"order_id": "same"}],
                "trades": [{"trade_id": "same"}],
                "account_assets": {"positions": [{"symbol": "native.B", "volume": 2}]},
                "first_tick": {"last_price": 1},
                "sizing": {"target_quantity": 2},
            }
        }
    }
    batch = encode_symbols(9, payload)
    row = batch[symbol_result][0]
    assert business_evidence(row["path"]) == business_evidence("/symbol_results/alias~1~0B")
    assert business_evidence(row["map_key"]) == business_evidence("alias/~B")
    assert business_evidence(row["symbol"]) == business_evidence("native.B")
    assert "orders" not in str(row["audit_evidence"].get("unrecognized_fields"))
    assert "trades" not in str(row["audit_evidence"].get("unrecognized_fields"))
    assert "account_assets" not in str(row["audit_evidence"].get("unrecognized_fields"))
    assert business_evidence(batch[symbol_tick][0]["last_price"]) == business_evidence(1)
    assert business_evidence(batch[symbol_sizing][0]["target_quantity"]) == business_evidence(2)
    assert business_evidence(decode_symbols(_loaded(batch))) == business_evidence(
        {
            "symbol_results": {
                "alias/~B": {"symbol": "native.B", "first_tick": {"last_price": 1}, "sizing": {"target_quantity": 2}}
            }
        }
    )


def test_roundtrip_keeps_every_sizing_and_five_level_tick_field():
    from axile.server.business_facts.symbols import SIZING_FIELDS, TICK_FIELDS

    sizing = {key: key if kind == "text" else 1.25 for key, kind in SIZING_FIELDS.items()}
    tick = {
        key: {"opaque": []}
        if kind == "json"
        else False
        if kind == "boolean"
        else 0
        if kind == "integer"
        else key
        if kind == "text"
        else 0.25
        for key, kind in TICK_FIELDS.items()
    }
    payload = {"symbol_results": {"A": {"sizing": sizing, "first_tick": tick}}}
    assert business_evidence(decode_symbols(_loaded(encode_symbols(1, payload)))) == business_evidence(payload)


def test_delegated_fragments_merge_without_field_order_metadata():
    from axile.server.business_facts.repository import merge_result_fragment

    source = {"symbol_results": {"A": {"status": "NOOP", "orders": [], "trades": []}}}
    batch = encode_symbols(1, source)
    fragment = decode_symbols(_loaded(batch))
    merge_result_fragment(fragment, {"symbol_results": {"A": {"orders": [], "trades": []}}})
    assert business_evidence(fragment) == business_evidence(source)
    assert business_evidence(batch[symbol_result][0]["audit_evidence"]) == business_evidence({})


@pytest.mark.parametrize("missing_table", [symbol_result, symbol_sizing, symbol_tick])
def test_missing_typed_facts_raise_instead_of_silently_omitting_evidence(missing_table):
    payload = {"symbol_results": {"A": {"sizing": {}, "first_tick": {}}}}
    batch = encode_symbols(1, payload)
    batch[missing_table] = []
    with pytest.raises(ValueError, match="Missing|Incomplete"):
        decode_symbols(_loaded(batch))


def test_sql_roundtrip_and_narrow_readers_preserve_order_and_do_not_load_arrays():
    asyncio.run(_sql_roundtrip_and_narrow_readers())


async def _sql_roundtrip_and_narrow_readers():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    payload = {
        "symbol_results": {
            "Z": {"status": "PARTIAL", "first_tick": {"timestamp": 0}, "sizing": {"target_quantity": 0}},
            "alias/A": {
                "outcome": "blocked",
                "target_volume": -2,
                "memory": {"large": "opaque", "unknown": float("inf")},
            },
            "malformed": None,
            "completed": {"status": "NOOP"},
            "explicit": {"status": "PARTIAL", "outcome": "completed"},
        },
        "target_volume": {"Z": 1, "alias/A": 1.0},
    }
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: SQLModel.metadata.create_all(sync, tables=list(TABLES)))
    try:
        async with AsyncSession(engine) as session:
            for table, rows in encode_symbols(17, payload).items():
                if rows:
                    await session.execute(sa.insert(table), rows)
            await session.commit()
            loaded = {table.name: (await session.execute(sa.select(table))).mappings().all() for table in TABLES}
            assert business_evidence(decode_symbols(loaded)) == business_evidence(payload)
            assert business_evidence(await read_outcome_symbols(session, 17)) == business_evidence(["Z", "alias/A"])
            assert business_evidence(await read_sizing_rows(session, 17)) == business_evidence(
                {"Z": {"target_quantity": 0}}
            )
            assert business_evidence(
                await read_symbol_rows(session, [17], fields=("target_volume",))
            ) == business_evidence({17: {"Z": {}, "alias/A": {"target_volume": -2}, "completed": {}, "explicit": {}}})
            assert business_evidence(await read_symbol_rows(session, [])) == business_evidence({})
            with pytest.raises(ValueError, match="Unsupported"):
                await read_symbol_rows(session, [17], fields=("orders",))
    finally:
        await engine.dispose()
