"""账户控制持久化表定义测试。"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, closing
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from axile.common.trade_channel import TradeChannel
from axile.executor.account_control.models import (
    AccountControlBucketType,
    AccountControlDecision,
    AccountControlScopeType,
)
from axile.server.db.models import Account
from axile.server.db.models.base import now_ms, now_str
from tests.fixtures.rust_store.oracle_tables import table
from tests.sqlite_oracle import to_us


def _build_account() -> Account:
    return Account(
        name="ctp-testnet-sim",
        market="期货",
        trade_channel=TradeChannel.CTP,
        account_control_preset="default",
        account_control_override=None,
        account_config={"api_key": "key", "secret_key": "secret", "is_testnet": True},
        is_started=True,
        cron_expr="*/5 * * * *",
        remark=None,
        brokerage="ctp",
        weight_precision=0.001,
        long_leverage=1.0,
        short_leverage=1.0,
        algorithm={"method": "SINGLE-MAKER"},
        empty_positions_algorithm=None,
        trade_rules={},
        forbidden_symbols=[],
        risk_symbols=[],
        feishu_key=None,
        portfolio_id=1,
        write_empty_record=0,
    )


@asynccontextmanager
async def _session_scope(path: Path) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    try:
        session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with session_factory() as session:
            yield session
    finally:
        await engine.dispose()


def test_account_control_tables_use_explicit_columns_and_unique_uids(native_database: Path) -> None:
    """运行期表应使用显式列和稳定 UID，而不是 JSON state。"""
    with closing(sqlite3.connect(native_database)) as connection:
        delta_columns = {
            row[1]: row[2] for row in connection.execute("PRAGMA table_info(account_control_counter_delta)")
        }
        event_columns = {row[1]: row[2] for row in connection.execute("PRAGMA table_info(account_control_event)")}

    assert set(delta_columns) == {
        "account_id",
        "execution_id",
        "context_kind",
        "source",
        "control_date",
        "bucket_type",
        "bucket_start",
        "scope_type",
        "symbol",
        "operation",
        "delta_count",
        "delta_uid",
        "id",
    }
    assert set(event_columns) == {
        "account_id",
        "control_date",
        "execution_id",
        "context_kind",
        "source",
        "control_date",
        "seq",
        "channel",
        "operation",
        "symbol",
        "metadata",
        "decision",
        "counted",
        "outcome",
        "event_uid",
        "created_at_us",
        "occurred_at_us",
        "id",
    }
    assert delta_columns["operation"] == "TEXT"
    assert event_columns["operation"] == "TEXT"


def test_account_control_tables_enforce_unique_delta_uid_and_event_uid(native_database: Path) -> None:
    """delta_uid 和 event_uid 必须由数据库唯一约束兜底。"""

    async def scenario() -> None:
        async with _session_scope(native_database) as session:
            account = _build_account()
            account_values = {
                key: value
                for key, value in account.model_dump(mode="json").items()
                if key in table("account").c and key != "id"
            }
            account_values.update(created_at_us=to_us(account.created_at), updated_at_us=to_us(account.updated_at))
            inserted = await session.execute(table("account").insert().values(**account_values))
            account.id = inserted.inserted_primary_key[0]
            await session.commit()
            assert account.id is not None
            account_id = account.id

            deltas = [
                dict(
                    account_id=account_id,
                    execution_id="exec-1",
                    control_date="2026-03-22",
                    bucket_type=AccountControlBucketType.DAY,
                    bucket_start="2026-03-22T00:00:00",
                    scope_type=AccountControlScopeType.ACCOUNT,
                    symbol=None,
                    operation="place_order",
                    delta_count=1,
                    delta_uid="duplicate-delta",
                ),
                dict(
                    account_id=account_id,
                    execution_id="exec-2",
                    control_date="2026-03-22",
                    bucket_type=AccountControlBucketType.MINUTE,
                    bucket_start="2026-03-22T09:31:00",
                    scope_type=AccountControlScopeType.ACCOUNT,
                    symbol=None,
                    operation="query_order",
                    delta_count=1,
                    delta_uid="duplicate-delta",
                ),
            ]
            with pytest.raises(
                IntegrityError, match=r"UNIQUE constraint failed: account_control_counter_delta\.delta_uid"
            ):
                await session.execute(
                    table("account_control_counter_delta").insert(),
                    deltas,
                )
                await session.commit()
            await session.rollback()

            events = [
                dict(
                    account_id=account_id,
                    control_date="2026-03-22",
                    execution_id="exec-1",
                    seq=1,
                    channel=TradeChannel.CTP,
                    operation="place_order",
                    symbol="rb2610",
                    metadata={},
                    created_at_us=to_us(now_str()),
                    occurred_at_us=now_ms() * 1000,
                    decision=AccountControlDecision.ALLOWED,
                    counted=True,
                    outcome="submitted",
                    event_uid="duplicate-event",
                ),
                dict(
                    account_id=account_id,
                    control_date="2026-03-22",
                    execution_id="exec-2",
                    seq=2,
                    channel=TradeChannel.CTP,
                    operation="cancel_order",
                    symbol="ag2612",
                    metadata={},
                    created_at_us=to_us(now_str()),
                    occurred_at_us=now_ms() * 1000,
                    decision=AccountControlDecision.BLOCKED,
                    counted=False,
                    outcome="policy_blocked",
                    event_uid="duplicate-event",
                ),
            ]
            with pytest.raises(IntegrityError, match=r"UNIQUE constraint failed: account_control_event\.event_uid"):
                await session.execute(
                    table("account_control_event").insert(),
                    events,
                )
                await session.commit()

    asyncio.run(scenario())
