"""在一次性子进程中执行组合试跑与信号数据函数。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from multiprocessing import get_context
from multiprocessing.connection import Connection
from typing import cast

from axile.channels.market_data import MarketDataUnavailable
from axile.channels.simulation import SimulationAccount
from axile.common.serialization import copy_data, to_payload
from axile.executor.algorithms.utils.clock import clock_monotonic
from axile.executor.models.portfolio_target import PortfolioTarget
from axile.executor.models.unified_account_assets import UnifiedAccountAssets
from axile.executor.models.unified_order import TradeRecord, UnifiedOrder
from axile.executor.models.unified_price import UnifiedPriceData
from axile.server.context import Context, ContextSettings, PortfolioExecutor
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


type QuoteReader = Callable[[list[str], float], dict[str, UnifiedPriceData]]


@dataclass(frozen=True, slots=True)
class _QuoteRequest:
    symbols: list[str]


class _MarketPortfolioExecutor:
    """通过父进程读取共享行情；账户读取需要就绪的交易会话。"""

    def __init__(self, pipe: Connection) -> None:
        self._pipe = pipe

    def get_market_data(self, symbols: list[str]) -> dict[str, UnifiedPriceData]:
        self._pipe.send(_QuoteRequest(symbols))
        result = self._pipe.recv()
        if isinstance(result, Exception):
            raise result
        return cast(dict[str, UnifiedPriceData], result)

    def get_account_assets(self) -> UnifiedAccountAssets:
        raise ValueError("读取资产或持仓需要先连接交易账户")

    def get_pending_orders(self, symbol: str | None = None) -> list[UnifiedOrder]:
        raise ValueError("读取委托需要先连接交易账户")

    def query_trades(self, symbol: str, order_id: str) -> list[TradeRecord]:
        raise ValueError("读取成交需要先连接交易账户")


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
    connection: Connection,
    code: str,
    settings: ContextSettings,
    simulation_account: SimulationAccount | None,
    read_market: bool,
) -> None:
    """在子进程内构造独立账户快照并返回函数结果载荷。"""
    try:
        executor: PortfolioExecutor | None = None
        if simulation_account is not None:
            executor = SimulationPortfolioExecutor(simulation_account)
        elif read_market:
            executor = _MarketPortfolioExecutor(connection)
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
    return _run_portfolio(code, ContextSettings(**values), timeout=timeout, simulation_account=simulation_account)


def calculate_market_portfolio(
    code: str,
    settings: ContextSettings,
    read_quotes: QuoteReader,
    *,
    timeout: float = PORTFOLIO_FUNCTION_TIMEOUT_SECONDS,
) -> PortfolioTarget:
    """在独立子进程按需取行情，复用主服务的行情源和配置。"""
    return _run_portfolio(code, settings, timeout=timeout, read_quotes=read_quotes)


def _quote_reply(
    request: _QuoteRequest, read_quotes: QuoteReader, remaining: float
) -> dict[str, UnifiedPriceData] | Exception:
    symbols = request.symbols
    if (
        not isinstance(symbols, list)
        or not 1 <= len(symbols) <= 50
        or any(
            not isinstance(symbol, str) or not 1 <= len(symbol) <= 80 or symbol.strip() != symbol for symbol in symbols
        )
    ):
        return ValueError("行情查询需要 1 至 50 个有效合约代码")
    try:
        return read_quotes(list(dict.fromkeys(symbols)), min(remaining, 5.0))
    except (MarketDataUnavailable, ValueError, TimeoutError) as exc:
        return exc
    except Exception:  # noqa: BLE001 - IPC 只回传可展示的渠道错误
        return MarketDataUnavailable("独立行情查询失败")


def _run_portfolio(
    code: str,
    settings: ContextSettings,
    *,
    timeout: float,
    simulation_account: SimulationAccount | None = None,
    read_quotes: QuoteReader | None = None,
) -> PortfolioTarget:
    try:
        payload = run_portfolio_child(
            _run_sample_portfolio_child,
            (code, settings, copy_data(simulation_account, deep=True), read_quotes is not None),
            timeout,
            read_quotes,
            name="axile-portfolio-trial",
        )
    except TimeoutError:
        raise _error_from_exception(TimeoutError(f"自定义组合函数执行超时（{timeout:g}s）")) from None
    except EOFError as exc:
        raise _error_from_exception(RuntimeError(f"组合试跑子进程异常退出: {exc}")) from exc
    return _normalize_target(payload)


def run_portfolio_child(
    target: Callable[..., None],
    args: tuple[object, ...],
    timeout: float,
    read_quotes: QuoteReader | None = None,
    *,
    name: str | None = None,
) -> object:
    """执行子进程并按需转发取价请求，超时或失败时回收进程。"""
    context = get_context("spawn")
    parent, child = context.Pipe(duplex=read_quotes is not None)
    process = context.Process(target=target, args=(child, *args), name=name, daemon=True)
    try:
        process.start()
        child.close()
        deadline = clock_monotonic() + timeout
        while (remaining := deadline - clock_monotonic()) > 0 and parent.poll(remaining):
            payload = parent.recv()
            if isinstance(payload, _QuoteRequest) and read_quotes is not None:
                remaining = deadline - clock_monotonic()
                if remaining <= 0:
                    break
                parent.send(_quote_reply(payload, read_quotes, remaining))
                continue
            if isinstance(payload, PortfolioFunctionError):
                raise payload
            return payload
        _stop_process(process)
        raise TimeoutError
    except (EOFError, BrokenPipeError) as exc:
        if process.pid is None:
            raise
        process.join(timeout=0.1)
        raise EOFError(f"exitcode={process.exitcode}") from exc
    finally:
        child.close()
        parent.close()
        if process.pid is not None:
            process.join(timeout=PORTFOLIO_FUNCTION_IPC_GRACE_SECONDS)
            _stop_process(process)
        process.close()


__all__ = [
    "PORTFOLIO_FUNCTION_IPC_GRACE_SECONDS",
    "PORTFOLIO_FUNCTION_TIMEOUT_SECONDS",
    "calculate_market_portfolio",
    "calculate_sample_portfolio",
    "run_portfolio_child",
]
