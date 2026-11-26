"""调仓信号的计算、定时触发与持久化通知。"""

import asyncio
from datetime import UTC, datetime
from typing import Any

from loguru import logger

from axile.channels.rebalance_signal import (
    SIGNAL_CHANNEL,
    SIGNAL_EVENT,
    SignalAccountConfig,
    SignalAccountContext,
    signal_market_descriptor,
)
from axile.common.serialization import parse_input, to_payload
from axile.executor.algorithms.utils.clock import clock_now_iso
from axile.executor.models.portfolio_target import PortfolioTarget
from axile.executor.models.unified_account_assets import UnifiedAccountAssets
from axile.server.context import ContextSettings
from axile.server.db.models.account import Account, AccountContext
from axile.server.execution.execution_algorithms import resolve_account_leverages
from axile.server.execution.notification_outbox import wake_notifications
from axile.server.native_store import native_operation
from axile.server.repositories import get_account
from axile.server.signal_runner import SignalCalculation, run_signal


async def calculate_signal(
    account: AccountContext,
    portfolio_code: str | None,
    *,
    leverage_overrides: dict[str, float] | None = None,
) -> SignalCalculation:
    """手动刷新、试跑与定时计算共用账户与行情函数。"""
    config = parse_input(SignalAccountConfig, account.account_config)
    long_leverage, short_leverage = resolve_account_leverages(account)
    overrides = leverage_overrides or {}
    settings = ContextSettings(
        long_leverage=overrides.get("long_leverage", long_leverage),
        short_leverage=overrides.get("short_leverage", short_leverage),
        weight_precision=account.weight_precision,
    )
    return await asyncio.to_thread(
        run_signal,
        config,
        {"account_id": account.id, "name": account.name, "market": account.market},
        portfolio_code,
        account.execution_timeout,
        settings,
    )


def signal_quantity_unit(market: str) -> str:
    """按账户已选市场提供目标数量单位。"""
    return signal_market_descriptor(market).units.quantity_label


def _signal_notification(
    account: SignalAccountContext,
    event_id: str,
    at: str,
    scheduled_at: str,
    result: SignalCalculation | None,
    *,
    is_test: bool = False,
) -> dict[str, object]:
    failed = result is None
    targets = result.target.targets if result is not None and result.target is not None else {}
    assets = result.assets if result is not None else None
    positions = (
        [to_payload(position, mode="json", exclude={"extra"}) for position in assets.positions]
        if assets is not None and assets.extra["positions_known"]
        else None
    )
    return {
        "event_id": event_id,
        "event_type": SIGNAL_EVENT,
        "account": {"id": account["account_id"], "name": account.get("name", ""), "market": account["market"]},
        "execution": {
            "id": None,
            "kind": "signal",
            "trigger_source": "notification_test" if is_test else "scheduler",
            "status": "FAILED" if failed else "SUCCEEDED",
            "success": not failed,
            "error": "账户数据、行情或组合计算失败，请查看服务日志" if failed else None,
            "notified_at": at,
            "is_test": is_test,
        },
        "scheduled_at": scheduled_at,
        "quantity_unit": signal_quantity_unit(account["market"]),
        "is_test": is_test,
        "strategy": {},
        "assets": {}
        if assets is None
        else {
            "total_assets": assets.total_asset,
            "available_cash": assets.available_cash,
            "currency": assets.currency,
            "update_time": assets.update_time,
        },
        "targets": {"mode": "quantity", "current": targets, "target_volume": targets},
        "positions": positions,
        "orders": [],
        "trades": [],
        "symbols": [],
        "summary": {"symbol_count": len(targets), "order_count": 0, "trade_count": 0},
        "default_feishu_variables": {},
    }


def signal_notification_sample(name: str, market: str) -> tuple[dict[str, object], dict[str, float], str]:
    """从已选市场生成测试卡片与通知函数共用的样例。"""
    descriptor = signal_market_descriptor(market)
    assert descriptor.portfolio is not None
    quantity = 100.0 if descriptor.units.quantity_kind == "share" else 1.0
    targets = dict.fromkeys(descriptor.portfolio.example_symbols[:2], quantity)
    at = clock_now_iso()
    assets = UnifiedAccountAssets(
        total_asset=10000,
        available_cash=10000,
        market_value=0,
        currency=descriptor.currency,
        update_time=at,
        extra={"positions_known": True},
    )
    result = SignalCalculation(assets, PortfolioTarget(mode="quantity", targets=targets))
    context = _signal_notification(
        {"account_id": None, "name": name, "market": market}, "signal:test", at, at, result, is_test=True
    )
    return context, targets, descriptor.units.quantity_label


async def run_scheduled_signal(account_id: int, scheduled_at: datetime) -> None:
    """加载已绑定组合，原子保存同一次触发的结果与待发通知。"""
    from axile.server.execution.registry import clear_account_asset_refresh, try_register_account_asset_refresh

    account = await get_account(account_id)
    if account is None or account.archived_at or not account.is_started or account.trade_channel != SIGNAL_CHANNEL:
        return
    portfolio_id = account.portfolio_id
    if portfolio_id is None or not try_register_account_asset_refresh(account_id):
        return
    try:
        bound = await native_operation("portfolio_operation", {"op": "bound_context", "account_id": account_id})
        portfolio = bound["portfolio"]
        if portfolio is None or portfolio["id"] != portfolio_id:
            return
        result = await _scheduled_calculation(account, portfolio)
        planned = scheduled_at.astimezone(UTC).isoformat()
        event_id = f"signal:{account_id}:{planned}"
        at = clock_now_iso()
        targets = result.target.targets if result is not None and result.target is not None else None
        await native_operation(
            "target_operation",
            {
                "op": "signal_commit",
                "account_id": account_id,
                "portfolio_id": portfolio_id,
                "revision": account.runtime_sync.revision if account.runtime_sync is not None else None,
                "target_mode": "quantity",
                "source": "signal",
                "raw_weights": targets,
                "normalized_weights": targets,
                "calculated_at": at,
                "account_assets": to_payload(result.assets, mode="json") if result is not None else None,
                "notification": {
                    "id": event_id,
                    "event_type": SIGNAL_EVENT,
                    "account_id": account_id,
                    "created_at": at,
                    "context": _signal_notification(
                        {"account_id": account.id, "name": account.name, "market": account.market},
                        event_id,
                        at,
                        planned,
                        result,
                    ),
                },
            },
        )
        wake_notifications(account_id)
    finally:
        clear_account_asset_refresh(account_id)


async def _scheduled_calculation(account: Account, portfolio: dict[str, Any]) -> SignalCalculation | None:
    try:
        return await calculate_signal(account, portfolio["custom_calc_py_code"])
    except (ValueError, TimeoutError):
        logger.bind(account_id=account.id).exception("调仓信号计算失败")
        return None
