"""在一次性子进程中执行样例组合函数."""

from __future__ import annotations

from multiprocessing import get_context
from multiprocessing.connection import Connection

from axile.server.context import build_sample_context
from axile.server.portfolio_function import (
    PortfolioFunctionError,
    _error_from_exception,
    _normalize_target,
    calculate_portfolio_target,
)

PORTFOLIO_FUNCTION_TIMEOUT_SECONDS = 30.0
"""单次组合函数允许占用的最大墙钟时间."""

PORTFOLIO_FUNCTION_IPC_GRACE_SECONDS = 5.0
"""子进程返回结果和退出的通信余量."""


def _run_sample_portfolio_child(connection: Connection, code: str) -> None:
    """在子进程内构造样例 Context 并返回函数结果载荷."""
    try:
        result = calculate_portfolio_target(code, build_sample_context())
        connection.send(result)
    except BaseException as exc:  # noqa: BLE001 - 子进程边界统一回传结构化错误
        connection.send(exc if isinstance(exc, PortfolioFunctionError) else _error_from_exception(exc))
    finally:
        connection.close()


def _stop_process(process: object) -> None:
    """终止并收割仍在运行的样例子进程."""
    is_alive = getattr(process, "is_alive")
    join = getattr(process, "join")
    terminate = getattr(process, "terminate")
    if is_alive():
        terminate()
        join(timeout=2.0)
    if is_alive():
        kill = getattr(process, "kill", None)
        if callable(kill):
            kill()
        join(timeout=2.0)


def calculate_sample_portfolio(
    code: str,
    *,
    timeout: float = PORTFOLIO_FUNCTION_TIMEOUT_SECONDS,
) -> dict[str, float]:
    """在可强制终止的一次性子进程中执行样例组合函数."""
    context = get_context("spawn")
    receive_connection, send_connection = context.Pipe(duplex=False)
    process = context.Process(
        target=_run_sample_portfolio_child,
        args=(send_connection, code),
        name="axile-sample-portfolio-runner",
        daemon=True,
    )
    process.start()
    send_connection.close()
    try:
        if not receive_connection.poll(timeout):
            _stop_process(process)
            raise _error_from_exception(TimeoutError(f"自定义组合函数执行超时（{timeout:g}s）"))
        try:
            payload = receive_connection.recv()
        except (EOFError, OSError):
            process.join(timeout=0.1)
            detail = f"样例组合函数子进程异常退出: exitcode={process.exitcode}"
            raise _error_from_exception(RuntimeError(detail))
        if isinstance(payload, PortfolioFunctionError):
            raise payload
        return _normalize_target(payload)
    finally:
        receive_connection.close()
        process.join(timeout=PORTFOLIO_FUNCTION_IPC_GRACE_SECONDS)
        _stop_process(process)


__all__ = [
    "PORTFOLIO_FUNCTION_IPC_GRACE_SECONDS",
    "PORTFOLIO_FUNCTION_TIMEOUT_SECONDS",
    "calculate_sample_portfolio",
]
