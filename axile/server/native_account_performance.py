"""补齐结构化分析事实后原样返回 Rust 编码的独立账户收益。"""

from __future__ import annotations

from dataclasses import dataclass

import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from axile.server.analysis_calendar import ensure_calendar_facts
from axile.server.analysis_observations import ensure_observation_facts
from axile.server.db.models import Account
from axile.server.db.models.account_settings import AccountSettings, BacktestSettings
from axile.server.db.models.analysis import analysis_state as states
from axile.server.db.models.performance import PerformanceSettings, RangeKey
from axile.server.native_analysis import NativeAnalysisError, get_native_analysis
from axile.server.performance_replay import ENGINE_VERSION


@dataclass(frozen=True, slots=True)
class _Metadata:
    settings: dict
    channel: str
    source_version: int | None


async def _metadata(session: AsyncSession, account_id: int) -> _Metadata:
    """在短读事务内只读取回测设置、渠道和同版本标识。"""
    await session.execute(sa.text("BEGIN"))
    row = (
        await session.execute(
            sa.select(col(Account.trade_channel), col(AccountSettings.backtest), states.c.source_version)
            .select_from(Account)
            .outerjoin(AccountSettings, col(AccountSettings.account_id) == col(Account.id))
            .outerjoin(states, states.c.account_id == col(Account.id))
            .where(col(Account.id) == account_id)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(404, detail="账户不存在")
    backtest = row.backtest or BacktestSettings()
    settings = PerformanceSettings.model_validate(backtest.model_dump()).model_dump()
    result = _Metadata(settings, str(row.trade_channel), row.source_version)
    await session.rollback()
    return result


async def native_account_performance(session: AsyncSession, account_id: int, range_key: RangeKey) -> bytes | None:
    """使用 Rust 计算独立账户收益；未启用原生路径时不触碰调用方事务。

    Notes
    -----
    初次兼容补齐会按批读取旧载荷。稳定运行只扫描结构化事实。
    元数据先固定源版本，再补齐；补齐期间有业务变更时由 Rust 快照内
    的版本检查拒绝，最多重试一次。补齐使用独立会话，避免提交调用方写入。
    """
    native = get_native_analysis(session)
    if native is None:
        return None
    from axile.server.performance_analysis import LOGIC_VERSION

    await session.rollback()
    async with AsyncSession(bind=session.bind, expire_on_commit=False) as dedicated:
        for attempt in range(2):
            metadata = await _metadata(dedicated, account_id)
            await ensure_observation_facts(dedicated, account_id)
            await ensure_calendar_facts(dedicated, account_id, metadata.channel)
            try:
                encoded = await native.query(
                    "performance",
                    account_id=account_id,
                    range=range_key,
                    settings=metadata.settings,
                    include_backtest=False,
                    include_metadata=True,
                    require_observation_facts=True,
                    expected_source_version=metadata.source_version,
                    logic_version=LOGIC_VERSION,
                    engine_version=ENGINE_VERSION,
                )
            except NativeAnalysisError as error:
                if error.code == "SOURCE_VERSION_CHANGED" and attempt == 0:
                    continue
                raise
            return encoded
    raise AssertionError("原生账户收益重试循环必须返回或抛出异常")
