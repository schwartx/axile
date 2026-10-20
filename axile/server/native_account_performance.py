"""从结构化事实计算账户收益，Python 仅补充渠道日历与组合绑定元数据。"""

from __future__ import annotations

import json
from dataclasses import dataclass

import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from starlette.concurrency import run_in_threadpool

from axile.server.analysis_observations import ensure_observation_facts
from axile.server.db.models import Account, PortfolioAccount
from axile.server.db.models.account_settings import AccountSettings, BacktestSettings
from axile.server.db.models.analysis import analysis_state as states
from axile.server.db.models.performance import PerformanceSettings, RangeKey
from axile.server.native_analysis import NativeAnalysisError, NativeAnalysisProtocolError, get_native_analysis
from axile.server.performance import local_time, performance_calendar
from axile.server.performance_replay import ENGINE_VERSION


@dataclass(frozen=True, slots=True)
class _Metadata:
    settings: dict
    channel: str
    source_version: int | None
    bindings: list[tuple[str, int | None]]


async def _metadata(session: AsyncSession, account_id: int) -> _Metadata:
    """在短读事务内只读取回测设置、绑定和同版本标识。"""
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
    bindings = (
        await session.execute(
            sa.select(col(PortfolioAccount.created_at), col(PortfolioAccount.portfolio_id)).where(
                col(PortfolioAccount.account_id) == account_id
            )
        )
    ).all()
    result = _Metadata(settings, str(row.trade_channel), row.source_version, [(when, pid) for when, pid in bindings])
    await session.rollback()
    return result


def _enrich(encoded: bytes, metadata: _Metadata) -> bytes:
    """补充小型业务元数据，不创建任何逐点 Pydantic 模型。"""
    try:
        result = json.loads(encoded)
    except (ValueError, UnicodeDecodeError) as exc:
        raise NativeAnalysisProtocolError("原生账户收益不是 JSON", code="INVALID_NATIVE_PERFORMANCE") from exc
    if not isinstance(result, dict) or "points" not in result:
        raise NativeAnalysisProtocolError("原生账户收益缺少曲线字段", code="INVALID_NATIVE_PERFORMANCE")
    baseline, end = result.get("baseline"), result.get("end")
    result["calendar"] = performance_calendar(metadata.channel, baseline, end).model_dump()
    result["bindings"] = []
    if baseline and end:
        start_time, end_time = local_time(baseline), local_time(end)
        result["bindings"] = [
            {"time": when.isoformat(), "portfolio_id": portfolio_id}
            for when, portfolio_id in sorted(
                ((local_time(when), pid) for when, pid in metadata.bindings), key=lambda item: item[0]
            )
            if start_time <= when <= end_time
        ]
    return json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()


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
            try:
                encoded = await native.query(
                    "performance",
                    account_id=account_id,
                    range=range_key,
                    settings=metadata.settings,
                    include_backtest=False,
                    expected_source_version=metadata.source_version,
                    logic_version=LOGIC_VERSION,
                    engine_version=ENGINE_VERSION,
                )
            except NativeAnalysisError as error:
                if error.code == "SOURCE_VERSION_CHANGED" and attempt == 0:
                    continue
                raise
            return await run_in_threadpool(_enrich, encoded, metadata)
    raise AssertionError("原生账户收益重试循环必须返回或抛出异常")
