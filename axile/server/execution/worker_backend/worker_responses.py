"""规范化 worker 成功、失败与终止响应.

该模块负责把执行器返回值和异常对象收敛为跨进程协议字段，确保
主进程只依赖稳定、可序列化的响应形状。
"""

from __future__ import annotations

import re
import traceback
from collections.abc import Callable
from typing import cast

from loguru import logger

from axile.executor.connection_state import connection_error_scope
from axile.executor.models.unified_output import UnifiedStandardOutput
from axile.executor.termination import ExecutionTerminated
from axile.server.db.models.account import AccountContext
from axile.server.error_details import json_error_value
from axile.server.execution.worker_backend.protocol import (
    WorkerBackendErrorPayload,
    WorkerBackendRequest,
    WorkerBackendResponse,
)


def _camel_to_snake(name: str) -> str:
    """将异常类名转换为协议使用的 snake_case 标识."""
    normalized = re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
    return normalized or "runtime_error"


def _build_error_payload(exc: Exception) -> WorkerBackendErrorPayload:
    """
    构造 worker 结构化错误载荷。

    Parameters
    ----------
    exc : Exception
        当前捕获的异常对象。

    Returns
    -------
    WorkerBackendErrorPayload
        结构化错误信息。
    """
    # 公开摘要保持渠道语义；原文和 traceback 通过可选字段完整传输。
    explicit = getattr(exc, "execution_error", None)
    if isinstance(explicit, str) and explicit:
        message = explicit
    elif isinstance(exc, TimeoutError):
        message = "执行请求超时，结果尚未确认"
    else:
        message = "worker 执行失败，具体原因未确认"
    return WorkerBackendErrorPayload(
        type=_camel_to_snake(exc.__class__.__name__),
        technical_detail=str(exc),
        traceback=getattr(exc, "formatted_traceback", None) or "".join(traceback.format_exception(exc)),
        message=message,
        reason_code=getattr(exc, "reason_code", None),
        error_id=getattr(exc, "error_id", None),
        retryable=isinstance(exc, TimeoutError) or bool(getattr(exc, "requires_session_recovery", False)),
        error_scope=connection_error_scope(exc),
        diagnostics=json_error_value(getattr(exc, "diagnostics", []) or []),
        exception_type=type(exc).__name__,
        retry_after=getattr(exc, "retry_after", 0),
    )


def _build_terminated_response(
    *,
    request_id: str,
    account: AccountContext,
    exc: ExecutionTerminated,
) -> WorkerBackendResponse:
    """
    构造 worker terminated 响应。

    Parameters
    ----------
    request_id : str
        当前请求标识。
    account : Account
        当前请求绑定的账户对象。
    exc : ExecutionTerminated
        终止异常对象。

    Returns
    -------
    WorkerBackendResponse
        结构化 terminated 响应。
    """
    # terminated 不是普通 error；主进程需要拿到 mode / acked_at / trigger 等字段
    # 继续驱动上层状态机与审计——worker 进程内触发的总超时也要能被主进程识别出来。
    return WorkerBackendResponse(
        request_id=request_id,
        kind="terminated",
        channel_type=account.trade_channel,
        reason=exc.reason,
        mode=exc.mode,
        acked_at=exc.acked_at,
        trigger=exc.trigger,
        cancel_failed_order_ids=exc.cancel_failed_order_ids,
        forced=exc.forced,
        cancel_attempted=exc.cancel_attempted,
        cancel_unconfirmed=exc.cancel_unconfirmed,
    )


def _dump_output_payload(output: UnifiedStandardOutput) -> dict[str, object]:
    """序列化 worker 标准输出载荷.

    Parameters
    ----------
    output : UnifiedStandardOutput
        执行器返回的统一输出对象。

    Returns
    -------
    dict[str, object]
        适合跨进程传输和审计落盘的 JSON 兼容字典。
    """
    # inputs 是主进程已持有的请求副本，其中包含账户凭据。Pydantic 还会按
    # BaseAccountConfig 声明类型截断渠道子类字段，导致主进程无法重新校验该副本。
    # 审计输入使用独立的脱敏 audit_input，因此 worker 响应无需回传 inputs。
    return cast("dict[str, object]", output.model_dump(mode="json", exclude={"inputs"}))


def _build_result_response(
    *,
    request_id: str,
    result: dict[str, object],
    normalized_symbol_fields: dict[str, object] | None = None,
) -> WorkerBackendResponse:
    """
    构造 worker 成功结果响应。

    Parameters
    ----------
    request_id : str
        当前请求标识。
    result : dict[str, object]
        序列化后的结果载荷。

    Returns
    -------
    WorkerBackendResponse
        成功结果响应。
    """
    # 这里返回的必须是纯字典载荷，主进程不会共享 Python 对象实例，只认协议字段。
    return WorkerBackendResponse(
        request_id=request_id,
        kind="result",
        output_payload=result,
        normalized_symbol_fields=normalized_symbol_fields,
    )


def _handle_worker_command_failure(
    *,
    request: WorkerBackendRequest,
    account: AccountContext,
    algorithm_name: str,
    executor: object | None,
    trigger_source: str,
    exc: Exception,
    log_message: str,
    append_failed_audit: Callable[..., None],
    normalized_symbol_fields: dict[str, object] | None = None,
) -> WorkerBackendResponse:
    """
    补写失败审计并返回统一错误响应。

    Parameters
    ----------
    request : WorkerBackendRequest
        当前 worker 请求。
    account : Account
        当前请求绑定的账户对象。
    algorithm_name : str
        本次请求解析出的算法名。
    executor : object | None
        当前请求使用的执行器；失败前可能尚未创建。
    trigger_source : str
        当前请求的触发来源。
    exc : Exception
        当前捕获的异常对象。
    log_message : str
        需要输出的日志消息前缀。
    append_failed_audit : Callable[..., None]
        当前请求应使用的失败审计写入函数。

    Returns
    -------
    WorkerBackendResponse
        统一错误响应。
    """
    # 先补失败审计再回错误响应，这样即使主进程马上抛错，审计流里也已经有失败事件。
    error_payload = _build_error_payload(exc)
    try:
        append_failed_audit(
            account=account,
            execution_id=request.execution_id,
            algorithm_name=algorithm_name,
            executor=executor,
            error=error_payload,
            trigger_source=trigger_source,
        )
    except Exception as audit_exc:  # noqa: BLE001 - 审计失败不得覆盖原始业务异常
        logger.opt(exception=audit_exc).error(
            "worker backend 失败审计写入异常，保留原始错误响应: request_id={}",
            request.request_id,
        )
    logger.exception("{}: {}", log_message, exc)
    return WorkerBackendResponse(
        request_id=request.request_id,
        kind="error",
        error=error_payload,
        channel_type=account.trade_channel,
        normalized_symbol_fields=normalized_symbol_fields,
    )
