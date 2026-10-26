"""账户控制配置模型测试。"""

from __future__ import annotations

import pytest

from axile.common.trade_channel import TradeChannel
from axile.executor.account_control.exceptions import AccountControlConfigError
from axile.executor.account_control.models import (
    AccountControlOperationOverride,
    AccountControlOperationPolicy,
    AccountControlOverride,
    AccountControlPresetDefinition,
)
from axile.executor.account_control.presets import (
    ACCOUNT_CONTROL_PRESETS,
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
    with pytest.raises(AccountControlConfigError, match="unknown field"):
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
    with pytest.raises(AccountControlConfigError, match="不兼容"):
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
    with pytest.raises(AccountControlConfigError, match="未知"):
        get_account_control_preset("missing")

    with pytest.raises(AccountControlConfigError, match="timezone"):
        AccountControlOverride.model_validate({"timezone": "Mars/Olympus"})

    with pytest.raises(AccountControlConfigError, match="未注册的 operation key"):
        AccountControlOverride.model_validate({"operations": {"missing": {"account": {}}}})

    with pytest.raises(AccountControlConfigError, match="未注册的 group key"):
        AccountControlOverride.model_validate({"groups": {"missing_group": {}}})

    with pytest.raises(AccountControlConfigError, match="greater than or equal to 0"):
        AccountControlOverride.model_validate(
            {
                "operations": {
                    "place_order": {
                        "account": {"per_minute": {"limit": -1, "on_trigger": "wait"}},
                    }
                }
            }
        )

    with pytest.raises(AccountControlConfigError, match="min_interval_ms"):
        AccountControlOverride.model_validate(
            {
                "operations": {
                    "place_order": {
                        "account": {"min_interval_ms": {"limit": 0, "on_trigger": "wait"}},
                    }
                }
            }
        )

    with pytest.raises(AccountControlConfigError, match="unknown field"):
        AccountControlOverride.model_validate({"symbol_overrides": {}})


def test_account_control_override_rejects_legacy_disable_switch() -> None:
    """账户控制不接受旧版 enabled 开关。"""
    with pytest.raises(AccountControlConfigError, match="unknown field"):
        AccountControlOverride.model_validate({"enabled": False})


@pytest.mark.parametrize(
    "value",
    [
        [],
        {"operations": {"place_order": []}},
        {"operations": {"place_order": {"account": []}}},
        {"operations": {"place_order": {"account": {"per_day": []}}}},
        {"groups": {"ctp_td_global": []}},
        {"operations": {"place_order": {"priority": True}}},
        {"operations": {"place_order": {"priority": 1.0}}},
        {"operations": {"place_order": {"priority": "1"}}},
        {"operations": {1: {}}},
        {"operations": {"place_order": {"account": {"per_day": {"limit": True}}}}},
        {"operations": {"place_order": {"account": {"per_day": {"unlimited": 1}}}}},
        {"timezone": 123},
    ],
)
def test_native_configuration_keeps_strict_json_types(value: object) -> None:
    """迁移到 Rust 后仍拒绝数组对象、隐式数值转换和非字符串键。"""
    with pytest.raises(AccountControlConfigError):
        AccountControlOverride.model_validate(value)


def test_native_configuration_rejects_circular_input() -> None:
    value = {}
    value["operations"] = value
    with pytest.raises(AccountControlConfigError, match="循环引用"):
        AccountControlOverride.model_validate(value)


@pytest.mark.parametrize(
    ("value", "code", "path"),
    [
        (object(), "CONTROL_CONFIG_INVALID_TYPE", []),
        ({"timezone": object()}, "CONTROL_CONFIG_INVALID_TYPE", ["timezone"]),
        (
            {"operations": {"place_order": {"priority": 2**100}}},
            "CONTROL_CONFIG_OUT_OF_RANGE",
            ["operations", "place_order", "priority"],
        ),
        (
            {"operations": {"place_order": {"account": {"per_day": {"limit": -1}}}}},
            "CONTROL_CONFIG_OUT_OF_RANGE",
            ["operations", "place_order", "account", "per_day", "limit"],
        ),
        (
            {"operations": {"place_order": {"account": {"per_day": {"limit": "7"}}}}},
            "CONTROL_CONFIG_INVALID_STRUCTURE",
            ["operations", "place_order", "account", "per_day", "limit"],
        ),
        (
            {"operations": {"place_order": {"account": []}}},
            "CONTROL_CONFIG_INVALID_STRUCTURE",
            ["operations", "place_order", "account"],
        ),
        ({"operations": {"unknown": {}}}, "CONTROL_CONFIG_UNKNOWN_OPERATION", ["operations", "unknown"]),
        ({"timezone": "\ud800"}, "CONTROL_CONFIG_INVALID_TYPE", ["timezone"]),
        ({"operations": {"\udfff": {}}}, "CONTROL_CONFIG_INVALID_TYPE", ["operations"]),
    ],
)
def test_native_configuration_preserves_error_contract(value, code, path) -> None:
    for parse in (
        AccountControlOverride.model_validate,
        lambda value: resolve_account_control_policy("default", value),
    ):
        with pytest.raises(AccountControlConfigError) as caught:
            parse(value)
        error = caught.value
        assert error.code == code
        assert error.path == path
        assert error.details == {"code": code, "path": path, "message": str(error)}
        assert error.message


def test_native_configuration_does_not_call_arbitrary_model_dump() -> None:
    class Unsupported:
        def model_dump(self):
            raise AssertionError("不应调用用户方法")

    with pytest.raises(AccountControlConfigError) as caught:
        AccountControlOverride.model_validate(Unsupported())
    assert caught.value.code == "CONTROL_CONFIG_INVALID_TYPE"


@pytest.mark.parametrize("effective", [False, True], ids=["override", "effective-policy"])
def test_model_copy_validates_updates_without_mutating_source(effective: bool) -> None:
    source = AccountControlOverride(timezone="Asia/Shanghai")
    if effective:
        source = resolve_account_control_policy("default", source)
    before = source.model_dump(mode="json")
    update = {
        "timezone": "UTC",
        "operations": {"place_order": {"account": {"per_day": {"limit": 7, "on_trigger": "block"}}}},
    }
    copied = source.model_copy(update=update)
    assert copied.timezone == "UTC"
    assert copied.operations["place_order"].account.per_day.limit == 7

    update["operations"]["place_order"]["account"]["per_day"]["limit"] = -1
    with pytest.raises(AccountControlConfigError) as caught:
        source.model_copy(update=update)
    assert caught.value.code == "CONTROL_CONFIG_OUT_OF_RANGE"
    assert caught.value.path == ["operations", "place_order", "account", "per_day", "limit"]
    assert caught.value.message
    assert source.model_dump(mode="json") == before
    assert copied.operations["place_order"].account.per_day.limit == 7


def test_native_public_override_has_explicit_defaults() -> None:
    """公开契约独立断言默认字段，避免以同一实现互相验证。"""
    assert AccountControlOverride.model_validate({}).model_dump(mode="json") == {
        "timezone": None,
        "operations": {},
        "groups": {},
    }
    operation = AccountControlOperationOverride.model_validate({"account": {"per_day": {"limit": 0}}})
    assert operation.model_dump(mode="json") == {
        "priority": None,
        "daily_warning_percent": None,
        "account": {
            "per_minute": None,
            "per_day": {"limit": 0, "on_trigger": None, "unlimited": False},
            "min_interval_ms": None,
        },
        "symbol": None,
    }


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
    with pytest.raises(AccountControlConfigError, match="互斥"):
        AccountControlOverride.model_validate(
            {"operations": {"place_order": {"account": {"per_minute": {"limit": 5, "unlimited": True}}}}}
        )

    with pytest.raises(AccountControlConfigError, match="不允许设置为不限制"):
        AccountControlOverride.model_validate(
            {"operations": {"place_order": {"account": {"min_interval_ms": {"unlimited": True}}}}}
        )


@pytest.mark.parametrize(
    ("preset_rule", "override_rule", "expected"),
    [
        (None, {"on_trigger": "block"}, None),
        ({"on_trigger": "block"}, {"limit": 7}, {"limit": 7, "on_trigger": "wait"}),
        ({"limit": 5, "on_trigger": "block"}, None, {"limit": 5, "on_trigger": "block"}),
        ({"limit": 5, "on_trigger": "block"}, {"limit": 0}, {"limit": 0, "on_trigger": "block"}),
        ({"limit": 5, "on_trigger": "block"}, {"on_trigger": "wait"}, {"limit": 5, "on_trigger": "wait"}),
        ({"limit": 5, "on_trigger": "block"}, {"unlimited": True}, None),
        ({"unlimited": True}, {"on_trigger": "block"}, None),
        ({"unlimited": True}, {"limit": 7}, {"limit": 7, "on_trigger": "wait"}),
    ],
)
def test_policy_resolves_partial_rules_without_reviving_cleared_values(
    monkeypatch: pytest.MonkeyPatch, preset_rule: dict | None, override_rule: dict | None, expected: dict | None
) -> None:
    """无额度时不保留触发行为；解除后重新设限应从默认行为开始。"""
    monkeypatch.setitem(
        ACCOUNT_CONTROL_PRESETS,
        "partial-rules",
        AccountControlPresetDefinition(
            preset_key="partial-rules",
            compatible_trade_channels=None,
            policy=AccountControlOverride.model_validate(
                {"operations": {"place_order": {"account": {"per_day": preset_rule}}}}
            ),
        ),
    )
    policy = resolve_account_control_policy(
        "partial-rules",
        AccountControlOverride.model_validate({"operations": {"place_order": {"account": {"per_day": override_rule}}}}),
    )

    rule = policy.operations["place_order"].account.per_day
    assert (None if rule is None else rule.model_dump(mode="json")) == expected


@pytest.mark.parametrize("operation_override", [None, {}, {"priority": 0}, {"daily_warning_percent": 80}])
def test_policy_keeps_operation_presence_semantics_for_warning(
    monkeypatch: pytest.MonkeyPatch, operation_override: dict | None
) -> None:
    """覆盖操作时缺省预警表示关闭；没有覆盖操作时保留 preset 预警。"""
    monkeypatch.setitem(
        ACCOUNT_CONTROL_PRESETS,
        "warning",
        AccountControlPresetDefinition(
            preset_key="warning",
            compatible_trade_channels=None,
            policy=AccountControlOverride.model_validate(
                {"operations": {"place_order": {"priority": 10, "daily_warning_percent": 70}}}
            ),
        ),
    )
    override = AccountControlOverride.model_validate(
        {"operations": {} if operation_override is None else {"place_order": operation_override}}
    )
    operation = resolve_account_control_policy("warning", override).operations["place_order"]

    assert operation.priority == (0 if operation_override == {"priority": 0} else 10)
    assert operation.daily_warning_percent == (
        70 if operation_override is None else operation_override.get("daily_warning_percent")
    )


def test_policy_snapshots_own_their_rules_and_operation_groups(monkeypatch: pytest.MonkeyPatch) -> None:
    """原生只读快照隔离输入配置，也不暴露内部可变映射。"""
    from dataclasses import replace

    preset = get_account_control_preset("ctp")
    preset_groups = dict(preset.operation_groups)
    monkeypatch.setitem(ACCOUNT_CONTROL_PRESETS, "ctp", replace(preset, operation_groups=preset_groups))
    override = {
        "timezone": "UTC",
        "operations": {"place_order": {"account": {"per_day": {"limit": 7}}}},
        "groups": {"ctp_td_global": {"per_minute": {"limit": 8}}},
    }
    first = resolve_account_control_policy("ctp", override)
    second = resolve_account_control_policy("ctp", override)
    expected = first.model_dump(mode="json")
    override["operations"]["place_order"]["account"]["per_day"]["limit"] = 9
    preset_groups["place_order"] = frozenset()
    exposed = first.operations
    exposed.clear()
    first.operation_groups.clear()
    with pytest.raises(AttributeError):
        first.operations["place_order"].account.per_day.limit = 99
    assert first.model_dump(mode="json") == second.model_dump(mode="json") == expected
    assert first.model_copy(deep=True).model_dump(mode="json") == expected
