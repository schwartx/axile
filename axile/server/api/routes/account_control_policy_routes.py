"""账户流控预设方案与生效配置的只读接口."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status

from axile.executor.account_control.presets import (
    ACCOUNT_CONTROL_PRESETS,
    ensure_account_control_preset_compatible,
    resolve_account_control_policy,
)
from axile.executor.account_control.registry import ensure_default_account_control_registry_bootstrapped
from axile.server.api.routes.account_support import _get_account_or_404

router = APIRouter()


_PRESET_META = {
    "default": ("默认", "适合通用交易渠道"),
    "ctp": ("CTP", "适合 CTP 期货与期权交易"),
}

_OPERATION_META = {
    "authenticate": ("认证", "向 CTP 交易前置提交客户端认证", "连接操作"),
    "trader_login": ("交易登录", "登录 CTP 交易前置", "连接操作"),
    "query_settlement_status": ("查询结算确认", "查询当前交易日结算确认状态", "连接操作"),
    "confirm_settlement": ("确认结算", "确认当前交易日结算信息", "连接操作"),
    "place_order": ("下单", "提交新的交易委托", "常用操作"),
    "cancel_order": ("撤单", "撤销尚未完成的交易委托", "常用操作"),
    "query_order": ("查询订单", "查询单个或当前未完成订单", "常用操作"),
    "query_trades": ("查询成交", "查询订单成交明细", "查询操作"),
    "query_instruments": ("查询合约", "查询可交易合约信息", "查询操作"),
    "query_account": ("查询资金", "查询账户资金信息", "查询操作"),
    "query_positions": ("查询持仓", "查询账户持仓信息", "查询操作"),
    "query_orders": ("查询委托", "查询 CTP 委托记录", "查询操作"),
    "ctp_query_trades": ("查询 CTP 成交", "查询 CTP 柜台成交记录", "查询操作"),
    "query_settlement_info": ("查询结算单", "查询账户结算信息", "查询操作"),
    "cancel_order_ctp": ("CTP 撤单", "向 CTP 交易前置提交撤单", "交易操作"),
    "option_exercise": ("期权行权", "提交期权行权请求", "期权操作"),
    "option_abandon": ("期权放弃", "提交期权放弃请求", "期权操作"),
    "option_self_close": ("期权自对冲", "提交期权自对冲请求", "期权操作"),
    "cancel_option_exercise": ("撤销期权行权", "撤销尚未完成的期权行权请求", "期权操作"),
    "cancel_option_abandon": ("撤销期权放弃", "撤销尚未完成的期权放弃请求", "期权操作"),
    "cancel_option_self_close": ("撤销期权自对冲", "撤销尚未完成的期权自对冲请求", "期权操作"),
}

_GROUP_META = {
    "ctp_td_global": (
        "CTP TraderApi 请求总间隔",
        "认证、登录、结算、下单、撤单、期权及查询的真实 TraderApi 请求共同受此限制",
    ),
}


def _compatible_presets(trade_channel: object) -> list[dict]:
    result: list[dict] = []
    for key in ACCOUNT_CONTROL_PRESETS:
        try:
            ensure_account_control_preset_compatible(key, trade_channel)  # type: ignore[arg-type]
        except ValueError:
            continue
        name, description = _PRESET_META.get(key, ("扩展方案", "渠道提供的流控预设方案"))
        result.append(dict(key=key, display_name=name, description=description))
    return result


@router.get("/{account_id}/control/policy")
async def get_account_control_policy(
    account_id: int,
    preset_key: str | None = Query(default=None),
) -> dict:
    """读取或无副作用预览账户流控配置."""
    account = await _get_account_or_404(account_id)
    selected_key = preset_key or account.account_control_preset
    try:
        ensure_account_control_preset_compatible(selected_key, account.trade_channel)
        preset_policy = resolve_account_control_policy(selected_key)
        effective_policy = resolve_account_control_policy(selected_key, account.account_control_override)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    registry = ensure_default_account_control_registry_bootstrapped()
    operations: list[dict] = []
    for index, operation in enumerate(registry.operations.values(), start=1):
        name, description, category = _OPERATION_META.get(
            operation.key,
            (f"扩展操作 {index}", "渠道提供的扩展流控操作", "扩展操作"),
        )
        operations.append(
            dict(
                key=operation.key,
                display_name=name,
                description=description,
                category=category,
                groups=sorted(set(operation.groups) | set(effective_policy.operation_groups.get(operation.key, ()))),
            )
        )

    groups: list[dict] = []
    for index, group in enumerate(registry.groups.values(), start=1):
        name, description = _GROUP_META.get(
            group.key,
            (f"扩展共享限制 {index}", "渠道提供的共享流控限制"),
        )
        groups.append(dict(key=group.key, display_name=name, description=description))

    preset_name, _ = _PRESET_META.get(selected_key, ("扩展方案", ""))
    return dict(
        preset_key=selected_key,
        preset_display_name=preset_name,
        compatible_presets=_compatible_presets(account.trade_channel),
        timezone_display_name=(
            "中国标准时间（UTC+8）" if effective_policy.timezone == "Asia/Shanghai" else "账户配置时区"
        ),
        override=account.account_control_override.model_dump(mode="json") if account.account_control_override else None,
        preset_policy=preset_policy.model_dump(mode="json", exclude={"preset_key", "operation_groups"}),
        effective_policy=effective_policy.model_dump(mode="json", exclude={"preset_key", "operation_groups"}),
        operations=operations,
        groups=groups,
    )
