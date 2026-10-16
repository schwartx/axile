"""委托展示字段白名单；外部渠道在适配层提供同名字段。"""

from math import isfinite
from typing import Any

TEXT_FIELDS = (
    "trading_day",
    "hedge_flag",
    "native_order_type",
    "original_order_type",
    "time_condition",
    "gtd_date",
    "volume_condition",
    "contingent_condition",
    "submit_status",
    "active_time",
    "suspend_time",
    "native_update_time",
    "cancel_time",
    "order_local_id",
    "force_close_reason",
    "exchange_order_id",
    "ex_ord_id",
    "offset_leg2",
    "position_side",
    "order_business",
    "order_style",
    "order_duration",
    "order_qualifier",
    "ord_rej_reason",
    "algo_order_id",
    "time_in_force",
    "price_match",
    "self_trade_prevention_mode",
    "working_type",
    "good_till_date",
)
NUMBER_FIELDS = (
    "min_volume",
    "stop_price",
    "filled_amount",
    "value",
    "percent",
    "target_volume",
    "target_value",
    "target_percent",
    "cum_quote",
    "activate_price",
    "price_rate",
)
BOOL_FIELDS = ("is_online", "is_dead", "is_error", "reduce_only", "close_position", "price_protect")


def order_attributes(extra: dict[str, Any]) -> dict[str, str | float | bool]:
    """只发布可展示的原生值，缺失、非法数值和未授权字段不外传。"""
    raw = extra.get("raw_order_data", {})
    values = {**(raw if isinstance(raw, dict) else {}), **{k: v for k, v in extra.items() if v is not None}}
    result: dict[str, str | float | bool] = {}
    for key in TEXT_FIELDS:
        value = values.get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value):
            result[key] = str(value)
    for key in NUMBER_FIELDS:
        value = values.get(key)
        if value is None or isinstance(value, bool):
            continue
        try:
            numeric = float(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if isfinite(numeric) and abs(numeric) < 1.7976931348623157e308:
            result[key] = numeric
    for key in BOOL_FIELDS:
        if isinstance(values.get(key), bool):
            result[key] = values[key]
    return result
