"""算法注册表相关测试。"""

from typing import Any

from axile.executor.algorithms.core.base import get_algorithm_metadata, register_algorithm


def _noop_algorithm(executor: Any, algorithm_input: Any) -> Any:
    """占位算法实现，仅用于注册元数据测试。"""
    return None


def test_slots_explicit_values_are_frozen() -> None:
    """显式声明的槽位应转成 frozenset 保存。"""
    register_algorithm("TEST-SLOTS-EMPTY-ONLY", slots=["empty"])(_noop_algorithm)
    meta = get_algorithm_metadata("TEST-SLOTS-EMPTY-ONLY")
    assert meta.slots == frozenset({"empty"})
