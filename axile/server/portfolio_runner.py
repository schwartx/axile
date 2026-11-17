"""在一次性子进程中执行模拟账户组合函数，保留无账户内部调用兼容。"""

from __future__ import annotations

from multiprocessing import get_context
from multiprocessing.connection import Connection

from axile.channels.simulation import SimulationAccount
from axile.common.serialization import copy_data, to_payload
from axile.executor.models.portfolio_target import PortfolioTarget
from axile.executor.models.unified_account_assets import UnifiedAccountAssets
from axile.executor.models.unified_order import TradeRecord, UnifiedOrder
from axile.executor.models.unified_price import UnifiedPriceData
from axile.server.context import Context, ContextSettings
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


class SimulationPortfolioExecutor:
    """仅实现 PortfolioExecutor 查询协议，不提供真实渠道或交易方法。"""

    def __init__(self, snapshot: SimulationAccount) -> None:
        self._snapshot = copy_data(snapshot, deep=True)
        self._quotes = {quote.symbol: quote for quote in self._snapshot.quotes}

    def get_account_assets(self) -> UnifiedAccountAssets:
        return self._snapshot.account

    def get_market_data(self, symbols: list[str]) -> dict[str, UnifiedPriceData]:
        missing = [symbol for symbol in symbols if symbol not in self._quotes]
        if missing:
            raise ValueError(f"缺少模拟行情: {', '.join(missing)}")
        return {symbol: self._quotes[symbol] for symbol in symbols}

    def get_pending_orders(self, symbol: str | None = None) -> list[UnifiedOrder]:
        return []

    def query_trades(self, symbol: str, order_id: str) -> list[TradeRecord]:
        return []


def _run_sample_portfolio_child(
    connection: Connection, code: str, settings: ContextSettings, simulation_account: SimulationAccount | None
) -> None:
    """在子进程内构造独立账户快照并返回函数结果载荷。"""
    try:
        executor = SimulationPortfolioExecutor(simulation_account) if simulation_account is not None else None
        result = calculate_portfolio_target(code, Context(executor, settings=settings))
        connection.send(to_payload(result))
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
    leverage_overrides: dict[str, float] | None = None,
    simulation_account: SimulationAccount | None = None,
) -> PortfolioTarget:
    """在一次性子进程中试跑；保留无账户内部调用兼容，不用于正式目标保存。"""
    values = (
        {"long_leverage": simulation_account.long_leverage, "short_leverage": simulation_account.short_leverage}
        if simulation_account is not None
        else {}
    )
    values.update(leverage_overrides or {})
    settings = ContextSettings(**values)
    context = get_context("spawn")
    receive_connection, send_connection = context.Pipe(duplex=False)
    process = context.Process(
        target=_run_sample_portfolio_child,
        args=(send_connection, code, settings, copy_data(simulation_account, deep=True)),
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
            detail = f"组合试跑子进程异常退出: exitcode={process.exitcode}"
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
