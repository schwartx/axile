"""资产可信度、旧占位与数据库过滤的跨层回归。"""

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, select

from axile.server.analysis_history import read_analysis_record
from axile.server.api.routes.account_assets import list_account_asset_snapshots
from axile.server.asset_observations import (
    is_asset_observation,
    is_legacy_placeholder,
    legacy_placeholder_condition,
    valid_asset_snapshot_condition,
)
from axile.server.business_facts.assets import asset_fact_id, read_snapshots_assets, write_snapshot_assets
from axile.server.business_facts.repository import store_execution_facts
from axile.server.db.models import AccountAssetSnapshot, ExecuteRecord
from axile.server.execution import notification_assets
from axile.server.execution.records import _persist_execute_record
from axile.server.performance import observation
from tests.unit.server._execution_test_support import build_account


def legacy():
    return dict(
        account_assets=dict(available_cash=0, total_asset=0, market_value=0, positions=[], extra={}, source="real"),
        status="FAILED",
        extra={"worker_error": {"type": "ValueError", "message": "任意错误"}},
        inputs=None,
        execution_time=0,
        symbol_results={},
    )


def cases():
    base = legacy()
    yield base, True
    for key in ("inputs", "symbol_results"):
        yield {**base, key: "{}"}, False
    for key, value in [("positions", "[]"), ("extra", "{}")]:
        yield {**base, "account_assets": {**base["account_assets"], key: value}}, False
    missing = deepcopy(base)
    del missing["account_assets"]["source"]
    yield missing, True
    for key, value in [
        ("extra", {}),
        ("inputs", {"curr_target": {}}),
        ("execution_time", 1),
        ("status", "SUCCEEDED"),
        ("symbol_results", {"A": {}}),
    ]:
        yield {**base, key: value}, False
    for key, value in [
        ("total_asset", 100),
        ("extra", {"balance": 0}),
        ("positions", [{}]),
        ("source", "unavailable"),
        ("available_cash", "0"),
        ("market_value", False),
    ]:
        yield {**base, "account_assets": {**base["account_assets"], key: value}}, False
    blocked = {
        **base,
        "status": "BLOCKED",
        "outcome": "BLOCKED",
        "outcome_reason": "当前不在交易时间",
        "error": "当前不在交易时间",
        "memory": {"message": "当前不在交易时间"},
        "inputs": {"curr_target": {}},
        "execution_time": 0.25,
    }
    yield blocked, True
    yield {**blocked, "memory": {"message": "当前不在交易时间", "other": 1}}, False


@pytest.mark.parametrize("result,expected", list(cases()))
def test_python_sql_placeholder_parity(result, expected):
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        actual = conn.scalar(sa.select(legacy_placeholder_condition(sa.literal(result, type_=sa.JSON))))
    assert bool(actual) == is_legacy_placeholder(result) == expected
    engine.dispose()


@pytest.mark.parametrize(
    "value,valid",
    [
        (0, True),
        (100000, True),
        (None, False),
        ("0", False),
        (False, False),
        (float("nan"), False),
        (float("inf"), False),
    ],
)
def test_asset_validity(value, valid):
    assert is_asset_observation({"total_asset": value}) == valid
    for source in ("assumed", "error", "unavailable"):
        assert not is_asset_observation({"total_asset": value, "source": source})


def test_failed_placeholders_do_not_become_zero_observations():
    async def check():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(SQLModel.metadata.create_all)
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            async with sessions() as session:
                session.add(build_account(id=1))
                records = []
                for i in range(1, 5):
                    record = ExecuteRecord(
                        account_id=1,
                        execution_id=str(i),
                        created_at=f"2026-09-{i:02d}",
                        is_success=0,
                    )
                    session.add(record)
                    await session.flush()
                    result = {"account_assets": {"total_asset": 100000}} if i == 1 else legacy()
                    await store_execution_facts(session, record, {}, result)
                    records.append(record)
                await session.commit()
                observations = [observation(await read_analysis_record(session, record)).asset for record in records]
                assert observations == [100000, None, None, None]
                real_zero = legacy()
                real_zero["extra"] = {}
                await store_execution_facts(session, records[1], {}, real_zero)
                await session.commit()
                assert observation(await read_analysis_record(session, records[1])).asset == 0
        finally:
            await engine.dispose()

    asyncio.run(check())


def test_snapshot_filter_before_ranking_pagination_and_persistence(tmp_path, monkeypatch):
    from axile.server._native import _core
    from axile.server.api.routes import account_assets

    monkeypatch.setattr(account_assets, "analysis_store", lambda: _core.Store(str(tmp_path / "assets.db")))

    async def check():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'assets.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        async with sessions() as session:
            session.add(build_account(id=1))
            session.add(build_account(id=2))
            for account in (1, 2):
                snapshot = AccountAssetSnapshot(account_id=account, source="manual")
                await write_snapshot_assets(session, snapshot, {"total_asset": 100000, "positions": []})
                for i in range(25):
                    execution_id = f"{account}-{i}"
                    record = ExecuteRecord(
                        account_id=account,
                        execution_id=execution_id,
                        is_success=0,
                    )
                    session.add(record)
                    await session.flush()
                    await store_execution_facts(session, record, {}, legacy())
                    session.add(
                        AccountAssetSnapshot(
                            account_id=account,
                            execution_id=execution_id,
                            asset_fact_id=asset_fact_id(record.id),
                            source="execution",
                        )
                    )
            await session.commit()
            monkeypatch.setattr(notification_assets, "SessionLocal", sessions)
            for account_id in (1, 2):
                latest = await notification_assets.load_notification_snapshot(
                    SimpleNamespace(id=account_id, execution_notification_code="configured")
                )
                assert latest["assets"]["total_asset"] == 100000
            page = json.loads((await list_account_asset_snapshots(1, skip=0, limit=1)).body)
            assert page["count"] == 1 and page["data"][0]["assets"]["total_asset"] == 100000
            assert not (json.loads((await list_account_asset_snapshots(1, skip=1, limit=1)).body))["data"]
        for i, result in enumerate(
            [
                legacy(),
                {"account_assets": {"total_asset": 0}},
                {"account_assets": {"total_asset": 500, "positions": []}},
                {"account_assets": {"total_asset": 0, "source": "unavailable"}},
            ]
        ):
            await _persist_execute_record(
                account_id=1,
                execution_id=f"new-{i}",
                raw_input={},
                raw_result=result,
                is_success=0,
                session_factory=sessions,
            )
        async with sessions() as session:
            rows = (
                (
                    await session.execute(
                        select(AccountAssetSnapshot)
                        .where(valid_asset_snapshot_condition(), AccountAssetSnapshot.account_id == 1)
                        .order_by(AccountAssetSnapshot.id)
                    )
                )
                .scalars()
                .all()
            )
            assets = await read_snapshots_assets(session, rows)
            assert [assets[row.id]["total_asset"] for row in rows] == [100000, 0, 500]
            assert (await session.scalar(sa.select(sa.func.count()).select_from(ExecuteRecord))) == 54
        await engine.dispose()

    asyncio.run(check())


def test_non_trading_producer_does_not_claim_zero_balance():
    from types import SimpleNamespace

    from axile.common.trade_channel import TradeChannel
    from axile.executor.abstract_executor.capability import AbstractExecutorCapabilityMixin
    from axile.executor.models.unified_input import GMAccountConfig, UnifiedStandardInput

    executor = SimpleNamespace(
        channel_type=TradeChannel.GM,
        require_execution_runtime=lambda: SimpleNamespace(elapsed_seconds=lambda: 0.1),
    )
    result = AbstractExecutorCapabilityMixin._create_non_trading_output(
        executor,
        UnifiedStandardInput(
            channel_type=TradeChannel.GM,
            account_config=GMAccountConfig(
                account_id="test", token="test", connection_mode="terminal", terminal_path="C:/gm"
            ),
            curr_target={"SHSE.600000": 1},
        ),
    )
    assert result.status == "BLOCKED"
    assert result.error == "非交易时段"
    assert result.reason_code == "COMMON.SESSION.CLOSED"
    assert result.account_assets.source == "unavailable"
    assert not is_asset_observation(result.account_assets.model_dump())
