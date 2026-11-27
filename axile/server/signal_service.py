"""调仓信号的计算、定时触发与持久化通知。"""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal
from uuid import uuid4

from loguru import logger

from axile.channels.gm_sessions import GMSessionSettings
from axile.channels.rebalance_signal import (
    SIGNAL_CHANNEL,
    SIGNAL_EVENT,
    SignalAccountConfig,
    SignalAccountContext,
    signal_market_descriptor,
)
from axile.common.serialization import copy_data, parse_input, to_payload
from axile.executor.algorithms.utils.clock import clock_now_iso
from axile.executor.models.portfolio_target import PortfolioTarget
from axile.executor.models.unified_account_assets import Position, PositionDirection, UnifiedAccountAssets
from axile.server.context import ContextSettings
from axile.server.cron import SCHEDULER_TIMEZONE, is_blank_cron_expr, parse_cron_expr
from axile.server.db.models.account import Account, AccountContext
from axile.server.execution.execution_algorithms import resolve_account_leverages
from axile.server.execution.notification_outbox import wake_notifications
from axile.server.native_store import native_operation
from axile.server.repositories import get_account
from axile.server.signal_runner import SignalCalculation, run_signal
from axile.server.trading_calendar import CalendarDecisionStatus, evaluate_channel_calendar_moment, schedule_windows


def _followed_positions(targets: dict[str, float]) -> list[Position]:
    return [
        Position(
            symbol=symbol,
            direction=PositionDirection.LONG if quantity > 0 else PositionDirection.SHORT,
            volume=abs(quantity),
            available_volume=0,
            market_value=None,
        )
        for symbol, quantity in targets.items()
        if quantity
    ]


async def _latest_signal_record(
    account_id: int, *, before: str | None = None, followed: bool = False, ledger: bool = False
) -> dict | None:
    records = await native_operation(
        "signal_records",
        {
            "account_id": account_id,
            "limit": 1,
            "trigger_source": "scheduler" if before is not None else None,
            "scheduled_before": before,
            "followed": followed,
            "ledger": ledger,
        },
    )
    return next(iter(records["items"]), None)


def _changes(previous: dict[str, float], current: dict[str, float]) -> dict[str, dict[str, float]]:
    changes = {}
    for symbol in dict.fromkeys((*current, *previous)):
        before, after = previous.get(symbol, 0.0), current.get(symbol, 0.0)
        if before != after:
            changes[symbol] = {
                "before": before,
                "after": after,
                "delta": float(Decimal(str(after)) - Decimal(str(before))),
            }
    return changes


def _missed_node(account: AccountContext, previous: datetime, current: datetime) -> bool:
    if is_blank_cron_expr(account.cron_expr):
        return False
    channel = signal_market_descriptor(account.market).channel
    windows = schedule_windows(channel, GMSessionSettings.from_channel_options(account.channel_options))
    start = previous.astimezone(SCHEDULER_TIMEZONE)
    for trigger in parse_cron_expr(account.cron_expr):
        candidate = trigger.get_next_fire_time(start, start)
        while candidate is not None and candidate < current:
            decision = evaluate_channel_calendar_moment(channel, candidate, windows=windows)
            if decision.status is not CalendarDecisionStatus.AVAILABLE_CLOSED:
                return True
            candidate = trigger.get_next_fire_time(candidate, candidate)
    return False


async def _node_change(account: Account, planned: str, targets: dict[str, float]) -> dict[str, object]:
    assert account.id is not None
    previous = await _latest_signal_record(account.id, before=planned)
    if previous is None:
        return {"status": "first"}
    previous_at = previous["scheduled_at"]
    if _missed_node(account, datetime.fromisoformat(previous_at), datetime.fromisoformat(planned)):
        return {"status": "missing"}
    if previous["status"] != "SUCCEEDED":
        return {"status": "missing", "previous_scheduled_at": previous_at}
    return {
        "status": "compared",
        "previous_scheduled_at": previous_at,
        "changes": _changes(previous["targets"], targets),
    }


def _adjustment(
    assets: UnifiedAccountAssets, targets: dict[str, float], *, stock_orders: bool = False
) -> dict[str, object] | None:
    if not stock_orders and assets.source != "signal_function":
        return None
    order_info = (
        {
            "kind": "order",
            "basis": "signal_holdings" if assets.source == "signal_follow" else "actual_holdings",
        }
        if stock_orders
        else {}
    )
    if not assets.extra["positions_known"]:
        return {"status": "unknown", **order_info}
    actual: dict[str, float] = {}
    for position in assets.positions:
        sign = -1 if position.direction == PositionDirection.SHORT else 1
        actual[position.symbol] = actual.get(position.symbol, 0.0) + sign * position.volume
    if stock_orders and any(quantity < 0 for quantity in (*actual.values(), *targets.values())):
        return {"status": "unsupported", **order_info}
    return {"status": "compared", "changes": _changes(actual, targets), **order_info}


async def calculate_signal(
    account: AccountContext,
    portfolio_code: str | None,
    *,
    leverage_overrides: dict[str, float] | None = None,
    size_weights: bool = False,
    trial: bool = False,
    settle_follow: bool = False,
) -> SignalCalculation:
    """手动刷新、试跑与定时计算共用账户与行情函数。"""
    config = parse_input(SignalAccountConfig, account.account_config)
    ledger_enabled = config.follow_target and signal_market_descriptor(account.market).market == "ashare"
    followed = False
    if config.follow_target and config.manual_assets is not None and account.id is not None:
        previous = await _latest_signal_record(account.id, followed=not ledger_enabled, ledger=ledger_enabled)
        if previous is not None:
            followed = True
            updates = {"positions": _followed_positions(previous["targets"])}
            if ledger_enabled:
                ledger = previous["follow_ledger"]
                updates = {
                    "positions": _followed_positions(ledger["positions"]),
                    "available_cash": ledger["cash"],
                }
            config = copy_data(config, update={"manual_assets": copy_data(config.manual_assets, update=updates)})
    long_leverage, short_leverage = resolve_account_leverages(account)
    overrides = leverage_overrides or {}
    settings = ContextSettings(
        long_leverage=overrides.get("long_leverage", long_leverage),
        short_leverage=overrides.get("short_leverage", short_leverage),
        weight_precision=account.weight_precision,
    )
    result = await asyncio.to_thread(
        run_signal,
        config,
        {"account_id": account.id, "name": account.name, "market": account.market},
        portfolio_code,
        account.execution_timeout,
        settings,
        size_weights=size_weights or (trial and config.quote_source.kind == "ctp"),
        follow_fee_bps=config.follow_fee_bps if ledger_enabled else None,
        settle_follow=settle_follow,
    )
    if followed:
        result.assets.source = "signal_follow"
    return result


def signal_quantity_unit(market: str) -> str:
    """按账户已选市场提供目标数量单位。"""
    return signal_market_descriptor(market).units.quantity_label


def _signal_notification(
    account: SignalAccountContext,
    event_id: str,
    at: str,
    scheduled_at: str | None,
    result: SignalCalculation | None,
    *,
    is_test: bool = False,
    trigger_source: Literal["manual", "scheduler"] = "scheduler",
) -> dict[str, object]:
    failed = result is None
    targets = result.target_quantities() if result is not None else {}
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
            "trigger_source": "notification_test" if is_test else trigger_source,
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


def signal_notification_sample(name: str, market: str, at: str) -> tuple[dict[str, object], dict[str, float], str]:
    """从已选市场生成测试卡片与通知函数共用的样例。"""
    descriptor = signal_market_descriptor(market)
    assert descriptor.portfolio is not None
    quantity = 100.0 if descriptor.units.quantity_kind == "share" else 1.0
    targets = dict.fromkeys(descriptor.portfolio.example_symbols[:2], quantity)
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
    """按定时触发生成信号，忽略已经暂停或归档的账户。"""
    account = await get_account(account_id)
    if (
        account is not None
        and not account.archived_at
        and account.is_started
        and account.trade_channel == SIGNAL_CHANNEL
    ):
        await publish_account_signal(account, scheduled_at)


async def publish_account_signal(account: Account, scheduled_at: datetime | None = None) -> dict[str, str] | None:
    """计算并原子保存目标快照、资产快照和待发通知。"""
    from axile.server.execution.registry import clear_account_asset_refresh, try_register_account_asset_refresh

    account_id = account.id
    assert account_id is not None
    portfolio_id = account.portfolio_id
    if portfolio_id is None or not try_register_account_asset_refresh(account_id):
        return None
    try:
        planned = scheduled_at.astimezone(UTC).isoformat() if scheduled_at is not None else None
        event_id = f"signal:{account_id}:{planned or uuid4()}"
        if planned is not None and await native_operation(
            "target_operation", {"op": "signal_exists", "account_id": account_id, "event_id": event_id}
        ):
            wake_notifications(account_id)
            return {"event_id": event_id, "message": "信号已生成"}

        bound = await native_operation("portfolio_operation", {"op": "bound_context", "account_id": account_id})
        portfolio = bound["portfolio"]
        if portfolio is None or portfolio["id"] != portfolio_id:
            return None
        failure = None
        try:
            result = await calculate_signal(
                account, portfolio["custom_calc_py_code"], size_weights=True, settle_follow=True
            )
        except (ValueError, TimeoutError) as error:
            logger.bind(account_id=account_id).exception("调仓信号计算失败")
            result = None
            failure = error
        at = clock_now_iso()
        context = _signal_notification(
            {"account_id": account.id, "name": account.name, "market": account.market},
            event_id,
            at,
            planned,
            result,
            trigger_source="manual" if scheduled_at is None else "scheduler",
        )
        message = "信号已生成"
        assets = result.assets if result is not None else None
        if result is not None and assets is not None:
            targets = result.target_quantities()
            if result.sizing_prices is not None:
                context["sizing_prices"] = result.sizing_prices
            if result.settlement_prices is not None:
                context["settlement_prices"] = result.settlement_prices
            context["adjustment"] = _adjustment(
                assets, targets, stock_orders=signal_market_descriptor(account.market).market == "ashare"
            )
            if planned is not None:
                context["node_change"] = await _node_change(account, planned, targets)
            if result.follow_ledger is not None:
                context["follow_ledger"] = result.follow_ledger
                context["followed_target"] = result.follow_ledger["status"] == "settled"
                assets = result.follow_assets
                message = {
                    "settled": "跟随已结转",
                    "insufficient_cash": "信号已生成，资金不足未结转",
                    "long_only": "信号已生成，目标含空头未结转",
                }[result.follow_ledger["status"]]
            elif account.account_config.get("follow_target") is True:
                context["followed_target"] = True
                message = "跟随已结转"
                assets = copy_data(
                    assets,
                    update={
                        "positions": _followed_positions(targets),
                        "source": "signal_follow",
                        "extra": {"positions_known": True},
                    },
                )
        await native_operation(
            "target_operation",
            {
                "op": "signal_commit",
                "account_id": account_id,
                "portfolio_id": portfolio_id,
                "revision": account.runtime_sync.revision if account.runtime_sync is not None else None,
                "source": "signal",
                **(result.snapshot_fields() if result is not None else {"raw_weights": None}),
                "calculated_at": at,
                "account_assets": to_payload(assets, mode="json") if assets is not None else None,
                "notification": {
                    "id": event_id,
                    "event_type": SIGNAL_EVENT,
                    "account_id": account_id,
                    "created_at": at,
                    "context": context,
                },
            },
        )
        wake_notifications(account_id)
        if failure is not None and planned is None:
            raise failure
        return {"event_id": event_id, "message": message}
    finally:
        clear_account_asset_refresh(account_id)
