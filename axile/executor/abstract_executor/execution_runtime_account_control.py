"""`AbstractExecutor` 的 execution runtime 账户控制 facade."""

from __future__ import annotations

from typing import TYPE_CHECKING

from axile.executor.abstract_executor.execution_runtime_host import _executor
from axile.executor.account_control.models import (
    AccountControlCounterDeltaWrite,
    AccountControlEventWrite,
)
from axile.executor.account_control.scope import scoped_guard

if TYPE_CHECKING:
    from axile.executor.account_control.guard import AccountControlGuard


class AbstractExecutorExecutionAccountControlFacadeMixin:
    """
    承载 execution runtime 与账户控制 guard 相关的 facade。

    Notes
    -----
    该 mixin 只处理 guard 绑定与账户控制记录导出，不承载审计或
    runtime 宿主初始化逻辑。
    """

    def get_account_control_guard(self) -> AccountControlGuard | None:
        """
        返回当前 execution 绑定的账户控制 guard。

        Returns
        -------
        AccountControlGuard | None
            当前执行绑定的账户控制 guard；不存在时返回 ``None``。
        """
        executor = _executor(self)
        local = scoped_guard(executor)
        if local is not None:
            return local
        runtime = executor.get_active_execution_runtime()
        if runtime is not None:
            return runtime.account_control_guard
        return executor._runtime_bindings.account_control_guard

    def set_account_control_guard(self, guard: AccountControlGuard | None) -> None:
        """
        绑定本次执行的账户控制防护层。

        Parameters
        ----------
        guard : AccountControlGuard | None
            账户控制防护层；传入 ``None`` 表示解除绑定。
        """
        _executor(self)._update_runtime_binding("account_control_guard", guard)
        # guard 也需要感知 terminate checkpoint，否则等待/节流中的操作无法协作式退出。
        set_termination_checkpoint = getattr(guard, "set_termination_checkpoint", None)
        if callable(set_termination_checkpoint):
            set_termination_checkpoint(_executor(self).handle_termination_checkpoint)

    def export_account_control_records(
        self,
    ) -> tuple[list[AccountControlCounterDeltaWrite], list[AccountControlEventWrite]]:
        """
        导出当前 execution 的账户控制防护层持久化记录。

        Returns
        -------
        tuple[list[AccountControlCounterDeltaWrite], list[AccountControlEventWrite]]
            账户计数器增量记录与账户控制事件记录。
        """
        executor = _executor(self)
        runtime = executor.get_active_execution_runtime()
        if runtime is not None:
            # 活跃执行期间优先从 runtime 导出，避免漏掉尚未 flush 回 guard 的请求内记录。
            return runtime.export_account_control_records()
        guard = executor.get_account_control_guard()
        if guard is None:
            return [], []
        # 非执行态下 guard 自己就是最后一份暂存区。
        return guard.flush_records()
