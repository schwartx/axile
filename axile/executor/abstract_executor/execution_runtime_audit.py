"""`AbstractExecutor` 的 execution runtime 审计 facade."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from axile.executor.abstract_executor.execution_runtime_host import _executor
from axile.executor.abstract_executor.support import _coerce_int
from axile.executor.audit import ExecutionAuditSink

if TYPE_CHECKING:
    from axile.executor.abstract_executor.base import AuditContext, OrderAuditMetadata


class AbstractExecutorExecutionAuditFacadeMixin:
    """
    承载 execution runtime 与审计上下文相关的 facade。

    Notes
    -----
    该 mixin 负责审计上下文、审计 sink、订单审计元数据，以及事件/
    附件写入入口。
    """

    @property
    def audit_context(self) -> AuditContext:
        """
        返回当前 executor 持有的审计上下文。

        Returns
        -------
        AuditContext
            当前执行绑定的审计上下文字典。
        """
        executor = _executor(self)
        runtime = executor.get_active_execution_runtime()
        if runtime is not None:
            return runtime.audit_context
        return executor._runtime_bindings.audit_context

    @audit_context.setter
    def audit_context(self, value: Mapping[str, object]) -> None:
        _executor(self)._update_runtime_binding(
            "audit_context",
            {str(key): item for key, item in value.items()},
        )

    def set_audit_context(self, context: AuditContext) -> None:
        """
        设置本次执行的审计上下文。

        Parameters
        ----------
        context : AuditContext
            需要写入当前执行绑定的审计上下文。
        """
        _executor(self)._update_runtime_binding("audit_context", dict(context))

    def set_audit_sink(self, sink: ExecutionAuditSink | None) -> None:
        """
        设置本次执行使用的审计输出 sink。

        Parameters
        ----------
        sink : ExecutionAuditSink | None
            审计输出 sink；传入 ``None`` 表示当前执行不输出审计。
        """
        _executor(self)._update_runtime_binding("audit_sink", sink)

    def next_audit_seq(self) -> int:
        """
        获取本次执行内单调递增的审计序号。

        Returns
        -------
        int
            下一条审计记录应使用的序号。
        """
        return _executor(self).require_execution_runtime().next_audit_seq()

    def register_order_audit_metadata(self, order_id: str, metadata: OrderAuditMetadata) -> None:
        """
        为订单注册审计关联元数据。

        Parameters
        ----------
        order_id : str
            订单标识。
        metadata : OrderAuditMetadata
            需要与订单关联的审计元数据。
        """
        executor = _executor(self)
        executor.account_orders.remember_metadata(order_id, metadata)
        executor.require_execution_runtime().register_order_audit_metadata(order_id, metadata)

    def get_order_audit_metadata(self, order_id: str) -> OrderAuditMetadata:
        """
        获取订单的审计关联元数据。

        Parameters
        ----------
        order_id : str
            订单标识。

        Returns
        -------
        OrderAuditMetadata
            与订单关联的审计元数据字典。
        """
        return _executor(self).account_orders.metadata(order_id)

    def emit_audit_event(
        self,
        *,
        event_type: object,
        status: object,
        reason_family: object,
        reason_code: str,
        symbol: str | None = None,
        intent_id: str | None = None,
        order_id: str | None = None,
        client_order_id: str | None = None,
        ts_exchange: str | None = None,
        seq: int | None = None,
        details: dict[str, object] | None = None,
        audit_context: AuditContext | None = None,
    ) -> bool:
        """
        写入执行审计事件。

        Parameters
        ----------
        event_type : object
            审计事件类型。
        status : object
            审计事件状态。
        reason_family : object
            审计原因分类。
        reason_code : str
            审计原因代码。
        symbol : str | None, default=None
            关联的品种代码。
        intent_id : str | None, default=None
            关联的意图标识。
        order_id : str | None, default=None
            关联的订单标识。
        client_order_id : str | None, default=None
            客户端订单标识。
        ts_exchange : str | None, default=None
            交易所侧时间戳。
        seq : int | None, default=None
            审计序号。
        details : dict[str, object] | None, default=None
            事件附加详情。
        audit_context : AuditContext | None, default=None
            显式传入的审计上下文；为空时回退到当前绑定上下文。

        Returns
        -------
        bool
            成功写入审计事件时返回 ``True``；上下文不足时返回 ``False``。
        """
        executor = _executor(self)
        runtime = executor.get_active_execution_runtime()
        if runtime is not None:
            return runtime.emit_audit_event(
                event_type=event_type,
                status=status,
                reason_family=reason_family,
                reason_code=reason_code,
                symbol=symbol,
                intent_id=intent_id,
                order_id=order_id,
                client_order_id=client_order_id,
                ts_exchange=ts_exchange,
                seq=seq,
                details=details,
                audit_context=audit_context,
            )
        # 预执行阶段常常只有 binding 上的 audit_context，此时允许惰性补建 runtime 落审计。
        context = audit_context or executor.audit_context
        execution_id = context.get("execution_id")
        account_id = _coerce_int(context.get("account_id"))
        algorithm = context.get("algorithm")
        if not execution_id or account_id is None or not algorithm or executor._runtime_bindings.audit_sink is None:
            return False
        return executor.require_execution_runtime().emit_audit_event(
            event_type=event_type,
            status=status,
            reason_family=reason_family,
            reason_code=reason_code,
            symbol=symbol,
            intent_id=intent_id,
            order_id=order_id,
            client_order_id=client_order_id,
            ts_exchange=ts_exchange,
            seq=seq,
            details=details,
            audit_context=context,
        )

    def emit_audit_artifact(
        self,
        *,
        artifact_type: object,
        content: dict[str, object],
        audit_context: AuditContext | None = None,
    ) -> bool:
        """
        写入执行审计附件。

        Parameters
        ----------
        artifact_type : object
            审计附件类型。
        content : dict[str, object]
            审计附件内容。
        audit_context : AuditContext | None, default=None
            显式传入的审计上下文；为空时回退到当前绑定上下文。

        Returns
        -------
        bool
            成功写入审计附件时返回 ``True``；上下文不足时返回 ``False``。
        """
        executor = _executor(self)
        runtime = executor.get_active_execution_runtime()
        if runtime is not None:
            return runtime.emit_audit_artifact(
                artifact_type=artifact_type,
                content=content,
                audit_context=audit_context,
            )
        # artifact 和 event 一样，允许在 active runtime 尚未创建时靠 audit_context 落审计。
        execution_id = (audit_context or executor.audit_context).get("execution_id")
        if not execution_id:
            return False
        return executor.require_execution_runtime().emit_audit_artifact(
            artifact_type=artifact_type,
            content=content,
            audit_context=audit_context,
        )
