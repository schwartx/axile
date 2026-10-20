"""真实 SQLite 中执行前输入、审计变体与观测的保形和原子提交。"""

import asyncio
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from axile.server.business_facts import assets, inputs
from axile.server.execution import input_persistence
from scripts.business_facts_contract import business_evidence


async def _database(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(input_persistence, "SessionLocal", sessions)
    return engine, sessions


def test_pre_execute_bundle_preserves_primary_and_independent_audit_variant(monkeypatch):
    async def check():
        engine, sessions = await _database(monkeypatch)
        primary = {"curr_target": {"A": 0}, "account_config": {"password": "synthetic"}}
        audit = {
            "curr_target": {"A": 0.0},
            "last_target": {},
            "strategy_target": {},
            "account_target": {"A": 0.0},
            "sizing_context": {},
            "trade_rules": {},
            "futures_close_policy": {},
            "execution_notification_code": "synthetic",
        }
        before = {"total_asset": 0.0, "positions": [], "source": "real"}
        try:
            primary_id, audit_id = await input_persistence._persist_pre_execute_bundle(
                account_id=1,
                execution_id="bundle",
                payload=primary,
                audit_payload=audit,
                before_account_assets=before,
            )
            assert primary_id != audit_id
            assert await input_persistence._persist_pre_execute_bundle(
                account_id=1,
                execution_id="bundle",
                payload=primary,
                audit_payload=audit,
                before_account_assets=before,
            ) == (primary_id, audit_id)
            async with sessions() as session:
                assert await inputs.read_input(session, primary_id) == {"curr_target": {"A": 0}}
                restored = await inputs.read_input(session, audit_id)
                assert business_evidence(restored) == business_evidence(
                    {key: value for key, value in audit.items() if key != "execution_notification_code"}
                )
                assert restored["curr_target"]["A"] == 0
                assert (await inputs.read_input(session, primary_id))["curr_target"]["A"] == 0
                variants = (await session.execute(sa.select(inputs.execution_input))).mappings().all()
                assert len(variants) == 2
                owner = next(row for row in variants if row["id"] == audit_id)
                assert owner["execution_id"] is None
                assert owner["owner_execution_id"] == "bundle"
                snapshots = (await session.execute(sa.select(assets.asset_snapshot))).mappings().all()
                assert len(snapshots) == 1
                assert snapshots[0]["input_id"] == primary_id
        finally:
            await engine.dispose()

    asyncio.run(check())


def test_bundle_variant_failure_rolls_back_primary_and_before_assets(monkeypatch):
    async def check():
        engine, sessions = await _database(monkeypatch)
        monkeypatch.setattr(
            input_persistence, "persist_owned_input_variant", AsyncMock(side_effect=RuntimeError("variant failure"))
        )
        try:
            with pytest.raises(RuntimeError, match="variant failure"):
                await input_persistence._persist_pre_execute_bundle(
                    account_id=1,
                    execution_id="bundle-failed",
                    payload={"curr_target": {}},
                    audit_payload={"account_target": {"A": 0.3}},
                    before_account_assets={"total_asset": 0.0, "positions": []},
                )
            async with sessions() as session:
                assert await session.scalar(sa.select(sa.func.count()).select_from(inputs.execution_input)) == 0
                assert await session.scalar(sa.select(sa.func.count()).select_from(assets.asset_snapshot)) == 0
        finally:
            await engine.dispose()

    asyncio.run(check())


def test_clear_before_asset_failure_rolls_back_released_input_savepoint(monkeypatch):
    async def check():
        engine, sessions = await _database(monkeypatch)

        def fail_assets(*_args, **_kwargs):
            raise RuntimeError("before asset failure")

        monkeypatch.setattr(assets, "encode_input_assets", fail_assets)
        try:
            with pytest.raises(RuntimeError, match="before asset failure"):
                await input_persistence._persist_pre_execute_input(
                    account_id=1,
                    execution_id="clear-failed",
                    payload={"algorithm": {}},
                    before_account_assets={"total_asset": 0.0, "positions": []},
                )
            async with sessions() as session:
                assert await session.scalar(sa.select(sa.func.count()).select_from(inputs.execution_input)) == 0
                assert await session.scalar(sa.select(sa.func.count()).select_from(assets.asset_snapshot)) == 0
        finally:
            await engine.dispose()

    asyncio.run(check())
