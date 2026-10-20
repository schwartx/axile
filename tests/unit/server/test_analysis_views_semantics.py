"""Hand-calculated fixtures preserve the meaning of version-bound analysis views."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime

import pytest
import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from axile.server.analysis_view_writer import prepare_read_views
from axile.server.analysis_views import read_chart, read_execution_detail, read_execution_summary, read_status
from axile.server.db.models.analysis import (
    analysis_snapshot,
    analysis_state,
    cost_execution,
    cost_execution_symbol,
    cost_position,
    cost_range,
    cost_trade,
    cost_transaction,
    cost_transaction_fee,
)
from axile.server.db.models.analysis_result import (
    result_daily_cost,
    result_daily_fee,
    result_event,
    result_point,
    result_range,
)
from axile.server.performance_analysis import ENGINE_VERSION, LOGIC_VERSION
from axile.server.performance_details import CostQuery, read_costs
from tests.unit.server._execution_test_support import build_account

SNAPSHOT = "retained-hand-calculated"
LATEST = "latest-different-result"
ACCOUNT = 2
START_ISO = "2026-10-19T23:59:50+08:00"
END_ISO = "2026-10-20T00:00:20+08:00"
START = datetime.fromisoformat(START_ISO).timestamp() * 1000
END = START + 30_000


def _rows(table):
    """Decode only the contract's table envelope, independently of client helpers."""
    return [dict(zip(table["columns"], row, strict=True)) for row in table["rows"]]


def _execution(record_id, time_ms, positions_present, position_count, symbol_count):
    return {
        "snapshot_id": SNAPSHOT,
        "record_id": record_id,
        "time": time_ms,
        "success": True,
        "noop": record_id == 20,
        "key": str(record_id),
        "execution_id": f"exec-{record_id}",
        "created_at": datetime.fromtimestamp(time_ms / 1000, datetime.fromisoformat(START_ISO).tzinfo).isoformat(),
        "is_success": 1,
        "duration_sec": 0.0,
        "trade_count": {10: 4, 20: 0, 30: 3, 40: 1}[record_id],
        "symbol_count": symbol_count,
        "position_count": position_count,
        "positions_present": positions_present,
        "reason": None,
        "payload": {},
    }


def _trade(trade_id, record_id, time_ms, value, cost, bp, fee=None, currency=None, quantity=1):
    return {
        "snapshot_id": SNAPSHOT,
        "id": trade_id,
        "record_id": record_id,
        "symbol": "UNKNOWN" if quantity is None else "A",
        "time": time_ms,
        "day": "2026-10-19" if time_ms < START + 10_000 else "2026-10-20",
        "value": value,
        "cost": cost,
        "loss_bp": bp,
        "fee": fee,
        "fee_currency": currency,
        "estimated": trade_id == 4,
        "quantity": quantity,
        "side": "buy",
        "payload": {},
    }


def _transaction(record_id, idx, **values):
    return {
        "snapshot_id": SNAPSHOT,
        "record_id": record_id,
        "idx": idx,
        "symbol": "A",
        "side": "buy",
        "quantity": 1,
        "time": START + 15_000,
        "end_time": START + 16_000,
        "time_estimated": False,
        "value": 600,
        "cost": 4,
        "loss_bp": 25,
        "coverage": 2 / 3,
        "covered": 2,
        "count": 3,
        "amount_complete": True,
        "fee_covered": 0,
        **values,
    }


async def _seed(session):
    session.add(build_account(id=ACCOUNT, account_control_preset="default"))
    await session.commit()
    for identity, version in ((SNAPSHOT, 7), (LATEST, 8)):
        await session.execute(
            analysis_snapshot.insert(),
            {
                "id": identity,
                "account_id": ACCOUNT,
                "source_version": version,
                "logic_version": LOGIC_VERSION,
                "engine_version": ENGINE_VERSION,
                "computed_at": "2026-10-20T00:01:00+08:00",
                "data_until": END_ISO,
                "settings": {},
                "ranges": {},
                "cost_schema_version": 1,
                "result_schema_version": 2,
                "backtest_weight_type": "cs",
                "backtest_fee_rate": 0.0,
            },
        )
    await session.execute(
        analysis_state.insert(),
        {"account_id": ACCOUNT, "source_version": 8, "published_version": 8, "current_snapshot": LATEST},
    )
    await session.execute(
        cost_execution.insert(),
        [
            _execution(40, END, True, 0, 1),
            _execution(30, START + 20_000, True, 1, 1),
            _execution(20, START + 20_000, True, 0, 0),
            _execution(10, START, False, None, None),
        ],
    )
    await session.execute(
        cost_trade.insert(),
        [
            _trade(1, 10, START, 100, 1, 10, 0, "CNY"),
            _trade(2, 10, START + 15_000, 300, 3, 30, -0.25, "USDT"),
            _trade(3, 10, START + 15_001, 200, None, 999, 0.25, "USDT"),
            _trade(4, 10, START + 15_002, None, 9, 20, 5, "", quantity=None),
            _trade(5, 30, START + 20_000, 100, 1, 10, -0.5, "EUR"),
            _trade(6, 30, START + 20_001, 300, 3, 30),
            _trade(7, 30, START + 20_002, 200, None, 999),
            _trade(8, 40, END, 0, 0, 0, 0, "CNY", quantity=0),
        ],
    )
    await session.execute(
        cost_transaction.insert(),
        [
            _transaction(10, 0, fee_covered=3),
            _transaction(
                10,
                1,
                symbol="UNKNOWN",
                quantity=None,
                value=None,
                cost=None,
                loss_bp=None,
                coverage=None,
                covered=0,
                count=1,
                amount_complete=False,
            ),
            _transaction(30, 0, time=START + 20_000, end_time=START + 20_002, fee_covered=1),
            _transaction(
                40,
                0,
                quantity=0,
                time=END,
                end_time=END,
                value=0,
                cost=0,
                loss_bp=None,
                coverage=None,
                covered=1,
                count=1,
                fee_covered=1,
            ),
        ],
    )
    await session.execute(
        cost_transaction_fee.insert(),
        [
            {"snapshot_id": SNAPSHOT, "record_id": 10, "transaction_idx": 0, "currency": "CNY", "value": 0},
            {"snapshot_id": SNAPSHOT, "record_id": 10, "transaction_idx": 0, "currency": "USDT", "value": 0},
            {"snapshot_id": SNAPSHOT, "record_id": 30, "transaction_idx": 0, "currency": "EUR", "value": -0.5},
            {"snapshot_id": SNAPSHOT, "record_id": 40, "transaction_idx": 0, "currency": "CNY", "value": 0},
        ],
    )
    await session.execute(
        cost_position.insert(),
        {"snapshot_id": SNAPSHOT, "record_id": 30, "idx": 0, "symbol": "A", "quantity": -2, "direction": "SHORT"},
    )
    await session.execute(
        cost_execution_symbol.insert(),
        {
            "snapshot_id": SNAPSHOT,
            "record_id": 10,
            "symbol": "A",
            "present": 17,
            "status": "PARTIAL",
            "reason_code": None,
        },
    )
    await _seed_results(session)
    assert await prepare_read_views(session, SNAPSHOT)
    assert await prepare_read_views(session, LATEST)
    # Any legacy JSON reconstruction fails immediately instead of hiding in a passing test.
    await session.execute(sa.text("UPDATE performance_snapshot SET ranges='invalid-json',settings='invalid-json'"))
    await session.execute(sa.text("UPDATE performance_cost_execution SET payload='invalid-json'"))
    await session.execute(sa.text("UPDATE performance_cost_trade SET payload='invalid-json'"))
    await session.commit()


async def _seed_results(session):
    for range_key in ("all", "30", "90"):
        base = {"snapshot_id": SNAPSHOT, "range_key": range_key}
        await session.execute(cost_range.insert(), {**base, "baseline": START, "end": END})
        await session.execute(
            result_range.insert(),
            {
                **base,
                "backtest_included": True,
                "engine_version": "test-engine",
                "baseline": START_ISO,
                "end": END_ISO,
                "record_count": 4,
                "observation_count": 2,
                "used_record_count": 2,
                "target_used_record_count": 0,
                "invalid_asset_count": 0,
                "calendar_status": "unavailable",
                "event_count": 1,
            },
        )
        for index, (when, equity, account_return, backtest_return, record_id) in enumerate(
            ((START_ISO, 100, 0, None, 10), (END_ISO, 110, 0.1, 0.0, 40))
        ):
            await session.execute(
                result_point.insert(),
                {
                    **base,
                    "idx": index,
                    "date": when[:10],
                    "observed_at": when,
                    "record_id": record_id,
                    "execution_id": f"exec-{record_id}",
                    "account_equity": equity,
                    "account_return": account_return,
                    "portfolio_return": backtest_return,
                    "sizing_fallback": False,
                },
            )
        await session.execute(
            result_daily_cost.insert(),
            [
                {
                    **base,
                    "day": "2026-10-19",
                    "value": 100,
                    "cost": 1,
                    "loss_bp": 10,
                    "coverage": 1,
                    "covered": 1,
                    "count": 1,
                    "amount_complete": True,
                    "fee_covered": 1,
                    "covered_value": 100,
                    "estimated": 0,
                    "present": 1023,
                },
                {
                    **base,
                    "day": "2026-10-20",
                    "value": 1100,
                    "cost": 7,
                    "loss_bp": 19000 / 700,
                    "coverage": None,
                    "covered": 4,
                    "count": 7,
                    "amount_complete": False,
                    "fee_covered": 4,
                    "covered_value": 700,
                    "estimated": 1,
                    "present": 1023,
                },
            ],
        )
        await session.execute(
            result_daily_fee.insert(),
            [
                {**base, "day": day, "currency": currency, "value": value}
                for day, currency, value in (
                    ("2026-10-19", "CNY", 0),
                    ("2026-10-20", "CNY", 0),
                    ("2026-10-20", "USDT", 0),
                    ("2026-10-20", "EUR", -0.5),
                )
            ],
        )
        await session.execute(
            result_event.insert(),
            {**base, "idx": 0, "time": START_ISO, "tag": "execution", "text": "实际执行", "execution_id": "exec-10"},
        )
    await session.execute(
        cost_range.insert(), {"snapshot_id": LATEST, "range_key": "all", "baseline": None, "end": None}
    )
    await session.execute(
        result_range.insert(),
        {
            "snapshot_id": LATEST,
            "range_key": "all",
            "backtest_included": True,
            "engine_version": ENGINE_VERSION,
            "record_count": 0,
            "observation_count": 0,
            "used_record_count": 0,
            "target_used_record_count": 0,
            "invalid_asset_count": 0,
            "calendar_status": "unavailable",
            "event_count": 0,
        },
    )


@asynccontextmanager
async def _database(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'view-semantics.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)
        async with sessions() as session:
            await _seed(session)
        yield sessions
    finally:
        await engine.dispose()


def test_frozen_execution_summary_uses_valid_costs_and_preserves_currency_counts(tmp_path):
    """100/300 valid amounts give 25 bp; neither an unknown price nor rebates vanish."""

    async def check():
        async with _database(tmp_path) as sessions, sessions() as session:
            response = await read_execution_summary(session, ACCOUNT, SNAPSHOT, "all")
            summaries = {row["record_id"]: row for row in _rows(response["table"])}
            partial = summaries[10]
            expected = {
                "value": 600,
                "cost": 4,
                "loss_bp": 25,
                "coverage": None,
                "covered": 2,
                "covered_value": 400,
                "count": 4,
                "amount_complete": False,
                "fee_covered": 3,
                "estimated": 1,
            }
            assert {key: partial[key] for key in expected} == expected
            complete = summaries[30]
            assert complete["value"] == 600
            assert complete["cost"] == 4
            assert complete["loss_bp"] == 25
            assert complete["coverage"] == pytest.approx(2 / 3)
            assert complete["amount_complete"] is True
            assert [fee for fee in _rows(response["fees"]) if fee["record_id"] == 10] == [
                {"record_id": 10, "currency": "CNY", "value": 0, "count": 1},
                {"record_id": 10, "currency": "USDT", "value": 0, "count": 2},
            ]
            assert next(fee for fee in _rows(response["fees"]) if fee["currency"] == "EUR") == {
                "record_id": 30,
                "currency": "EUR",
                "value": -0.5,
                "count": 1,
            }
            # The old paged cost endpoint intentionally counts the unvalued cost=9.
            old = await read_costs(session, ACCOUNT, CostQuery(snapshot_id=SNAPSHOT, dimension="summary", record_id=10))
            assert old["summary"]["cost"] == 13
            assert old["summary"]["fees"][""] == 5

    asyncio.run(check())


def test_no_fills_and_true_zero_remain_different_in_frozen_summaries(tmp_path):
    async def check():
        async with _database(tmp_path) as sessions, sessions() as session:
            page = await read_execution_summary(session, ACCOUNT, SNAPSHOT, "all")
            summaries = {row["record_id"]: row for row in _rows(page["table"])}
            empty = summaries[20]
            assert all(empty[key] is None for key in ("value", "cost", "loss_bp", "coverage"))
            assert all(empty[key] == 0 for key in ("covered", "count", "covered_value", "estimated", "fee_covered"))
            assert empty["amount_complete"] is True
            assert empty["has_transactions"] is False
            zero = summaries[40]
            assert zero["value"] == zero["cost"] == zero["covered_value"] == 0
            assert zero["loss_bp"] is None and zero["coverage"] is None
            assert zero["covered"] == zero["count"] == zero["fee_covered"] == 1
            assert zero["amount_complete"] is True
            assert zero["has_transactions"] is True
            assert summaries[10]["symbol_count"] is None
            assert empty["symbol_count"] == 0
            assert zero["symbol_count"] == 1  # quantity=0 is still known.

    asyncio.run(check())


def test_transaction_presence_comes_from_frozen_transaction_facts(tmp_path):
    """Neither a trade count nor a known empty position proves transaction presence."""

    async def check():
        async with _database(tmp_path) as sessions, sessions() as session:
            await session.execute(
                cost_transaction.delete().where(
                    cost_transaction.c.snapshot_id == SNAPSHOT, cost_transaction.c.record_id == 40
                )
            )
            await session.execute(
                cost_transaction.insert(),
                _transaction(
                    20,
                    0,
                    quantity=None,
                    value=None,
                    cost=None,
                    loss_bp=None,
                    coverage=None,
                    covered=0,
                    count=0,
                ),
            )
            assert await prepare_read_views(session, SNAPSHOT)
            await session.commit()
            page = await read_execution_summary(session, ACCOUNT, SNAPSHOT, "all")
            summaries = {row["record_id"]: row for row in _rows(page["table"])}
            assert summaries[20]["count"] == 0 and summaries[20]["has_transactions"] is True
            assert summaries[40]["count"] == 1 and summaries[40]["has_transactions"] is False

    asyncio.run(check())


def test_curve_daily_costs_and_selected_detail_stay_on_the_requested_retained_version(tmp_path):
    async def check():
        async with _database(tmp_path) as sessions, sessions() as session:
            assert (await read_status(session, ACCOUNT))["snapshot_id"] == LATEST
            chart = await read_chart(session, ACCOUNT, SNAPSHOT, "all")
            page = await read_execution_summary(session, ACCOUNT, SNAPSHOT, "all")
            detail = await read_execution_detail(session, ACCOUNT, SNAPSHOT, 10)
            assert chart["snapshot_id"] == page["snapshot_id"] == detail["snapshot_id"] == SNAPSHOT
            assert chart["snapshot_version"] == 7
            points = _rows(chart["tables"]["points"])
            assert [(row["account_equity"], row["account_return"], row["portfolio_return"]) for row in points] == [
                (100, 0, None),
                (110, 0.1, 0),
            ]
            daily = _rows(chart["tables"]["daily_costs"])
            assert [row["day"] for row in daily] == ["2026-10-19", "2026-10-20"]
            assert daily[0]["value"] == 100 and daily[0]["cost"] == 1
            assert daily[1]["value"] == 1100 and daily[1]["cost"] == 7
            assert daily[1]["coverage"] is None
            assert daily[1]["present"] == 1023
            index = _rows(chart["tables"]["execution_index"])
            assert index == _rows(page["table"])
            fields = ("value", "cost", "loss_bp", "coverage", "covered", "count", "amount_complete", "fee_covered")
            assert {key: detail["summary"][key] for key in fields} == {key: index[0][key] for key in fields}
            assert detail["summary"]["fees"] == {"CNY": 0, "USDT": 0}
            assert chart["header"]["event_count"] == 1
            assert _rows(chart["tables"]["events"])[0]["execution_id"] == "exec-10"

    asyncio.run(check())


def test_detail_preserves_unknown_flat_short_and_true_transaction_times(tmp_path):
    async def check():
        async with _database(tmp_path) as sessions, sessions() as session:
            unknown = await read_execution_detail(session, ACCOUNT, SNAPSHOT, 10)
            flat = await read_execution_detail(session, ACCOUNT, SNAPSHOT, 20)
            short = await read_execution_detail(session, ACCOUNT, SNAPSHOT, 30)
            assert unknown["header"]["positions_present"] is False
            assert unknown["header"]["position_count"] is None
            assert flat["header"]["positions_present"] is True
            assert flat["header"]["position_count"] == 0
            assert _rows(unknown["tables"]["positions"]) == _rows(flat["tables"]["positions"]) == []
            assert _rows(short["tables"]["positions"]) == [
                {"idx": 0, "symbol": "A", "quantity": -2, "direction": "SHORT"},
            ]
            assert unknown["header"]["time"] == START
            transaction = _rows(unknown["tables"]["transactions"])[0]
            assert transaction["time"] == START + 15_000
            assert transaction["end_time"] == START + 16_000
            assert transaction["time_estimated"] is False
            symbol = _rows(unknown["tables"]["symbols"])[0]
            assert symbol["present"] == 17 and symbol["reason_code"] is None
            assert _rows(unknown["tables"]["transaction_fees"]) == [
                {"transaction_idx": 0, "currency": "CNY", "value": 0},
                {"transaction_idx": 0, "currency": "USDT", "value": 0},
            ]

    asyncio.run(check())


def test_frozen_range_endpoints_and_equal_time_pages_have_deterministic_record_order(tmp_path):
    async def check():
        async with _database(tmp_path) as sessions, sessions() as session:
            first = await read_execution_summary(session, ACCOUNT, SNAPSHOT, "all", limit=2)
            second = await read_execution_summary(
                session, ACCOUNT, SNAPSHOT, "all", limit=2, offset=first["next_offset"]
            )
            assert [row["record_id"] for row in _rows(first["table"])] == [10, 20]
            assert [row["record_id"] for row in _rows(second["table"])] == [30, 40]
            assert second["next_offset"] is None
            viewport = await read_execution_summary(session, ACCOUNT, SNAPSHOT, "all", start=START + 20_000, end=END)
            assert [row["record_id"] for row in _rows(viewport["table"])] == [20, 30, 40]

    asyncio.run(check())


@pytest.mark.parametrize("reader", ["chart", "summary", "detail"])
def test_marker_zero_is_explicitly_rejected_instead_of_reconstructing_legacy_json(tmp_path, reader):
    async def check():
        async with _database(tmp_path) as sessions, sessions() as session:
            await session.execute(
                analysis_snapshot.update().where(analysis_snapshot.c.id == SNAPSHOT).values(read_view_schema_version=0)
            )
            await session.execute(
                analysis_state.update().values(current_snapshot=SNAPSHOT, source_version=7, published_version=7)
            )
            await session.commit()
            status = await read_status(session, ACCOUNT)
            assert status["snapshot_id"] == SNAPSHOT and status["read_view_schema_version"] == 0
            with pytest.raises(HTTPException) as captured:
                if reader == "chart":
                    await read_chart(session, ACCOUNT, SNAPSHOT, "all")
                elif reader == "summary":
                    await read_execution_summary(session, ACCOUNT, SNAPSHOT, "all")
                else:
                    await read_execution_detail(session, ACCOUNT, SNAPSHOT, 10)
            assert captured.value.status_code == 409
            assert captured.value.detail["code"] == "ANALYSIS_READ_VIEW_UNAVAILABLE"

    asyncio.run(check())


def test_typed_snapshot_with_zero_executions_is_readable(tmp_path):
    async def check():
        async with _database(tmp_path) as sessions, sessions() as session:
            status = await read_status(session, ACCOUNT)
            assert status["snapshot_id"] == LATEST and status["read_view_schema_version"] == 1
            chart = await read_chart(session, ACCOUNT, LATEST, "all")
            page = await read_execution_summary(session, ACCOUNT, LATEST, "all")
            assert _rows(chart["tables"]["points"]) == []
            assert _rows(chart["tables"]["execution_index"]) == _rows(page["table"]) == []
            assert page["next_offset"] is None

    asyncio.run(check())
