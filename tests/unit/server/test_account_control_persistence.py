"""账户控制持久化表定义测试。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import Text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from axile.common.trade_channel import TradeChannel
from axile.executor.account_control.models import (
    AccountControlBucketType,
    AccountControlDecision,
    AccountControlScopeType,
)
from axile.server.db.models import Account, AccountControlCounterDelta, AccountControlEvent


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
async def _session_scope() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)

        session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with session_factory() as session:
            yield session
    finally:
        await engine.dispose()


def test_account_control_tables_use_explicit_columns_and_unique_uids() -> None:
    """运行期表应使用显式列和稳定 UID，而不是 JSON state。"""
    delta_columns = set(AccountControlCounterDelta.__table__.columns.keys())
    event_columns = set(AccountControlEvent.__table__.columns.keys())
    delta_operation_column = AccountControlCounterDelta.__table__.columns["operation"]
    event_operation_column = AccountControlEvent.__table__.columns["operation"]

    assert delta_columns == {
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
    assert event_columns == {
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
        "created_at",
        "occurred_at_ms",
        "id",
    }
    assert isinstance(delta_operation_column.type, Text)
    assert isinstance(event_operation_column.type, Text)


def test_account_control_tables_enforce_unique_delta_uid_and_event_uid() -> None:
    """delta_uid 和 event_uid 必须由数据库唯一约束兜底。"""

    async def scenario() -> None:
        async with _session_scope() as session:
            account = _build_account()
            session.add(account)
            await session.commit()
            assert account.id is not None
            account_id = account.id

            session.add_all(
                [
                    AccountControlCounterDelta(
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
                    AccountControlCounterDelta(
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
            )
            with pytest.raises(IntegrityError):
                await session.commit()
            await session.rollback()

            session.add_all(
                [
                    AccountControlEvent(
                        account_id=account_id,
                        control_date="2026-03-22",
                        execution_id="exec-1",
                        seq=1,
                        channel=TradeChannel.CTP,
                        operation="place_order",
                        symbol="rb2610",
                        metadata_={},
                        decision=AccountControlDecision.ALLOWED,
                        counted=True,
                        outcome="submitted",
                        event_uid="duplicate-event",
                    ),
                    AccountControlEvent(
                        account_id=account_id,
                        control_date="2026-03-22",
                        execution_id="exec-2",
                        seq=2,
                        channel=TradeChannel.CTP,
                        operation="cancel_order",
                        symbol="ag2612",
                        metadata_={},
                        decision=AccountControlDecision.BLOCKED,
                        counted=False,
                        outcome="policy_blocked",
                        event_uid="duplicate-event",
                    ),
                ]
            )
            with pytest.raises(IntegrityError):
                await session.commit()

    asyncio.run(scenario())
