"""运行多进程执行 worker 的命令分派入口.

该文件只保留 worker 命令级骨架：解析请求、准备执行器、写审计、
回传响应和清理运行时。更细的请求解析、状态复用与响应规范化逻辑
分别下沉到相邻辅助模块。
"""

from __future__ import annotations

import os
import threading
import traceback
from multiprocessing.connection import Connection
from typing import cast

from loguru import logger

from axile.executor.abstract_executor.base import AbstractExecutor
from axile.executor.algorithms.utils.clock import get_default_clock
from axile.executor.models.unified_account_assets import UnifiedAccountAssets
from axile.executor.models.unified_input import UnifiedStandardInput
from axile.executor.models.unified_output import UnifiedStandardOutput
from axile.executor.termination import ExecutionTerminated
from axile.server.context import Context, PortfolioExecutor
from axile.server.db.models.account import AccountSnapshot
from axile.server.execution.execution_records_output import sanitize_standard_input_for_audit
from axile.server.execution.worker_backend.catalog import RemoteCatalogProvider
from axile.server.execution.worker_backend.protocol import (
    WorkerBackendErrorPayload,
    WorkerBackendRequest,
    WorkerBackendResponse,
    WorkerTerminationSignal,
)
from axile.server.execution.worker_backend.worker_audit import (
    _append_empty_positions_pre_execute_audit,
    _append_failed_audit,
    _append_success_audit,
    _append_trade_pre_execute_audit,
)
from axile.server.execution.worker_backend.worker_requests import (
    _parse_empty_positions_request,
    _parse_execute_trade_request,
)
from axile.server.execution.worker_backend.worker_responses import (
    _build_error_payload,
    _build_result_response,
    _build_terminated_response,
    _dump_output_payload,
    _handle_worker_command_failure,
)
from axile.server.execution.worker_backend.worker_state import (
    _activate_worker_termination,
    _clear_worker_termination,
    _close_executor,
    _close_state_executor,
    _finalize_executor,
    _request_worker_termination,
    _resolve_prepared_executor,
    _WorkerBackendState,
)
from axile.server.portfolio_function import calculate_portfolio_target
from axile.server.portfolio_runner import PORTFOLIO_FUNCTION_TIMEOUT_SECONDS

_SYMBOL_FIELDS = (
    "curr_target",
    "last_target",
    "symbol_algorithms",
    "trade_rules",
    "forbidden_symbols",
    "risk_symbols",
)


def _handle_prepare(request: WorkerBackendRequest, state: _WorkerBackendState) -> WorkerBackendResponse:
    """创建或复用账户执行器，并返回通道准备结果。"""
    account = AccountSnapshot.model_validate(request.account_payload)
    expected = str(request.payload.get("expected_trading_day", "") or "") or None
    executor = None
    termination_controller = _activate_worker_termination(state, request.execution_id)
    try:
        executor = _resolve_prepared_executor(
            state=state,
            account=account,
            execution_id=request.execution_id or request.request_id,
            audit_context={"request_id": request.request_id, "trigger_source": "prepare"},
            termination_controller=termination_controller,
        )
        if expected and account.trade_channel.value == "ctp":
            trading_day = str(getattr(executor, "_trading_day", "") or "")
            if trading_day != expected:
                raise RuntimeError(f"柜台交易日不匹配: expected={expected}, actual={trading_day or 'unknown'}")
        return WorkerBackendResponse(
            request_id=request.request_id,
            kind="result",
            output_payload={
                "ready": True,
                "trading_day": str(getattr(executor, "_trading_day", "") or ""),
            },
        )
    except Exception as exc:  # noqa: BLE001 - IPC 边界统一返回结构化错误
        error = _build_error_payload(exc)
        if error.reason_code != "CTP_NOT_INITIALIZED":
            # IPC 只传安全文案；故障的技术上下文保留在 worker 日志。
            logger.opt(exception=exc).error("账户通道准备失败 account_id={}", account.id)
        return WorkerBackendResponse(
            request_id=request.request_id,
            kind="error",
            channel_type=account.trade_channel,
            error=error,
        )
    finally:
        _finalize_executor(executor)
        _clear_worker_termination(state, request.execution_id)


def _capture_before_account_snapshot(executor: object) -> dict[str, object] | None:
    """执行前拉取一次账户快照作为对账基线（worker 同步路径）.

    Parameters
    ----------
    executor : object
        已就绪的执行器实例。

    Returns
    -------
    dict[str, object] | None
        账户快照的 JSON 化内容；读取失败或执行器不支持时返回 ``None``（由下游按
        ``unavailable`` 处理），不影响执行主线。
    """
    get_account_assets = getattr(executor, "get_account_assets", None)
    if not callable(get_account_assets):
        return None
    try:
        assets = cast(UnifiedAccountAssets, get_account_assets())
    except Exception as exc:  # noqa: BLE001
        if bool(getattr(exc, "requires_session_recovery", False)):
            raise
        logger.warning(f"执行前账户快照读取失败，将标记为 unavailable: {exc}")
        return None
    return cast("dict[str, object]", assets.model_dump(mode="json"))


def _handle_get_account_assets(
    request: WorkerBackendRequest,
    state: _WorkerBackendState,
) -> WorkerBackendResponse:
    """查询并返回账户当前资产，不进入交易执行生命周期."""
    account = AccountSnapshot.model_validate(request.account_payload)
    executor = None
    try:
        executor = _resolve_prepared_executor(
            state=state,
            account=account,
            execution_id=request.execution_id or request.request_id,
            audit_context={"request_id": request.request_id, "trigger_source": "account_assets"},
        )
        assets = cast(AbstractExecutor, executor).get_account_assets()
        return WorkerBackendResponse(
            request_id=request.request_id,
            kind="result",
            output_payload=cast("dict[str, object]", assets.model_dump(mode="json")),
            channel_type=account.trade_channel,
        )
    except Exception as exc:  # noqa: BLE001 - IPC 边界统一返回结构化错误
        return WorkerBackendResponse(
            request_id=request.request_id,
            kind="error",
            channel_type=account.trade_channel,
            error=_build_error_payload(exc),
        )
    finally:
        _finalize_executor(executor)


def _handle_calculate_portfolio(
    request: WorkerBackendRequest,
    state: _WorkerBackendState,
) -> WorkerBackendResponse:
    """使用账户常驻执行器运行可信的自定义组合函数.

    正常成功后保留完整 executor 及进程状态；失败结果由 manager 关闭并重建
    worker，超时则由 watchdog 直接终止当前进程。
    """
    account = AccountSnapshot.model_validate(request.account_payload)
    executor = None
    calculation_finished = threading.Event()
    try:
        executor = _resolve_prepared_executor(
            state=state,
            account=account,
            execution_id=request.execution_id,
            audit_context={
                "execution_id": request.execution_id,
                "account_id": account.id,
                "channel": str(account.trade_channel),
                "trigger_source": "portfolio_calculation",
            },
        )
        code = str(request.payload.get("code", ""))
        watchdog = threading.Thread(
            target=_terminate_stuck_portfolio_calculation,
            args=(calculation_finished, PORTFOLIO_FUNCTION_TIMEOUT_SECONDS),
            name=f"axile-portfolio-watchdog-{account.id}",
            daemon=True,
        )
        watchdog.start()
        result = calculate_portfolio_target(code, Context(cast("PortfolioExecutor", executor)))
        return WorkerBackendResponse(
            request_id=request.request_id,
            kind="result",
            output_payload=result.to_payload(),
            channel_type=account.trade_channel,
        )
    except Exception as exc:  # noqa: BLE001 - IPC 边界统一返回结构化错误
        return WorkerBackendResponse(
            request_id=request.request_id,
            kind="error",
            channel_type=account.trade_channel,
            error=_build_error_payload(exc),
        )
    finally:
        calculation_finished.set()
        _finalize_executor(executor)


def _terminate_stuck_portfolio_calculation(finished: threading.Event, timeout: float) -> None:
    """组合函数超过墙钟上限时立即终止其所在 worker."""
    if not get_default_clock().event_wait(finished, timeout):
        os._exit(124)


def _handle_execute_trade(
    request: WorkerBackendRequest,
    state: _WorkerBackendState,
) -> WorkerBackendResponse:
    """处理单次 `execute_trade` 命令.

    Parameters
    ----------
    request : WorkerBackendRequest
        主进程发来的调仓命令。
    state : _WorkerBackendState
        worker 当前缓存状态。

    Returns
    -------
    WorkerBackendResponse
        对应的成功、终止或失败响应。
    """
    context = _parse_execute_trade_request(request)
    executor = None
    termination_controller = _activate_worker_termination(state, request.execution_id)
    normalized_symbol_fields: dict[str, object] | None = None
    try:
        # worker 入口只保留命令级主线，具体的状态准备和审计细节都下沉到专用模块。
        executor = _resolve_prepared_executor(
            state=state,
            account=context.account,
            execution_id=request.execution_id,
            audit_context=context.audit_context,
            termination_controller=termination_controller,
        )
        normalize = getattr(executor, "_normalize_standard_input", lambda value: value)
        normalize_connected = getattr(executor, "_normalize_connected_standard_input", lambda value: value)
        standard_input = cast(UnifiedStandardInput, normalize(context.standard_input))
        standard_input = cast(UnifiedStandardInput, normalize_connected(standard_input))
        normalized_symbol_fields = {field: getattr(standard_input, field) for field in _SYMBOL_FIELDS}
        audit_input = sanitize_standard_input_for_audit(standard_input)
        before_account_assets = _capture_before_account_snapshot(executor)
        _append_trade_pre_execute_audit(
            account=context.account,
            execution_id=request.execution_id,
            algorithm_name=context.algorithm_name,
            trigger_source=context.trigger_source,
            audit_input=audit_input,
            standard_input=standard_input,
            executor=executor,
            before_account_assets=before_account_assets,
        )
        execute = getattr(executor, "execute")
        output = cast(
            UnifiedStandardOutput,
            execute(
                standard_input,
                cleanup=False,
                retain_runtime=True,
                **({"notification_snapshot": context.notification_snapshot} if context.notification_snapshot else {}),
            ),
        )
        result = _dump_output_payload(output)
        _append_success_audit(
            account=context.account,
            execution_id=request.execution_id,
            algorithm_name=context.algorithm_name,
            output=output,
            result=result,
            executor=executor,
            include_trade_rule_snapshots=True,
            before_account_assets=before_account_assets,
        )
        return _build_result_response(
            request_id=request.request_id,
            result=result,
            normalized_symbol_fields=normalized_symbol_fields,
        )
    except ExecutionTerminated as exc:
        return _build_terminated_response(
            request_id=request.request_id,
            account=context.account,
            exc=exc,
        )
    except Exception as exc:  # noqa: BLE001
        return _handle_worker_command_failure(
            request=request,
            account=context.account,
            algorithm_name=context.algorithm_name,
            executor=executor,
            trigger_source=context.trigger_source,
            exc=exc,
            log_message="worker backend execute_trade 失败",
            append_failed_audit=_append_failed_audit,
            normalized_symbol_fields=normalized_symbol_fields,
        )
    finally:
        _finalize_executor(executor)
        _clear_worker_termination(state, request.execution_id)


def _handle_empty_positions(
    request: WorkerBackendRequest,
    state: _WorkerBackendState,
) -> WorkerBackendResponse:
    """处理单次 `empty_positions` 命令.

    Parameters
    ----------
    request : WorkerBackendRequest
        主进程发来的清仓命令。
    state : _WorkerBackendState
        worker 当前缓存状态。

    Returns
    -------
    WorkerBackendResponse
        对应的成功、终止或失败响应。
    """
    context = _parse_empty_positions_request(request)
    executor = None
    termination_controller = _activate_worker_termination(state, request.execution_id)
    try:
        # 清仓和调仓共用同一套外层骨架，差异只保留在请求解析、pre-audit 和真实调用上。
        executor = _resolve_prepared_executor(
            state=state,
            account=context.account,
            execution_id=request.execution_id,
            audit_context=context.audit_context,
            termination_controller=termination_controller,
        )
        before_account_assets = _capture_before_account_snapshot(executor)
        _append_empty_positions_pre_execute_audit(
            account=context.account,
            execution_id=request.execution_id,
            algorithm_name=context.algorithm_name,
            executor=executor,
            before_account_assets=before_account_assets,
        )
        empty_positions = getattr(executor, "empty_positions")
        output = cast(
            UnifiedStandardOutput,
            empty_positions(
                cleanup=False,
                retain_runtime=True,
                **({"notification_snapshot": context.notification_snapshot} if context.notification_snapshot else {}),
                **context.empty_kwargs,
            ),
        )
        result = _dump_output_payload(output)
        _append_success_audit(
            account=context.account,
            execution_id=request.execution_id,
            algorithm_name=context.algorithm_name,
            output=output,
            result=result,
            executor=executor,
            include_trade_rule_snapshots=False,
            audit_input=context.audit_input,
            before_account_assets=before_account_assets,
        )
        return _build_result_response(request_id=request.request_id, result=result)
    except ExecutionTerminated as exc:
        return _build_terminated_response(
            request_id=request.request_id,
            account=context.account,
            exc=exc,
        )
    except Exception as exc:  # noqa: BLE001
        return _handle_worker_command_failure(
            request=request,
            account=context.account,
            algorithm_name=context.algorithm_name,
            executor=executor,
            trigger_source="empty_positions",
            exc=exc,
            log_message="worker backend empty_positions 失败",
            append_failed_audit=_append_failed_audit,
        )
    finally:
        _finalize_executor(executor)
        _clear_worker_termination(state, request.execution_id)


def _handle_shutdown(
    request: WorkerBackendRequest,
    state: _WorkerBackendState,
) -> WorkerBackendResponse:
    """处理 worker 显式关闭请求.

    Parameters
    ----------
    request : WorkerBackendRequest
        当前关闭请求。
    state : _WorkerBackendState
        worker 运行时状态。

    Returns
    -------
    WorkerBackendResponse
        关闭确认响应。
    """
    reason = str(request.payload.get("reason", "manager_shutdown"))
    if state.executor is not None:
        _close_state_executor(state)
        state.executor = None
        state.account_id = None
        state.config_signature = None
    return WorkerBackendResponse(
        request_id=request.request_id,
        kind="result",
        output_payload={"shutdown": True, "reason": reason},
    )


def _handle_worker_request(
    request: WorkerBackendRequest,
    state: _WorkerBackendState,
) -> WorkerBackendResponse:
    """分派并执行单个 worker 请求.

    Parameters
    ----------
    request : WorkerBackendRequest
        主进程发来的请求。
    state : _WorkerBackendState
        worker 当前运行时状态。

    Returns
    -------
    WorkerBackendResponse
        对应响应对象。
    """
    if request.command == "prepare":
        return _handle_prepare(request, state)
    if request.command == "get_account_assets":
        return _handle_get_account_assets(request, state)
    if request.command == "calculate_portfolio":
        return _handle_calculate_portfolio(request, state)
    if request.command == "execute_trade":
        return _handle_execute_trade(request, state)
    if request.command == "empty_positions":
        return _handle_empty_positions(request, state)
    return _handle_shutdown(request, state)


def _run_termination_control_loop(connection: Connection, state: _WorkerBackendState) -> None:
    """监听独立控制管道，使业务请求阻塞时仍能接收终止信号."""
    try:
        while True:
            signal = connection.recv()
            if signal is None:
                return
            if isinstance(signal, WorkerTerminationSignal):
                _request_worker_termination(state, signal)
    except (EOFError, OSError):
        return
    except BaseException as exc:  # noqa: BLE001 - IPC 最外层必须保留所有进程退出原因
        # 线程异常默认只走 threading.excepthook 写 stderr，多进程管道下容易被
        # 其他输出撕碎；必须兜底写入 loguru，完整堆栈才能落进日志文件。
        logger.opt(exception=exc).error(
            "控制管道监听线程异常退出 | account_id={}",
            state.account_id,
        )


def run_worker_backend_loop(
    connection: Connection,
    account_id: int,
    control_connection: Connection,
    catalog_connection: Connection | None = None,
    monitor_connection: Connection | None = None,
) -> None:
    """运行多进程 worker 的阻塞请求循环.

    Parameters
    ----------
    connection : Connection
        与主进程通信的管道连接。
    account_id : int
        该 worker 绑定的账户标识。
    """
    from axile.server.connection_state import ConnectionBinding, ConnectionCollector

    state = _WorkerBackendState(account_id=account_id)
    if monitor_connection is not None:
        from axile.server.account_control.ipc import serve

        threading.Thread(
            target=serve,
            args=(monitor_connection, account_id, lambda: state.connection_collector.snapshot()),
            daemon=True,
            name=f"axile-worker-monitor-{account_id}",
        ).start()
    catalog = RemoteCatalogProvider(catalog_connection) if catalog_connection is not None else None
    state.catalog_provider = catalog
    control_thread = threading.Thread(
        target=_run_termination_control_loop,
        args=(control_connection, state),
        name=f"axile-worker-control-{account_id}",
        daemon=True,
    )
    control_thread.start()
    logger.info("worker 启动 | account_id={}", account_id)
    current_request: WorkerBackendRequest | None = None
    try:
        while True:
            try:
                raw_request = connection.recv()
            except EOFError:
                break

            if raw_request is None:
                break

            current_request = cast(WorkerBackendRequest, raw_request)
            if catalog is not None:
                catalog.request_id = current_request.request_id
            binding = (
                ConnectionBinding(
                    account_id=int(str(current_request.connection_binding["account_id"])),
                    config_key=str(current_request.connection_binding["config_key"]),
                    instance_id=str(current_request.connection_binding["instance_id"]),
                )
                if current_request.connection_binding
                else None
            )
            if state.connection_collector.binding != binding:
                state.connection_collector = ConnectionCollector(binding)
            response = _handle_worker_request(current_request, state)
            response.connection_snapshot = state.connection_collector.snapshot()
            response.requires_worker_restart = (
                response.requires_worker_restart
                or state.requires_worker_restart
                or (catalog is not None and catalog.failed)
            )
            # 先回响应，再按 shutdown 命令退出循环，保证主进程一定能收到确认消息。
            connection.send(response)
            if current_request.command == "shutdown" or response.requires_worker_restart:
                break
            current_request = None
    except BaseException as exc:  # noqa: BLE001 - IPC 最外层必须保留所有进程退出原因
        request_id = current_request.request_id if current_request is not None else "unknown"
        logger.opt(exception=exc).critical(
            "未捕获异常 | account_id={} request_id={} command={}",
            account_id,
            request_id,
            current_request.command if current_request is not None else "unknown",
        )
        try:
            connection.send(
                WorkerBackendResponse(
                    request_id=request_id,
                    kind="error",
                    requires_worker_restart=True,
                    error=WorkerBackendErrorPayload(
                        type=exc.__class__.__name__,
                        message=str(exc) or repr(exc),
                        technical_detail=str(exc),
                        traceback="".join(traceback.format_exception(exc)),
                        retryable=False,
                    ),
                )
            )
        except Exception as send_exc:  # noqa: BLE001 - 管道可能已经断开
            logger.opt(exception=send_exc).error(
                "未捕获异常响应发送失败 | account_id={} request_id={}",
                account_id,
                request_id,
            )
    finally:
        try:
            _close_executor(state.executor)
        except BaseException as close_exc:  # noqa: BLE001 - 清理异常也必须可见
            logger.opt(exception=close_exc).error(
                "执行器清理失败 | account_id={}",
                account_id,
            )
        if catalog is not None:
            catalog.stop()
        try:
            connection.close()
        except OSError as close_exc:
            logger.opt(exception=close_exc).error(
                "管道关闭失败 | account_id={}",
                account_id,
            )
        try:
            control_connection.close()
        except OSError:
            pass
        logger.info("worker 退出 | account_id={}", account_id)
