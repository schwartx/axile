"""账户控制注册表测试。"""

from __future__ import annotations

import pytest

from axile.executor.account_control.registry import (
    AccountControlRegistry,
)


def test_register_operation_allows_identical_reuse_but_rejects_conflicts() -> None:
    registry = AccountControlRegistry()
    first = registry.register_operation(
        "place_order",
        groups={"trade_global"},
    )
    second = registry.register_operation(
        "place_order",
        groups={"trade_global"},
    )

    assert first is second
    assert first.groups == frozenset({"trade_global"})

    with pytest.raises(ValueError, match="conflict"):
        registry.register_operation(
            "place_order",
            groups={"other_group"},
        )
