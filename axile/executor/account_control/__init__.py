"""
导出账户控制模块的公开接口.

Notes
-----
该模块聚合账户控制相关的模型、注册表、装饰器与 preset 解析能力，
供执行器和上层业务以稳定入口导入。
"""

from typing import TYPE_CHECKING, Any

from axile.executor.account_control.decorators import controlled_operation
from axile.executor.account_control.exceptions import AccountControlBlockedError, AccountControlConfigError
from axile.executor.account_control.models import (
    AccountControlBucketType,
    AccountControlCounterDeltaWrite,
    AccountControlDecision,
    AccountControlEventWrite,
    AccountControlPresetDefinition,
    AccountControlScopeType,
)
from axile.executor.account_control.registry import (
    AccountControlRegistry,
    RegisteredAccountControlOperation,
    RegisteredThrottleGroup,
    ensure_default_account_control_registry_bootstrapped,
    get_default_account_control_registry,
    register_default_registry_bootstrap,
    reset_default_account_control_registry_for_tests,
)
from axile.executor.account_control.snapshot import (
    AccountControlCounterSnapshot,
    AccountControlRecentAllowedSnapshot,
)

if TYPE_CHECKING:
    from axile.executor.account_control.models import (
        AccountControlOverride,
        AccountControlPolicy,
        AccountControlScopeOverride,
        AccountControlScopePolicy,
        EffectiveAccountControlPolicy,
    )
    from axile.executor.account_control.presets import (
        ACCOUNT_CONTROL_PRESETS,
        ensure_account_control_preset_compatible,
        get_account_control_preset,
        resolve_account_control_policy,
    )

__all__ = [
    "ACCOUNT_CONTROL_PRESETS",
    "AccountControlRegistry",
    "AccountControlBucketType",
    "AccountControlCounterDeltaWrite",
    "AccountControlCounterSnapshot",
    "AccountControlBlockedError",
    "AccountControlConfigError",
    "AccountControlDecision",
    "AccountControlEventWrite",
    "AccountControlRecentAllowedSnapshot",
    "AccountControlOverride",
    "AccountControlPolicy",
    "AccountControlPresetDefinition",
    "AccountControlScopeType",
    "AccountControlScopeOverride",
    "AccountControlScopePolicy",
    "EffectiveAccountControlPolicy",
    "RegisteredAccountControlOperation",
    "RegisteredThrottleGroup",
    "controlled_operation",
    "ensure_account_control_preset_compatible",
    "ensure_default_account_control_registry_bootstrapped",
    "get_account_control_preset",
    "get_default_account_control_registry",
    "register_default_registry_bootstrap",
    "reset_default_account_control_registry_for_tests",
    "resolve_account_control_policy",
]

_LAZY_PRESET_EXPORTS = frozenset(
    {
        "ACCOUNT_CONTROL_PRESETS",
        "ensure_account_control_preset_compatible",
        "get_account_control_preset",
        "resolve_account_control_policy",
    }
)

_LAZY_MODEL_EXPORTS = frozenset(
    {
        "AccountControlOverride",
        "AccountControlPolicy",
        "AccountControlScopeOverride",
        "AccountControlScopePolicy",
        "EffectiveAccountControlPolicy",
    }
)


def __getattr__(name: str) -> Any:
    if name in _LAZY_MODEL_EXPORTS:
        from axile.executor.account_control import models

        return getattr(models, name)
    if name in _LAZY_PRESET_EXPORTS:
        from axile.executor.account_control import presets as _presets

        return getattr(_presets, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
