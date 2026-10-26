"""
提供系统内置的账户控制 preset 定义与解析逻辑.

Notes
-----
该模块会先导入内置 operation 与 group 的声明来源，再基于注册结果叠加 preset
和运行时 override，生成执行期有效策略。
"""

from __future__ import annotations

import importlib

from axile.common.trade_channel import TradeChannel
from axile.executor.account_control.ctp_operations import (
    CTP_TRADER_API_OPERATIONS,
    CTP_TRADER_API_THROTTLE_GROUP,
)
from axile.executor.account_control.exceptions import AccountControlConfigError
from axile.executor.account_control.models import (
    AccountControlOverride,
    AccountControlPresetDefinition,
    EffectiveAccountControlPolicy,
)
from axile.executor.account_control.registry import ensure_default_account_control_registry_bootstrapped
from axile.server._native import _core


def _rule(limit: int, on_trigger: str) -> dict[str, object]:
    return {"limit": limit, "on_trigger": on_trigger}


# 期权行权 / 放弃 / 自对冲操作的统一基线限额。
# 三种 operation（option_exercise / option_abandon / option_self_close）按相同基线，
# 仅 option_self_close 走 ``_OPTION_SELF_CLOSE_OVERRIDES`` 覆盖更严限额。
def _option_action_limits(*, overrides: dict[str, dict[str, object]] | None = None) -> dict[str, object]:
    """生成期权操作限额配置（基线模板 + 可选覆盖）.

    Parameters
    ----------
    overrides : dict[str, dict[str, object]] | None, optional
        ``{scope: {rule_key: rule}}``，用于覆盖基线模板的对应字段。

    Returns
    -------
    dict[str, object]
        合并后的 ``{"account": {...}, "symbol": {...}}`` 限额字典。
    """
    base: dict[str, dict[str, object]] = {
        "account": {
            "per_minute": _rule(10, "wait"),
            "per_day": _rule(200, "block"),
            "min_interval_ms": _rule(1000, "wait"),
        },
        "symbol": {
            "per_minute": _rule(5, "wait"),
            "min_interval_ms": _rule(500, "wait"),
        },
    }
    merged: dict[str, object] = {"priority": 10, **{scope: dict(rules) for scope, rules in base.items()}}
    for scope, scope_overrides in (overrides or {}).items():
        # 注：merged[scope] 可能不存在；这里允许 overrides 引入新 scope。
        scope_dict = merged.setdefault(scope, {})
        if isinstance(scope_dict, dict):
            scope_dict.update(scope_overrides)
    return merged


# 自对冲（option_self_close）风险更高（直接平自有期权头寸），按更严限额覆盖：
# - account.per_day: 100（基线 200）
# - symbol.per_minute: 3（基线 5）
_OPTION_SELF_CLOSE_OVERRIDES: dict[str, dict[str, object]] = {
    "account": {"per_day": _rule(100, "block")},
    "symbol": {"per_minute": _rule(3, "wait")},
}


def _import_builtin_account_control_declarers() -> None:
    """
    导入内置声明模块以建立默认注册.

    Notes
    -----
    这些模块在导入时会通过装饰器或显式调用向默认注册表登记 operation 与 group。
    """
    importlib.import_module("axile.executor.abstract_executor.base")
    importlib.import_module("axile.executor.account_control.ctp_operations")


_import_builtin_account_control_declarers()


ACCOUNT_CONTROL_PRESETS: dict[str, AccountControlPresetDefinition] = {
    "default": AccountControlPresetDefinition(
        preset_key="default",
        compatible_trade_channels=None,
        policy=AccountControlOverride.model_validate(
            {
                "timezone": "Asia/Shanghai",
                "operations": {
                    "place_order": {
                        "account": {
                            "per_minute": _rule(20, "wait"),
                            "per_day": _rule(500, "block"),
                            "min_interval_ms": _rule(300, "wait"),
                        },
                        "symbol": {
                            "per_minute": _rule(10, "wait"),
                            "min_interval_ms": _rule(300, "wait"),
                        },
                    },
                    "cancel_order": {
                        "account": {
                            "per_minute": _rule(30, "wait"),
                            "per_day": _rule(800, "block"),
                            "min_interval_ms": _rule(200, "wait"),
                        },
                        "symbol": {
                            "per_minute": _rule(10, "wait"),
                            "min_interval_ms": _rule(200, "wait"),
                        },
                    },
                    "query_order": {
                        "account": {
                            "per_minute": _rule(120, "wait"),
                            "per_day": _rule(5000, "block"),
                            "min_interval_ms": _rule(100, "wait"),
                        },
                        "symbol": {
                            "per_minute": _rule(10, "wait"),
                            "min_interval_ms": _rule(300, "wait"),
                        },
                    },
                },
            }
        ),
    ),
    "ctp": AccountControlPresetDefinition(
        preset_key="ctp",
        compatible_trade_channels=frozenset({TradeChannel.CTP}),
        operation_groups={
            operation: frozenset({CTP_TRADER_API_THROTTLE_GROUP})
            for operation in {"place_order", *CTP_TRADER_API_OPERATIONS}
        },
        policy=AccountControlOverride.model_validate(
            {
                "timezone": "Asia/Shanghai",
                "operations": {
                    "authenticate": {"priority": 20},
                    "trader_login": {"priority": 20},
                    "query_settlement_status": {
                        "priority": 20,
                        "account": {
                            "per_minute": _rule(20, "wait"),
                            "per_day": _rule(200, "block"),
                        },
                    },
                    "confirm_settlement": {"priority": 20},
                    "place_order": {
                        "priority": 10,
                        "account": {
                            "per_minute": _rule(30, "wait"),
                            "per_day": _rule(500, "block"),
                            "min_interval_ms": _rule(500, "wait"),
                        },
                        "symbol": {
                            "per_minute": _rule(10, "wait"),
                            "min_interval_ms": _rule(300, "wait"),
                        },
                    },
                    "cancel_order": {
                        "priority": 0,
                        "account": {
                            "per_minute": _rule(60, "wait"),
                            "per_day": _rule(400, "block"),
                            "min_interval_ms": _rule(200, "wait"),
                        },
                        "symbol": {
                            "per_minute": _rule(10, "wait"),
                            "min_interval_ms": _rule(200, "wait"),
                        },
                    },
                    "query_order": {
                        "account": {
                            "per_minute": _rule(60, "wait"),
                            "per_day": _rule(2000, "block"),
                            "min_interval_ms": _rule(1500, "wait"),
                        },
                        "symbol": {
                            "per_minute": _rule(10, "wait"),
                            "min_interval_ms": _rule(300, "wait"),
                        },
                    },
                    "query_commission_rates": {
                        "priority": 100,
                        "account": {"per_minute": _rule(30, "wait"), "per_day": _rule(2000, "block")},
                    },
                    "query_instruments": {
                        "priority": 100,
                        "account": {
                            "per_minute": _rule(60, "wait"),
                            "per_day": _rule(2000, "block"),
                        },
                    },
                    "query_account": {
                        "priority": 100,
                        "account": {
                            "per_minute": _rule(60, "wait"),
                            "per_day": _rule(2000, "block"),
                        },
                    },
                    "query_positions": {
                        "priority": 100,
                        "account": {
                            "per_minute": _rule(60, "wait"),
                            "per_day": _rule(2000, "block"),
                        },
                    },
                    "query_depth_market_data": {
                        "priority": 100,
                        "account": {
                            "per_minute": _rule(60, "wait"),
                            "per_day": _rule(2000, "block"),
                        },
                    },
                    "query_orders": {
                        "priority": 100,
                        "account": {
                            "per_minute": _rule(60, "wait"),
                            "per_day": _rule(2000, "block"),
                        },
                    },
                    "ctp_query_trades": {
                        "priority": 100,
                        "account": {
                            "per_minute": _rule(60, "wait"),
                            "per_day": _rule(2000, "block"),
                        },
                    },
                    "query_settlement_info": {
                        "priority": 100,
                        "account": {
                            "per_minute": _rule(20, "wait"),
                            "per_day": _rule(200, "block"),
                        },
                    },
                    # 期权行权 / 放弃 / 自对冲操作（ReqExecOrderInsert / ReqOptionSelfCloseInsert）
                    # 风险高于普通下单，按更严格的频次限额配置；典型场景：到期日临近的批量行权。
                    # 共用基线模板 _option_action_limits()；self_close 走 OVERRIDES 进一步收紧。
                    "option_exercise": _option_action_limits(),
                    "option_abandon": _option_action_limits(),
                    "option_self_close": _option_action_limits(overrides=_OPTION_SELF_CLOSE_OVERRIDES),
                    "cancel_order_ctp": {"priority": 0},
                    "cancel_option_exercise": {"priority": 0},
                    "cancel_option_abandon": {"priority": 0},
                    "cancel_option_self_close": {"priority": 0},
                },
                "groups": {
                    "ctp_td_global": {
                        "min_interval_ms": _rule(1500, "wait"),
                    }
                },
            }
        ),
    ),
}


def get_account_control_preset(preset_key: str) -> AccountControlPresetDefinition:
    """
    根据键获取系统内置 preset 定义.

    Parameters
    ----------
    preset_key : str
        preset 唯一键。

    Returns
    -------
    AccountControlPresetDefinition
        命中的 preset 定义。

    Raises
    ------
    AccountControlConfigError
        当 preset_key 未命中时抛出。
    """
    preset = ACCOUNT_CONTROL_PRESETS.get(preset_key)
    if preset is None:
        raise AccountControlConfigError(
            "CONTROL_CONFIG_UNKNOWN_PRESET", ["account_control_preset"], f"未知的账户控制 preset: {preset_key}"
        )
    return preset


def ensure_account_control_preset_compatible(preset_key: str, trade_channel: TradeChannel) -> None:
    """
    校验 preset 与交易渠道是否兼容.

    Parameters
    ----------
    preset_key : str
        preset 唯一键。
    trade_channel : TradeChannel
        需要校验的交易渠道。

    Raises
    ------
    AccountControlConfigError
        当 preset 不存在或与渠道不兼容时抛出。
    """
    preset = get_account_control_preset(preset_key)
    if preset.compatible_trade_channels is not None and trade_channel not in preset.compatible_trade_channels:
        raise AccountControlConfigError(
            "CONTROL_CONFIG_INCOMPATIBLE_PRESET",
            ["account_control_preset"],
            f"账户控制 preset `{preset_key}` 与 trade_channel `{trade_channel}` 不兼容",
        )


def resolve_account_control_policy(
    preset_key: str,
    override: dict | AccountControlOverride | None = None,
) -> EffectiveAccountControlPolicy:
    """
    解析 preset 与 override 后的执行期有效策略.

    Parameters
    ----------
    preset_key : str
        preset 唯一键。
    override : dict | AccountControlOverride | None, optional
        账户级运行时覆盖配置。

    Returns
    -------
    EffectiveAccountControlPolicy
        完成默认策略、preset 与 override 叠加后的有效策略。

    Notes
    -----
    每次调用创建独立快照，不与 preset 或 override 共享可变规则；执行期间仅读取。
    """
    preset = get_account_control_preset(preset_key)
    ensure_default_account_control_registry_bootstrapped()
    return _core.resolve_control_policy(preset_key, preset.policy, override, preset.operation_groups)
