"""账户级飞书执行结果通知测试路由."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Any, Literal, NamedTuple

from fastapi import APIRouter, HTTPException, status
from loguru import logger
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from axile.common.default_account_notification import DEFAULT_ACCOUNT_NOTIFICATION_CODE
from axile.common.feishu import push_feishu_card
from axile.common.notification_function import run_notification_function
from axile.executor.algorithms.utils import clock_now
from axile.executor.feishu_notifications import (
    LoggerLike,
    build_execute_results_feishu_card,
)
from axile.executor.models.execution_result import AlgorithmResult, ExecutionStatus
from axile.executor.models.unified_account_assets import UnifiedAccountAssets
from axile.executor.models.unified_input import UnifiedStandardInput
from axile.executor.models.unified_order import OrderDirection, OrderType, TradeRecord, UnifiedOrder
from axile.executor.models.unified_output import UnifiedStandardOutput
from axile.server.account_assets import query_account_assets
from axile.server.api.deps import SessionDep
from axile.server.api.routes.account_support import _get_account_or_404
from axile.server.business_facts.repository import read_execution_detail
from axile.server.db.models.account import AccountContext
from axile.server.db.models.supplement import NotificationEvent
from axile.server.error_details import exception_details
from axile.server.execution.notification_records import build_persisted_notification_context
from axile.server.execution.registry import clear_account_asset_refresh, try_register_account_asset_refresh
from axile.server.repositories import get_recent_execute_records_by_account_id
from axile.server.target_weight_snapshots import get_latest_account_target_snapshot

router = APIRouter()


@router.get("/{account_id}/notification/default")
def get_default_account_notification(account_id: int) -> dict[str, str]:
    """提供可恢复的默认飞书执行通知函数源码。"""
    return {"code": DEFAULT_ACCOUNT_NOTIFICATION_CODE}


class AccountFeishuTestRequest(BaseModel):
    """账户飞书通知测试载荷."""

    model_config = ConfigDict(extra="forbid")

    feishu_key: str


class AccountNotificationFunctionTestRequest(BaseModel):
    """使用当前编辑草稿试跑账户执行通知函数。"""

    code: str
    event_type: Literal["execution.finished", "supplement.cancelled"] = "execution.finished"
    feishu_key: str | None = None


class AccountFeishuTestResult(BaseModel):
    """账户飞书通知测试结果."""

    ok: bool
    message: str
    error_line: int | None = None
    traceback: str | None = None


class _TestNotificationSource:
    """测试卡片构造所需的最小账户视图；账户名带「样例」后缀以免与真实执行混淆。"""

    def __init__(self, account_mark: str) -> None:
        self._account_mark = account_mark
        self.logger: LoggerLike = _TestLogger()

    def _get_account_mark(self) -> str:
        return f"{self._account_mark}（样例）"

    def _get_operation_display(self, order: UnifiedOrder) -> str:
        return "买入" if order.direction == OrderDirection.BUY else "卖出"


class _TestLogger:
    """把测试卡片构造日志接到服务端 Loguru."""

    def info(self, message: object, *args: object, **kwargs: object) -> None:
        logger.info(str(message), *args, **kwargs)

    def error(self, message: object, *args: object, **kwargs: object) -> None:
        logger.error(str(message), *args, **kwargs)


def _test_input(
    account: AccountContext,
    curr_target: dict[str, float],
    last_target: dict[str, float],
) -> UnifiedStandardInput:
    """用账户公开配置与样例目标构造测试输入；成交事实由样例输出承载。"""
    return UnifiedStandardInput.from_dict(
        {
            "channel_type": str(account.trade_channel),
            "account_config": account.account_config,
            "curr_target": curr_target,
            "last_target": last_target,
            "algorithm": account.algorithm,
            "trade_rules": account.trade_rules or {},
            "forbidden_symbols": account.forbidden_symbols or [],
            "risk_symbols": account.risk_symbols or [],
            "feishu_account": {
                "id": account.id,
                "name": account.name,
                "market": account.market,
                "trade_channel": str(account.trade_channel),
                "brokerage": account.brokerage,
                "remark": account.remark,
                "portfolio_id": account.portfolio_id,
                "weight_precision": account.weight_precision,
                "long_leverage": account.long_leverage,
                "short_leverage": account.short_leverage,
            },
            "extra": {
                "audit": {
                    "execution_id": None,
                    "execution_kind": "test",
                    "trigger_source": "feishu_test",
                    "is_test": True,
                }
            },
        }
    )


class _SampleLeg(NamedTuple):
    """样例成交腿：从真实持仓派生的一笔试探性调仓。"""

    symbol: str
    direction: OrderDirection
    price: float
    trade_volume: float
    target_volume: float


def _sample_legs(assets: UnifiedAccountAssets) -> list[_SampleLeg]:
    """从真实持仓取最多两个品种派生样例成交腿（首腿加仓一倍、次腿减半）。"""
    legs: list[_SampleLeg] = []
    for position in assets.positions[:2]:
        volume = float(position.volume)
        if volume <= 0:
            continue
        price = float(position.avg_price or 0.0)
        if price <= 0 and position.market_value is not None:
            multiplier = position.extra.get("volume_multiple", 1)
            if isinstance(multiplier, (int, float)) and multiplier > 0:
                price = position.market_value / (volume * multiplier)
        if price <= 0:
            continue
        if not legs:
            legs.append(_SampleLeg(position.symbol, OrderDirection.BUY, price, volume, volume * 2))
        else:
            half = volume / 2
            legs.append(_SampleLeg(position.symbol, OrderDirection.SELL, price, half, half))
    return legs


def _sample_symbol_results(legs: list[_SampleLeg], algorithm: str) -> dict[str, AlgorithmResult]:
    """把样例成交腿包装成品种级执行结果，字段口径与真实执行一致。"""
    trade_time = clock_now().strftime("%Y-%m-%d %H:%M:%S")
    results: dict[str, AlgorithmResult] = {}
    for index, leg in enumerate(legs):
        order_id = f"sample-order-{index + 1}"
        order = UnifiedOrder(
            order_id=order_id,
            symbol=leg.symbol,
            direction=leg.direction,
            order_type=OrderType.LIMIT,
            volume=leg.trade_volume,
            price=leg.price,
            status="FILLED",
            filled_volume=leg.trade_volume,
            avg_price=leg.price,
        )
        trade = TradeRecord(
            trade_id=f"sample-trade-{index + 1}",
            symbol=leg.symbol,
            order_id=order_id,
            trade_time=trade_time,
            trade_volume=leg.trade_volume,
            trade_price=leg.price,
            trade_value=leg.trade_volume * leg.price,
        )
        results[leg.symbol] = AlgorithmResult(
            symbol=leg.symbol,
            algorithm=algorithm,
            orders=[order],
            trades=[trade],
            target_volume=leg.target_volume,
            status=ExecutionStatus.SUCCEEDED,
        )
    return results


def _holding_weights(assets: UnifiedAccountAssets) -> dict[str, float]:
    """把当前持仓市值折算为目标权重视角的「上一期权重」。"""
    total = float(assets.total_asset)
    if total <= 0:
        return {}
    return {
        position.symbol: position.market_value / total
        for position in assets.positions
        if position.market_value is not None and position.market_value > 0
    }


def _leg_target_weights(legs: list[_SampleLeg], assets: UnifiedAccountAssets) -> dict[str, float]:
    """无目标快照时，按样例目标市值折算当前目标权重。"""
    total = float(assets.total_asset)
    if total <= 0:
        return {}
    return {leg.symbol: leg.target_volume * leg.price / total for leg in legs}


async def _load_sample_target_weights(session: AsyncSession, account: AccountContext) -> dict[str, float]:
    """读取账户当前组合最近的归一化目标权重作为样例目标；无快照则返回空。"""
    if account.id is None or account.portfolio_id is None:
        return {}
    snapshot = await get_latest_account_target_snapshot(session, account.id, account.portfolio_id)
    if snapshot is None or not snapshot.normalized_weights:
        return {}
    return dict(snapshot.normalized_weights)


async def _build_sample_output(
    session: AsyncSession,
    account: AccountContext,
    assets: UnifiedAccountAssets,
) -> UnifiedStandardOutput:
    """用真实资产叠加样例成交，构造接近真实执行版面的输出。"""
    legs = _sample_legs(assets)
    target_weights = await _load_sample_target_weights(session, account)
    curr_target = target_weights or _leg_target_weights(legs, assets)
    algorithm = str(account.algorithm.get("method", "Unknown")) if isinstance(account.algorithm, dict) else "Unknown"
    return UnifiedStandardOutput(
        account_assets=assets,
        inputs=_test_input(account, curr_target=curr_target, last_target=_holding_weights(assets)),
        symbol_results=_sample_symbol_results(legs, algorithm),
        status=ExecutionStatus.SUCCEEDED,
        channel_type=account.trade_channel,
        execution_time=1.0,
    )


async def _build_test_card(
    session: AsyncSession,
    account: AccountContext,
) -> dict[str, object]:
    """读取当前账户资产并构造默认样例卡片。"""
    source = _TestNotificationSource(account.name)

    account_id = account.id
    if account_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="账户不存在")
    if not try_register_account_asset_refresh(account_id):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="账户正在执行或刷新资产，请稍后再试")
    try:
        await session.close()
        assets = await query_account_assets(account)
    except TimeoutError as exc:
        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail="账户权益查询超时，请稍后重试") from exc
    except Exception as exc:
        logger.warning(f"账户飞书测试读取权益失败: account_id={account.id}, error={exc}")
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="账户权益查询失败，请检查渠道连接") from exc
    finally:
        clear_account_asset_refresh(account_id)

    output = await _build_sample_output(session, account, assets)
    return build_execute_results_feishu_card(source, output)


@router.post("/{account_id}/feishu/test", response_model=AccountFeishuTestResult)
async def test_account_feishu(
    session: SessionDep,
    account_id: int,
    payload: AccountFeishuTestRequest,
) -> AccountFeishuTestResult:
    """使用账户当前资产、目标快照与页面草稿推送样例执行结果卡片。"""
    account = await _get_account_or_404(session, account_id)
    key = payload.feishu_key.strip() or account.feishu_key or ""
    if not key:
        return AccountFeishuTestResult(ok=False, message="请先配置飞书 Webhook")
    card = await _build_test_card(session, account)
    await session.close()
    try:
        await asyncio.to_thread(push_feishu_card, card, key)
    except Exception as exc:  # noqa: BLE001 - 统一转为可展示的联通测试结果
        return AccountFeishuTestResult(
            ok=False, message=f"推送失败：{exc}", traceback=str(exception_details(exc)["traceback"])
        )
    return AccountFeishuTestResult(ok=True, message="样例卡片已发送，请在群内确认。")


async def _load_notification_preview_context(
    session: AsyncSession,
    account: AccountContext,
) -> dict[str, Any]:
    """复用最近一次执行的脱敏通知快照，不连接渠道或模拟成交。"""
    records = await get_recent_execute_records_by_account_id(session, account.id, limit=1)
    if not records:
        raise ValueError("当前账户暂无执行记录，无法试跑通知")
    record = records[0]
    event = (
        await session.scalar(
            select(NotificationEvent).where(
                NotificationEvent.account_id == account.id,
                NotificationEvent.id == f"execution:{record.execution_id}",
            )
        )
        if record.execution_id
        else None
    )
    if event is not None and event.context:
        context = deepcopy(event.context)
    else:
        detail = await read_execution_detail(session, record)
        raw = deepcopy(detail["result"])
        if not isinstance(raw, dict):
            raise ValueError("最近一次执行没有可用的通知快照")
        extra = raw.get("extra")
        captured = extra.get("server_notification_context") if isinstance(extra, dict) else None
        if isinstance(captured, dict) and captured:
            context = deepcopy(captured)
        else:
            try:
                context = build_persisted_notification_context(
                    _TestNotificationSource(account.name), raw, detail["input"], account.account_config
                )
            except ValueError as exc:
                raise ValueError("最近一次执行数据不完整，无法重建通知快照") from exc
            execution = context.get("execution")
            if isinstance(execution, dict):
                execution["notified_at"] = record.created_at
    context["is_test"] = True
    context["event_type"] = "execution.finished"
    execution = context.get("execution")
    if isinstance(execution, dict):
        execution["is_test"] = True
        execution["id"] = record.execution_id
        execution.setdefault("notified_at", record.created_at)
    return context


@router.post("/{account_id}/notification/test", response_model=AccountFeishuTestResult)
async def test_account_notification_function(
    session: SessionDep,
    account_id: int,
    payload: AccountNotificationFunctionTestRequest,
) -> AccountFeishuTestResult:
    """复用最近一次执行的真实快照，在独立进程中运行页面草稿函数。"""
    account = await _get_account_or_404(session, account_id)
    key = payload.feishu_key if "feishu_key" in payload.model_fields_set else account.feishu_key
    if payload.code.strip() == DEFAULT_ACCOUNT_NOTIFICATION_CODE.strip() and not key:
        return AccountFeishuTestResult(ok=False, message="请返回基本信息页配置飞书 Webhook")
    try:
        context = await _load_notification_preview_context(session, account)
    except ValueError as exc:
        return AccountFeishuTestResult(ok=False, message=str(exc), traceback=str(exception_details(exc)["traceback"]))
    await session.close()
    if payload.event_type == "supplement.cancelled":
        context = {
            "event_id": "supplement:test:cancelled",
            "event_type": "supplement.cancelled",
            "account": {"id": account.id, "name": account.name},
            "execution": None,
            "is_test": True,
            "supplement": {
                "group_id": "test",
                "base_scheduled_at": clock_now().isoformat(),
                "reason": "terminated",
                "cancel_requested_at": clock_now().isoformat(),
                "steps": [],
            },
            "last_execution": context,
            "last_execution_at": (context.get("execution") or {}).get("notified_at"),
        }
    result = await asyncio.to_thread(run_notification_function, payload.code, context, feishu_key=key)
    return AccountFeishuTestResult(
        ok=result.ok,
        message="已使用最近一次执行快照试跑成功" if result.ok else result.error or "运行失败",
        error_line=getattr(result, "error_line", None),
        traceback=getattr(result, "traceback", None),
    )
