"""CTP 渠道使用的公共账户控制操作声明。"""

from axile.executor.account_control.decorator_registry import (
    build_registered_operation,
    register_operation_bootstrap,
    register_or_validate_operation,
)
from axile.executor.account_control.registry import (
    get_default_account_control_registry,
    register_default_registry_bootstrap,
)

CTP_TRADER_API_THROTTLE_GROUP = "ctp_td_global"
CTP_TRADER_API_OPERATIONS = frozenset(
    {
        "authenticate",
        "trader_login",
        "query_settlement_status",
        "confirm_settlement",
        "cancel_order_ctp",
        "query_instruments",
        "query_commission_rates",
        "query_account",
        "query_positions",
        "query_depth_market_data",
        "query_orders",
        "ctp_query_trades",
        "query_settlement_info",
        "option_exercise",
        "option_abandon",
        "option_self_close",
        "cancel_option_exercise",
        "cancel_option_abandon",
        "cancel_option_self_close",
    }
)


def _register_group() -> None:
    registry = get_default_account_control_registry()
    if not registry.is_frozen and registry.get_group(CTP_TRADER_API_THROTTLE_GROUP) is None:
        registry.register_group(CTP_TRADER_API_THROTTLE_GROUP)


def register_ctp_account_control_operations() -> None:
    """登记 CTP 操作与共享柜台节流组。"""
    _register_group()
    register_default_registry_bootstrap(_register_group)
    for key in CTP_TRADER_API_OPERATIONS:
        operation = build_registered_operation(key, groups=())
        register_or_validate_operation(operation)
        register_default_registry_bootstrap(lambda value=operation: register_operation_bootstrap(value))


register_ctp_account_control_operations()
