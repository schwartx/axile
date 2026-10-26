"""
提供账户控制操作与共享节流组的注册表.

Notes
-----
默认注册表会在启动期累计各模块声明的 operation 与 group，
并在执行前冻结，避免运行时继续修改定义。
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from threading import RLock

type RegistryBootstrap = Callable[[], None]


def _normalize_registry_key(value: str, *, label: str) -> str:
    key = value.strip()
    if not key:
        raise ValueError(f"{label} 不能为空")
    return key


@dataclass(frozen=True)
class RegisteredThrottleGroup:
    """
    描述共享节流组的注册定义.

    Attributes
    ----------
    key : str
        节流组唯一键。
    """

    key: str


@dataclass(frozen=True)
class RegisteredAccountControlOperation:
    """
    描述账户控制操作的注册定义.

    Attributes
    ----------
    key : str
        操作唯一键。
    groups : frozenset[str]
        当前操作所属的共享节流组集合。
    """

    key: str
    groups: frozenset[str] = field(default_factory=frozenset)


class AccountControlRegistry:
    """
    管理账户控制定义的可冻结注册表.

    Notes
    -----
    注册表在冻结前允许追加 operation 与 group；冻结后仅允许查询。
    """

    def __init__(self) -> None:
        self._operations: dict[str, RegisteredAccountControlOperation] = {}
        self._groups: dict[str, RegisteredThrottleGroup] = {}
        self._frozen = False
        self._lock = RLock()

    @property
    def is_frozen(self) -> bool:
        """
        返回注册表是否已冻结.

        Returns
        -------
        bool
            若注册表已禁止继续写入则返回 ``True``。
        """
        return self._frozen

    @property
    def operations(self) -> Mapping[str, RegisteredAccountControlOperation]:
        """
        返回已注册的操作映射.

        Returns
        -------
        Mapping[str, RegisteredAccountControlOperation]
            以操作键为索引的只读视图。
        """
        return self._operations

    @property
    def groups(self) -> Mapping[str, RegisteredThrottleGroup]:
        """
        返回已注册的共享节流组映射.

        Returns
        -------
        Mapping[str, RegisteredThrottleGroup]
            以组键为索引的只读视图。
        """
        return self._groups

    def freeze(self) -> None:
        """冻结注册表，禁止后续写入."""
        with self._lock:
            self._frozen = True

    def register_operation(
        self,
        key: str,
        *,
        groups: Iterable[str] = (),
    ) -> RegisteredAccountControlOperation:
        """
        注册账户控制操作定义.

        Parameters
        ----------
        key : str
            操作唯一键。
        groups : Iterable[str], optional
            当前操作所属的共享节流组键集合。

        Returns
        -------
        RegisteredAccountControlOperation
            新建或已存在的操作定义。

        Raises
        ------
        RuntimeError
            注册表已冻结时抛出。
        ValueError
            操作键或组键为空，或与已存在定义冲突时抛出。
        """
        with self._lock:
            self._assert_mutable()
            normalized_key = _normalize_registry_key(key, label="operation key")
            operation = RegisteredAccountControlOperation(
                key=normalized_key,
                groups=frozenset(_normalize_registry_key(group, label="group key") for group in groups),
            )
            existing = self._operations.get(normalized_key)
            if existing is None:
                self._operations[normalized_key] = operation
                self._sync_native()
                return operation
            if existing != operation:
                raise ValueError(f"operation key `{normalized_key}` definition conflict")
            return existing

    def register_group(
        self,
        key: str,
    ) -> RegisteredThrottleGroup:
        """
        注册共享节流组定义.

        Parameters
        ----------
        key : str
            节流组唯一键。

        Returns
        -------
        RegisteredThrottleGroup
            新建或已存在的节流组定义。

        Raises
        ------
        RuntimeError
            注册表已冻结时抛出。
        ValueError
            组键为空或与已存在定义冲突时抛出。
        """
        with self._lock:
            self._assert_mutable()
            normalized_key = _normalize_registry_key(key, label="group key")
            group = RegisteredThrottleGroup(key=normalized_key)
            existing = self._groups.get(normalized_key)
            if existing is None:
                self._groups[normalized_key] = group
                self._sync_native()
                return group
            if existing != group:
                raise ValueError(f"group key `{normalized_key}` definition conflict")
            return existing

    def get_operation(self, key: str) -> RegisteredAccountControlOperation | None:
        """
        根据键查询操作定义.

        Parameters
        ----------
        key : str
            操作唯一键。

        Returns
        -------
        RegisteredAccountControlOperation | None
            命中的操作定义；未命中时返回 ``None``。
        """
        return self._operations.get(key)

    def get_group(self, key: str) -> RegisteredThrottleGroup | None:
        """
        根据键查询节流组定义.

        Parameters
        ----------
        key : str
            节流组唯一键。

        Returns
        -------
        RegisteredThrottleGroup | None
            命中的节流组定义；未命中时返回 ``None``。
        """
        return self._groups.get(key)

    def require_operation(self, key: str) -> RegisteredAccountControlOperation:
        """
        获取指定操作定义，未注册时抛出异常.

        Parameters
        ----------
        key : str
            操作唯一键。

        Returns
        -------
        RegisteredAccountControlOperation
            已注册的操作定义。

        Raises
        ------
        ValueError
            未找到对应操作键时抛出。
        """
        operation = self.get_operation(key)
        if operation is None:
            raise ValueError(f"未注册的 operation key: {key}")
        return operation

    def require_group(self, key: str) -> RegisteredThrottleGroup:
        """
        获取指定节流组定义，未注册时抛出异常.

        Parameters
        ----------
        key : str
            节流组唯一键。

        Returns
        -------
        RegisteredThrottleGroup
            已注册的节流组定义。

        Raises
        ------
        ValueError
            未找到对应组键时抛出。
        """
        group = self.get_group(key)
        if group is None:
            raise ValueError(f"未注册的 group key: {key}")
        return group

    def _sync_native(self) -> None:
        """仅发布默认注册表的声明，独立测试注册表不影响运行时。"""
        native = sys.modules.get("axile.server._native._core")
        if self is _DEFAULT_REGISTRY and native is not None:
            native.control_registry(list(self._operations), list(self._groups))

    def _assert_mutable(self) -> None:
        if self._frozen:
            raise RuntimeError("account control registry is frozen")


_DEFAULT_REGISTRY = AccountControlRegistry()
_DEFAULT_BOOTSTRAP_CALLBACKS: list[RegistryBootstrap] = []
_DEFAULT_BOOTSTRAPPED = False
_DEFAULT_BOOTSTRAP_LOCK = RLock()


def get_default_account_control_registry() -> AccountControlRegistry:
    """
    返回默认账户控制注册表.

    Returns
    -------
    AccountControlRegistry
        进程级共享的默认注册表实例。
    """
    return _DEFAULT_REGISTRY


def register_default_registry_bootstrap(callback: RegistryBootstrap) -> None:
    """
    注册默认注册表的延迟引导回调.

    Parameters
    ----------
    callback : RegistryBootstrap
        在默认注册表首次引导时执行的回调函数。
    """
    with _DEFAULT_BOOTSTRAP_LOCK:
        _DEFAULT_BOOTSTRAP_CALLBACKS.append(callback)


def ensure_default_account_control_registry_bootstrapped() -> AccountControlRegistry:
    """
    确保默认注册表完成引导并返回其实例.

    Returns
    -------
    AccountControlRegistry
        已执行全部 bootstrap 回调后的默认注册表实例。
    """
    global _DEFAULT_BOOTSTRAPPED
    if _DEFAULT_BOOTSTRAPPED:
        return _DEFAULT_REGISTRY

    with _DEFAULT_BOOTSTRAP_LOCK:
        if _DEFAULT_BOOTSTRAPPED:
            return _DEFAULT_REGISTRY
        for callback in list(_DEFAULT_BOOTSTRAP_CALLBACKS):
            callback()
        _DEFAULT_BOOTSTRAPPED = True
    return _DEFAULT_REGISTRY


def reset_default_account_control_registry_for_tests() -> AccountControlRegistry:
    """
    重置默认注册表，供测试场景重新引导.

    Returns
    -------
    AccountControlRegistry
        重置后的默认注册表实例。
    """
    global _DEFAULT_REGISTRY
    global _DEFAULT_BOOTSTRAPPED

    with _DEFAULT_BOOTSTRAP_LOCK:
        _DEFAULT_REGISTRY = AccountControlRegistry()
        _DEFAULT_REGISTRY._sync_native()
        _DEFAULT_BOOTSTRAPPED = False
    return _DEFAULT_REGISTRY
