"""定义多进程 worker 与主进程之间的最小协议边界.

该模块只保留跨进程通信真正需要的稳定字段，避免把执行器实例、
领域模型或不可序列化的运行时对象泄漏到 `multiprocessing`
 边界之外。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from axile.common.trade_channel import TradeChannel
from axile.executor.termination import TERMINATION_TRIGGER_OPERATOR

WorkerBackendCommand = Literal[
    "prepare",
    "get_account_assets",
    "calculate_portfolio",
    "execute_trade",
    "empty_positions",
    "disconnect",
    "shutdown",
]
WorkerBackendResponseKind = Literal["result", "error", "terminated"]


@dataclass(slots=True)
class WorkerBackendErrorPayload:
    """描述多进程 worker 的结构化错误载荷.

    Attributes
    ----------
    type : str
        错误类型标识，使用 snake_case。
    message : str
        面向主进程和审计的错误摘要。
    retryable : bool
        是否建议上层视为可重试错误，不代表预期的柜台等待状态。
    reason_code : str | None
        渠道在错误发生处确定的稳定原因码，不从 message 推断。
    error_id : int | None
        渠道异步响应错误码，与同步请求返回码区分。
    technical_detail : str | None
        异常原文；旧 worker 载荷可不提供。
    traceback : str | None
        异常发生处的完整调用栈。
    """

    type: str
    message: str
    retryable: bool = False
    reason_code: str | None = None
    error_id: int | None = None
    technical_detail: str | None = None
    traceback: str | None = None
    error_scope: str | None = None
    diagnostics: list[dict[str, object]] = field(default_factory=list)
    exception_type: str | None = None
    retry_after: int = 0


@dataclass(slots=True)
class WorkerTerminationSignal:
    """主进程通过独立控制管道发送的 execution 终止信号."""

    execution_id: str
    reason: str | None
    mode: str | None


@dataclass(slots=True)
class WorkerBackendRequest:
    """描述主进程发送给多进程 worker 的请求载荷.

    Notes
    -----
    该对象是跨进程协议载体，不承诺承载强类型领域对象。worker
    收到请求后会在本地把 `account_payload` 和 `payload` 再解析
    为更强的运行时上下文。

    Attributes
    ----------
    request_id : str
        请求唯一标识。
    command : WorkerBackendCommand
        worker 需要执行的命令。
    account_payload : dict[str, object]
        账户快照；关闭命令下允许为空。
    execution_id : str | None
        对应的 execution 标识。
    payload : dict[str, object]
        命令专属请求参数。
    """

    request_id: str
    command: WorkerBackendCommand
    account_payload: dict[str, object]
    execution_id: str | None
    payload: dict[str, object]
    connection_binding: dict[str, object] | None = None

    @classmethod
    def shutdown(cls, request_id: str, reason: str = "manager_shutdown") -> "WorkerBackendRequest":
        """构造 worker 显式关闭请求.

        Parameters
        ----------
        request_id : str
            请求唯一标识。
        reason : str, default="manager_shutdown"
            关闭原因。

        Returns
        -------
        WorkerBackendRequest
            关闭命令请求对象。
        """
        return cls(
            request_id=request_id,
            command="shutdown",
            account_payload={},
            execution_id=None,
            payload={"reason": reason},
        )


@dataclass(slots=True)
class WorkerBackendResponse:
    """描述多进程 worker 返回给主进程的响应载荷.

    Notes
    -----
    响应字段需要同时服务于主进程状态机恢复和执行审计回放，因此
    即使失败或终止，也会显式携带 `channel_type`、`reason` 等
    上层继续编排所需的最小上下文。

    Attributes
    ----------
    request_id : str
        对应请求标识。
    kind : WorkerBackendResponseKind
        响应类型。
    output_payload : dict[str, object] | None
        成功执行时的输出载荷，关闭命令也复用该字段返回确认信息。
    error : WorkerBackendErrorPayload | None
        结构化错误信息。
    channel_type : TradeChannel | None
        当前响应所属的交易渠道；用于主进程在失败场景恢复输出语义。
    reason : str | None
        终止原因。
    mode : str | None
        终止模式。
    acked_at : str | None
        终止确认时间。
    trigger : str
        终止来源：``operator``（人工请求）或 ``timeout``（执行层总超时）；
        与 ``mode`` 正交。``ExecutionTerminated`` 侧同样恒有取值，故这里不用可空。
    cancel_failed_order_ids : list[str]
        终止时撤单失败的订单标识。
    normalized_symbol_fields : dict[str, object] | None
        worker 按渠道目录归一化后的 symbol 字段；不包含账户凭据。
    requires_worker_restart : bool
        通信或资源清理失败后回收当前 worker；与原始业务错误独立，不重放当前请求。
    """

    request_id: str
    kind: WorkerBackendResponseKind
    output_payload: dict[str, object] | None = None
    error: WorkerBackendErrorPayload | None = None
    channel_type: TradeChannel | None = None
    reason: str | None = None
    mode: str | None = None
    acked_at: str | None = None
    trigger: str = TERMINATION_TRIGGER_OPERATOR
    cancel_failed_order_ids: list[str] = field(default_factory=list)
    forced: bool = False
    cancel_attempted: bool | None = None
    cancel_unconfirmed: bool = False
    normalized_symbol_fields: dict[str, object] | None = None
    requires_worker_restart: bool = False
    connection_snapshot: dict[str, object] | None = None
