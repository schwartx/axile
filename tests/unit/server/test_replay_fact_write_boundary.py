"""分析事实与结构化业务写入共用事务，迟到目标只更新对应执行。"""

from __future__ import annotations

import asyncio
from contextlib import closing

import pytest

from axile.server._native import _core
from axile.server.execution import records
from axile.server.target_weight_snapshots import append_target_weight_snapshot, get_latest_account_target_snapshot
from tests.sqlite_oracle import connect as oracle_connect
from tests.unit.server._business_facts_test_support import persist_result, rows, seed_account, sql
from tests.unit.server._native_read_views import read_execution_input, read_execution_result


@pytest.fixture
def database(native_database, monkeypatch):
    seed_account(native_database)
    sql(
        native_database,
        "INSERT INTO portfolio(id,name,market,custom_calc_py_code,status,updated_at,created_at) "
        "VALUES(1,'事实测试','期货','def calculate_portfolio(context): return {}','normal','now','now')",
    )
    monkeypatch.setattr("axile.server.core.db.database_path", lambda: str(native_database))
    return native_database


def _fact_counts(path):
    with closing(oracle_connect(path)) as connection:
        names = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {
            name: connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            for name in names
            if name.startswith(
                ("business_", "execution_input", "execution_order", "execution_symbol", "execution_extension")
            )
            or name in {"executerecord", "execution_termination_cancel_failure", "account_asset_snapshot"}
        }


def test_record_fact_freezes_final_sizing_and_commits_with_record(database) -> None:
    async def scenario() -> None:
        record = await records._persist_execute_record(
            account_id=1,
            execution_id="sized",
            raw_input={"account_config": {"secret": "hidden"}, "curr_target": {"A": 0.9}},
            raw_result={
                "account_assets": {"total_asset": 1000.0, "positions": []},
                "symbol_results": {
                    "A": {
                        "first_tick": {"last_price": 100.0},
                        "sizing": {
                            "status": "SIZED",
                            "sizing_mode": "weight",
                            "target_quantity": 2.0,
                            "unit_notional": 300.0,
                            "reference_price": 100.0,
                            "equity": 1000.0,
                        },
                    }
                },
            },
            is_success=1,
        )
        result = await read_execution_result(record)
        sizing = result["symbol_results"]["A"]["sizing"]
        assert sizing["target_quantity"] == 2.0
        assert sizing["unit_notional"] == 300.0
        assert result["account_assets"]["total_asset"] == 1000.0
        assert record.facts_complete
        assert len(rows(database, "account_asset_snapshot")) == 1
        assert record.input_id is not None
        assert await read_execution_input(record.input_id) == {"curr_target": {"A": 0.9}}

    asyncio.run(scenario())


@pytest.mark.parametrize("writer", ["store_execution_facts", "ensure_execution_asset_snapshot"])
def test_fact_failure_rolls_back_record_assets_and_facts(database, writer: str) -> None:
    table = "business_asset_snapshot" if writer == "store_execution_facts" else "account_asset_snapshot"
    sql(
        database,
        f"CREATE TRIGGER reject_projection AFTER INSERT ON {table} BEGIN SELECT RAISE(ABORT,'projection failed'); END",
    )

    async def scenario() -> None:
        with pytest.raises(_core.NativeError, match="projection failed"):
            await records._persist_execute_record(
                account_id=1,
                execution_id="rollback",
                raw_input={"curr_target": {"A": 1.0}},
                raw_result={"account_assets": {"total_asset": 1000.0, "positions": []}},
                is_success=1,
            )
        assert all(count == 0 for count in _fact_counts(database).values())

    asyncio.run(scenario())


def test_late_target_refreshes_only_matching_execution_fact(database) -> None:
    async def scenario() -> None:
        for execution_id in ("late", "unrelated"):
            await records._persist_execute_record(
                account_id=1,
                execution_id=execution_id,
                raw_input={},
                raw_result={"first_ticks": {"A": {"last_price": 100.0}}},
                is_success=1,
            )
        await append_target_weight_snapshot(
            portfolio_id=1,
            account_id=1,
            raw_weights={"A": 0.6},
            normalized_weights={"A": 0.6},
            source="execution",
            execution_id="late",
        )
        assert (await get_latest_account_target_snapshot(1, 1))["normalized_weights"] == {"A": 0.6}

    asyncio.run(scenario())


def test_duplicate_record_retains_first_source_facts_and_projects_once(database) -> None:
    async def scenario() -> None:
        first = await records._persist_execute_record(
            account_id=1,
            execution_id="duplicate-source",
            raw_input={},
            raw_result={"status": "NOOP"},
            is_success=1,
        )
        committed = {name: rows(database, name) for name in _fact_counts(database)}
        version = rows(database, "account_analysis")
        # 重复终态若重新投影，真实触发器会中止整个调用。
        sql(
            database,
            "CREATE TRIGGER reject_second_projection BEFORE UPDATE ON executerecord "
            "BEGIN SELECT RAISE(ABORT,'duplicate projection'); END",
        )
        second = await records._persist_execute_record(
            account_id=1,
            execution_id="duplicate-source",
            raw_input={},
            raw_result={"status": "FAILED", "error": "late failure"},
            is_success=0,
        )
        assert first.id == second.id
        assert {name: rows(database, name) for name in committed} == committed
        assert rows(database, "account_analysis") == version
        assert (await read_execution_result(second))["status"] == "NOOP"
        assert second.facts_complete

    asyncio.run(scenario())


def test_unsupported_source_shape_preserves_business_record_and_marks_fact(database) -> None:
    async def scenario() -> None:
        record = await records._persist_execute_record(
            account_id=1,
            execution_id="unsupported-source",
            raw_input={},
            raw_result={"status": 123, "account_assets": {"total_asset": 101}, "symbol_results": {}},
            is_success=0,
        )
        persisted = rows(database, "executerecord")
        assert len(persisted) == 1 and persisted[0]["id"] == record.id
        result = await read_execution_result(record)
        assert "status" not in result
        assert "123" in str(result["audit_evidence"])
        assert persisted[0]["facts_complete"] and record.facts_complete

    asyncio.run(scenario())


def test_terminal_source_version_does_not_scale_with_fill_count(database) -> None:
    initial = rows(database, "account_analysis")
    before = initial[0]["source_version"] if initial else 0
    versions = [before]
    for count in (0, 20):
        persist_result(
            database,
            {
                "status": "SUCCEEDED",
                "trades": [{"trade_id": str(index), "trade_volume": 1, "trade_price": 3} for index in range(count)],
            },
            raw_input={},
            execution_id=f"fills-{count}",
            is_success=1,
        )
        versions.append(rows(database, "account_analysis")[0]["source_version"])
    assert versions[1] > versions[0]
    assert versions[2] - versions[1] == versions[1] - versions[0]
