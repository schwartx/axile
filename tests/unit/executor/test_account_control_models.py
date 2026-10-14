"""账户控制配置模型测试。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from axile.common.trade_channel import TradeChannel
from axile.executor.account_control.models import (
    AccountControlOperationOverride,
    AccountControlOperationPolicy,
    AccountControlOverride,
)
from axile.executor.account_control.presets import (
    ensure_account_control_preset_compatible,
    get_account_control_preset,
    resolve_account_control_policy,
)
from axile.executor.account_control.registry import (
    ensure_default_account_control_registry_bootstrapped,
    reset_default_account_control_registry_for_tests,
)


@pytest.fixture(autouse=True)
def _seed_account_control_registry() -> None:
    registry = reset_default_account_control_registry_for_tests()
    ensure_default_account_control_registry_bootstrapped()
    registry.freeze()


def test_account_control_override_rejects_legacy_top_level_operation_fields() -> None:
    """账户控制配置不再接受旧顶层 place/cancel/query 字段。"""
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AccountControlOverride.model_validate(
            {
                "timezone": "Asia/Shanghai",
                "place_order": {
                    "per_day": {"limit": 1, "on_trigger": "block"},
                },
            }
        )


def test_account_control_operation_models_accept_shorthand_scope_map() -> None:
    """操作级模型应使用显式 account/symbol 作用域映射。"""
    override = AccountControlOperationOverride.model_validate(
        {
            "account": {
                "per_minute": {"limit": 2},
                "min_interval_ms": {"limit": 300},
            }
        }
    )
    policy = AccountControlOperationPolicy.model_validate(
        {
            "account": {
                "per_day": {"limit": 5, "on_trigger": "block"},
            }
        }
    )

    assert override.account is not None
    assert override.account.per_minute is not None
    assert override.account.per_minute.limit == 2
    assert override.account.per_minute.on_trigger is None
    assert override.account.min_interval_ms is not None
    assert override.account.min_interval_ms.limit == 300
    assert override.account.min_interval_ms.on_trigger is None
    assert policy.account.per_day is not None
    assert policy.account.per_day.limit == 5
    assert policy.account.per_day.on_trigger.value == "block"


def test_ctp_account_control_preset_uses_ctp_specific_limits() -> None:
    """CTP preset 应声明渠道兼容性与专属限流阈值。"""
    preset = get_account_control_preset("ctp")
    policy = resolve_account_control_policy("ctp")

    assert preset.preset_key == "ctp"
    assert preset.compatible_trade_channels == {TradeChannel.CTP}
    assert preset.policy.timezone == "Asia/Shanghai"
    assert preset.policy.operations["query_order"].account.per_minute.limit == 60
    assert preset.policy.operations["query_order"].account.per_day.limit == 2000
    assert preset.policy.operations["query_order"].account.min_interval_ms.limit == 1500
    assert policy.operations["query_order"].priority == 100
    assert preset.policy.operations["place_order"].account.per_minute.limit == 30
    assert preset.policy.operations["place_order"].account.per_day.limit == 500
    assert preset.policy.operations["place_order"].account.min_interval_ms.limit == 500
    assert policy.operations["place_order"].priority == 10
    assert preset.policy.operations["cancel_order"].account.per_minute.limit == 60
    assert preset.policy.operations["cancel_order"].account.per_day.limit == 400
    assert preset.policy.operations["cancel_order"].account.min_interval_ms.limit == 200
    assert policy.operations["cancel_order"].priority == 0
    assert "insert_order" not in policy.operations
    assert policy.operation_groups["place_order"] == {"ctp_td_global"}
    assert policy.operation_groups["query_account"] == {"ctp_td_global"}
    assert policy.operation_groups["query_depth_market_data"] == {"ctp_td_global"}
    assert policy.operations["query_depth_market_data"].account.per_minute.limit == 60
    assert policy.operations["cancel_order_ctp"].priority == 0
    assert preset.policy.groups["ctp_td_global"].min_interval_ms.limit == 1500

    ensure_account_control_preset_compatible("ctp", TradeChannel.CTP)
    with pytest.raises(ValueError, match="不兼容"):
        ensure_account_control_preset_compatible("ctp", TradeChannel("external-demo"))


def test_ctp_option_operation_keeps_account_override() -> None:
    """CTP 期权操作仍允许账户覆盖业务限额。"""
    policy = resolve_account_control_policy(
        "ctp",
        AccountControlOverride.model_validate(
            {
                "operations": {
                    "option_self_close": {
                        "account": {"per_day": {"limit": 10, "on_trigger": "block"}},
                    }
                }
            }
        ),
    )

    assert policy.operations["option_self_close"].account.per_day is not None
    assert policy.operations["option_self_close"].account.per_day.limit == 10


def test_default_query_operation_keeps_account_override() -> None:
    """通用 preset 的查询操作不受 CTP 专属分组影响。"""
    policy = resolve_account_control_policy(
        "default",
        AccountControlOverride.model_validate(
            {
                "operations": {
                    "query_account": {
                        "account": {"per_day": {"limit": 7, "on_trigger": "block"}},
                    }
                }
            }
        ),
    )

    assert policy.operations["query_account"].account.per_day is not None
    assert policy.operations["query_account"].account.per_day.limit == 7
    assert "query_account" not in policy.operation_groups


def test_account_control_override_rejects_invalid_values() -> None:
    """未知 preset、非法时区、未注册 key 和非法规则值都应快速失败。"""
    with pytest.raises(ValueError, match="未知"):
        get_account_control_preset("missing")

    with pytest.raises(ValidationError, match="timezone"):
        AccountControlOverride.model_validate({"timezone": "Mars/Olympus"})

    with pytest.raises(ValidationError, match="未注册的 operation key"):
        AccountControlOverride.model_validate({"operations": {"missing": {"account": {}}}})

    with pytest.raises(ValidationError, match="未注册的 group key"):
        AccountControlOverride.model_validate({"groups": {"missing_group": {}}})

    with pytest.raises(ValidationError, match="greater than or equal to 0"):
        AccountControlOverride.model_validate(
            {
                "operations": {
                    "place_order": {
                        "account": {"per_minute": {"limit": -1, "on_trigger": "wait"}},
                    }
                }
            }
        )

    with pytest.raises(ValidationError, match="min_interval_ms"):
        AccountControlOverride.model_validate(
            {
                "operations": {
                    "place_order": {
                        "account": {"min_interval_ms": {"limit": 0, "on_trigger": "wait"}},
                    }
                }
            }
        )

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AccountControlOverride.model_validate({"symbol_overrides": {}})


def test_account_control_override_rejects_legacy_disable_switch() -> None:
    """账户控制不接受旧版 enabled 开关。"""
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AccountControlOverride.model_validate({"enabled": False})


def test_account_control_rule_override_unlimited_resolves_to_no_rule() -> None:
    """额度类规则允许显式 unlimited，解析后不启用该规则。"""
    policy = resolve_account_control_policy(
        "default",
        AccountControlOverride.model_validate(
            {
                "operations": {
                    "place_order": {
                        "account": {
                            "per_minute": {"unlimited": True},
                            "per_day": {"unlimited": True},
                        }
                    }
                }
            }
        ),
    )

    place_order_account = policy.operations["place_order"].account
    assert place_order_account.per_minute is None
    assert place_order_account.per_day is None
    # 未覆盖的规则保持预设值
    assert place_order_account.min_interval_ms is not None
    assert place_order_account.min_interval_ms.limit == 300


def test_account_control_rule_override_unlimited_rejects_mixed_and_interval() -> None:
    """unlimited 与 limit/on_trigger 互斥，且 min_interval_ms 不允许解除。"""
    with pytest.raises(ValidationError, match="互斥"):
        AccountControlOverride.model_validate(
            {"operations": {"place_order": {"account": {"per_minute": {"limit": 5, "unlimited": True}}}}}
        )

    with pytest.raises(ValidationError, match="不允许设置为不限制"):
        AccountControlOverride.model_validate(
            {"operations": {"place_order": {"account": {"min_interval_ms": {"unlimited": True}}}}}
        )
