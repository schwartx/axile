"""用指定的实际 Rust 程序验证规范业务事实、发布和版本绑定读取。"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from axile.common.trade_channel import TradeChannel
from axile.domain.execution import ExecutionKind
from axile.server import analysis_views
from axile.server.analysis_history import AnalysisRecord
from axile.server.business_facts import inputs, trades
from axile.server.business_facts.repository import read_execution_detail
from axile.server.db.models import ExecuteRecord
from axile.server.db.models.analysis import analysis_snapshot, analysis_state
from axile.server.db.models.analysis_replay import replay_observation, replay_observation_symbol
from axile.server.db.models.analysis_source import source_execution
from axile.server.db.models.performance import PerformanceSettings
from axile.server.execution import records
from axile.server.native_analysis import NativeAnalysis
from axile.server.performance_analysis import ENGINE_VERSION, LOGIC_VERSION, AnalysisManager, compute_batch, enqueue
from axile.server.trading_calendar import CalendarDayDecision, CalendarDecisionStatus
from scripts.business_facts_contract import business_evidence
from tests.unit.server._execution_test_support import build_account
from tests.unit.server.test_initial_migration import _MIGRATIONS_DIR, _load_migration
from tests.unit.server.test_native_analysis_contract import _same

pytestmark = pytest.mark.contract
ACCOUNT = 7


@pytest.fixture(scope="module")
def rebuilt_native_binary() -> Path:
    """只接受明确指定且声明支持原子发布的程序，不寻找其他构建或自动编译。"""
    explicit = os.environ.get("AXILE_ANALYSIS_BINARY")
    assert explicit, "Set AXILE_ANALYSIS_BINARY to the rebuilt business-facts native binary"
    binary = Path(explicit).resolve()
    assert binary.is_file() and os.access(binary, os.X_OK)
    result = subprocess.run([str(binary), "capabilities"], capture_output=True, check=True, timeout=5)
    capabilities = json.loads(result.stdout)
    assert business_evidence(capabilities["protocol"]) == business_evidence(1)
    assert "publish_store" in capabilities["commands"]
    assert business_evidence(capabilities["write_commands"]) == business_evidence(["publish_store"])
    return binary


def _create_schema(connection) -> None:
    SQLModel.metadata.create_all(connection)
    for name, installer in (
        ("0012_performance_snapshots.py", "install_triggers"),
        ("0020_account_settings.py", "install_settings_trigger"),
        ("0034_analysis_rust_facts.py", "install_triggers"),
        ("0035_analysis_structured_results.py", "install_triggers"),
        ("0037_business_execution_facts.py", "install_revision_triggers"),
    ):
        getattr(_load_migration(_MIGRATIONS_DIR / name), installer)(connection)
    columns = {item["name"] for item in sa.inspect(connection).get_columns("executerecord")}
    assert not columns.intersection({"raw_input", "raw_result"})
    assert connection.scalar(
        sa.text("SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'business_facts_0037_%'")
    )


def _payload(index: int) -> tuple[dict, dict]:
    price = float(100 + index * 10)
    return (
        {
            "channel_type": "TQ",
            "curr_target": {"A": 0.25 * index},
            "last_target": {} if index == 0 else {"A": 0.25 * (index - 1)},
            "algorithm": {"method": "TEST"},
        },
        {
            "status": "SUCCEEDED",
            "outcome": "completed",
            "success": True,
            "channel_type": "TQ",
            "execution_time": 0,
            "account_assets": {
                "available_cash": 100.0,
                "total_asset": 1000.0 + 100 * index,
                "market_value": 900.0 + 100 * index,
                "source": "real",
                "positions": [{"symbol": "A", "volume": index, "available_volume": 0}],
            },
            "symbol_results": {
                "A": {
                    "status": "SUCCEEDED",
                    "first_tick": {"last_price": price, "bid_price": price, "ask_price": price},
                    "sizing": {"unit_multiplier": 10},
                    "orders": [
                        {"order_id": f"order-{index}", "direction": "BUY", "filled_volume": 2, "status": "FILLED"}
                    ],
                    "trades": [
                        {
                            "trade_id": f"trade-{index}",
                            "order_id": f"order-{index}",
                            "trade_price": price + 2,
                            "trade_volume": 2,
                            "trade_time": f"2026-01-0{index + 1}T09:00:00+08:00",
                            "extra": {"commission": 0 if index == 0 else -0.1, "commission_asset": "CNY"},
                        }
                    ],
                }
            },
        },
    )


@asynccontextmanager
async def _database(tmp_path, binary: Path, monkeypatch):
    monkeypatch.setenv("AXILE_ANALYSIS_MODE", "required")
    monkeypatch.setenv("AXILE_ANALYSIS_BINARY", str(binary))
    monkeypatch.setenv("AXILE_ANALYSIS_TIMEOUT", "5")

    def evaluate(channel, day):
        return CalendarDayDecision(channel=str(channel), day=day, status=CalendarDecisionStatus.NOT_REQUIRED)

    monkeypatch.setattr("axile.server.performance.evaluate_channel_calendar_day", evaluate)
    monkeypatch.setattr("axile.server.analysis_calendar.evaluate_channel_calendar_day", evaluate)
    path = tmp_path / "canonical-native.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(_create_schema)
        account = build_account(
            id=ACCOUNT,
            portfolio_id=None,
            account_config={},
            trade_channel=TradeChannel.TQ,
            backtest_weight_type="cs",
            backtest_fee_rate=0.001,
        )
        async with sessions() as session:
            session.add(account)
            await session.commit()
        identities = []
        for index in range(3):
            payload, result = _payload(index)
            record = await records.append_success_execute_record(
                account,
                payload,
                result,
                execution_id=f"native-business-{index}",
                execution_kind=ExecutionKind.REBALANCE,
                session_factory=cast("records.SessionFactory", sessions),
            )
            identities.append(record.id)
            # Freeze observation days after exercising the real runtime writer;
            # the actual migration triggers must invalidate its initial facts.
            async with sessions() as session:
                await session.execute(
                    sa.update(ExecuteRecord)
                    .where(ExecuteRecord.id == record.id)
                    .values(created_at=f"2026-01-0{index + 1}T09:00:00+08:00")
                )
                await session.commit()
        yield sessions, AnalysisManager(sessions), NativeAnalysis(binary=binary, database=path, timeout=5), identities
    finally:
        await engine.dispose()


def _table_rows(table: dict) -> list[dict]:
    return [dict(zip(table["columns"], row, strict=True)) for row in table["rows"]]


async def _canonical_oracle(sessions) -> dict:
    async with sessions() as session:
        heads = (await session.scalars(sa.select(ExecuteRecord).order_by(ExecuteRecord.id))).all()
        canonical = [AnalysisRecord.from_detail(await read_execution_detail(session, head)) for head in heads]
        assert all(not hasattr(head, "raw_input") and not hasattr(head, "raw_result") for head in heads)
    expected, _, _ = compute_batch(
        canonical, [], [], [], PerformanceSettings(backtest_weight_type="cs", backtest_fee_rate=0.001), "tq"
    )
    return expected


async def _publish(sessions, manager: AnalysisManager, monkeypatch) -> None:
    async def no_python_fallback(*_args, **_kwargs):
        raise AssertionError("This regression must publish through the actual Rust worker")

    monkeypatch.setattr(manager, "_load", no_python_fallback)
    async with sessions() as session:
        await enqueue(session, ACCOUNT)
    assert await manager.run_once()


async def _native_snapshot(native: NativeAnalysis, range_key: str = "all") -> dict:
    return json.loads(
        await native.query(
            "snapshot", account_id=ACCOUNT, range=range_key, logic_version=LOGIC_VERSION, engine_version=ENGINE_VERSION
        )
    )


async def _assert_views_match_facts(sessions, native: NativeAnalysis, expected: dict) -> dict:
    snapshot = await _native_snapshot(native)
    assert business_evidence(snapshot["status"]) == business_evidence("ready")
    _same(snapshot["result"], expected["all"]["performance"])
    _same(snapshot["daily_costs"], expected["all"]["daily_costs"])
    async with sessions() as session:
        status = await analysis_views.read_status(session, ACCOUNT)
        assert business_evidence(status["status"]) == business_evidence("ready")
        assert status["snapshot_version"] == status["source_version"] == snapshot["snapshot_version"]
        chart = await analysis_views.read_chart(session, ACCOUNT, snapshot["snapshot_id"], "all")
        _same(_table_rows(chart["tables"]["points"]), expected["all"]["performance"]["points"])
        assert business_evidence(len(chart["tables"]["execution_index"]["rows"])) == business_evidence(3)
        for execution in expected["all"]["performance"]["executions"]:
            detail = await analysis_views.read_execution_detail(
                session, ACCOUNT, snapshot["snapshot_id"], execution["record"]["id"]
            )
            assert business_evidence(detail["header"]["is_success"]) == business_evidence(
                execution["record"]["is_success"]
            )
            assert detail["summary"]["cost"] == pytest.approx(execution["summary"]["cost"])
            assert business_evidence(detail["summary"]["fees"]) == business_evidence(execution["summary"]["fees"])
            assert business_evidence(len(_table_rows(detail["tables"]["transactions"]))) == business_evidence(
                len(execution["transactions"])
            )
        assert business_evidence(
            await session.scalar(sa.select(sa.func.count()).select_from(source_execution))
        ) == business_evidence(3)
        assert business_evidence(
            await session.scalar(sa.select(sa.func.count()).select_from(replay_observation))
        ) == business_evidence(3)
        assert business_evidence(
            await session.scalar(
                sa.select(sa.func.count())
                .select_from(source_execution)
                .where(source_execution.c.projection_version != 1)
            )
        ) == business_evidence(0)
    return snapshot


def test_real_native_publication_reads_canonical_business_facts(rebuilt_native_binary, tmp_path, monkeypatch):
    """正式写入和 0037 失效后，原生发布、状态、曲线及执行明细保持同一事实含义。"""

    async def scenario():
        async with _database(tmp_path, rebuilt_native_binary, monkeypatch) as (sessions, manager, native, _):
            expected = await _canonical_oracle(sessions)
            await _publish(sessions, manager, monkeypatch)
            await _assert_views_match_facts(sessions, native, expected)
            for range_key in ("30", "90"):
                snapshot = await _native_snapshot(native, range_key)
                expected[range_key]["performance"]["executions"] = expected["all"]["performance"]["executions"]
                _same(snapshot["result"], expected[range_key]["performance"])
                _same(snapshot["daily_costs"], expected[range_key]["daily_costs"])

    asyncio.run(scenario())


@pytest.mark.parametrize("mutation", ["trade", "target"])
def test_same_count_business_mutation_republishes_and_retains_old_snapshot(
    rebuilt_native_binary, tmp_path, monkeypatch, mutation
):
    """同数量明细改值使分析投影失效，新版重算；旧版曲线和明细继续冻结。"""

    async def scenario():
        async with _database(tmp_path, rebuilt_native_binary, monkeypatch) as (sessions, manager, native, identities):
            await _publish(sessions, manager, monkeypatch)
            before = await _native_snapshot(native)
            old_id = before["snapshot_id"]
            changed_record = identities[1]
            async with sessions() as session:
                old_chart = await analysis_views.read_chart(session, ACCOUNT, old_id, "all")
                old_detail = await analysis_views.read_execution_detail(session, ACCOUNT, old_id, changed_record)
                if mutation == "trade":
                    table, owner = trades.trade, trades.trade.c.record_id == changed_record
                    values = {"trade_price": 119.0}
                else:
                    head = await session.get(ExecuteRecord, changed_record)
                    table = inputs.execution_input_target
                    owner = sa.and_(table.c.input_id == head.input_id, table.c.role == "curr_target")
                    values = {"value": 0.75}
                count = await session.scalar(sa.select(sa.func.count()).select_from(table).where(owner))
                assert business_evidence(count) == business_evidence(1)
                await session.execute(table.update().where(owner).values(**values))
                await session.commit()
                assert business_evidence(
                    await session.scalar(sa.select(sa.func.count()).select_from(table).where(owner))
                ) == business_evidence(count)
                status = await analysis_views.read_status(session, ACCOUNT)
                assert business_evidence(status["status"]) == business_evidence("stale")
                assert status["source_version"] > before["snapshot_version"]
                assert business_evidence(
                    await session.scalar(
                        sa.select(sa.func.count())
                        .select_from(source_execution)
                        .where(source_execution.c.record_id == changed_record)
                    )
                ) == business_evidence(0)
                assert business_evidence(
                    await session.scalar(
                        sa.select(sa.func.count())
                        .select_from(replay_observation)
                        .where(replay_observation.c.record_id == changed_record)
                    )
                ) == business_evidence(0)
                assert business_evidence(
                    await analysis_views.read_chart(session, ACCOUNT, old_id, "all")
                ) == business_evidence(old_chart)
                assert business_evidence(
                    await analysis_views.read_execution_detail(session, ACCOUNT, old_id, changed_record)
                ) == business_evidence(old_detail)
            expected = await _canonical_oracle(sessions)
            await _publish(sessions, manager, monkeypatch)
            after = await _assert_views_match_facts(sessions, native, expected)
            assert after["snapshot_id"] != old_id
            assert after["snapshot_version"] > before["snapshot_version"]
            async with sessions() as session:
                assert business_evidence(
                    await analysis_views.read_chart(session, ACCOUNT, old_id, "all")
                ) == business_evidence(old_chart)
                assert business_evidence(
                    await analysis_views.read_execution_detail(session, ACCOUNT, old_id, changed_record)
                ) == business_evidence(old_detail)
                state = (await session.execute(sa.select(analysis_state))).mappings().one()
                assert business_evidence(state["previous_snapshot"]) == business_evidence(old_id)
                assert business_evidence(
                    await session.scalar(sa.select(analysis_snapshot.c.id).where(analysis_snapshot.c.id == old_id))
                ) == business_evidence(old_id)
                if mutation == "trade":
                    assert after["daily_costs"] != before["daily_costs"]
                else:
                    target = await session.scalar(
                        sa.select(replay_observation_symbol.c.target_weight).where(
                            replay_observation_symbol.c.record_id == changed_record,
                            replay_observation_symbol.c.symbol == "A",
                        )
                    )
                    assert business_evidence(target) == business_evidence(0.75)
                    assert (
                        after["result"]["points"][-1]["target_portfolio_return"]
                        != before["result"]["points"][-1]["target_portfolio_return"]
                    )

    asyncio.run(scenario())
