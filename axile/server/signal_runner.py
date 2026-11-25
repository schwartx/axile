"""用独立进程执行调仓信号的只读数据函数与数量组合。"""

from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from multiprocessing import get_context
from multiprocessing.connection import Connection
from typing import cast

from axile.channels.rebalance_signal import SignalAccountConfig, SignalAccountContext, SignalAssets, SignalQuoteSource
from axile.common.function_contract import accepts_context
from axile.common.serialization import parse_input
from axile.executor.algorithms.utils.clock import clock_monotonic, clock_now_iso
from axile.executor.models.portfolio_target import PortfolioTarget
from axile.executor.models.unified_account_assets import Position, UnifiedAccountAssets
from axile.executor.models.unified_order import TradeRecord, UnifiedOrder
from axile.executor.models.unified_price import UnifiedPriceData
from axile.server.context import Context
from axile.server.market_data import read_configured_market_data, read_market_data
from axile.server.portfolio_function import PortfolioFunctionError, _error_from_exception, calculate_portfolio_target
from axile.server.portfolio_runner import (
    PORTFOLIO_FUNCTION_IPC_GRACE_SECONDS,
    _MarketPortfolioExecutor,
    _quote_reply,
    _QuoteRequest,
    _stop_process,
)
from axile.server.tushare_quotes import read_tushare_quotes


@dataclass
class SignalCalculation:
    """同一轮计算使用的账户观测与目标。"""

    assets: UnifiedAccountAssets
    target: PortfolioTarget | None


def source_function(code: str, name: str) -> Callable[[object], object]:
    """加载用户配置的同步单参数函数。"""
    namespace: dict[str, object] = {}
    exec(compile(code, f"<{name}>", "exec"), namespace)  # noqa: S102 - 用户显式配置 Python 函数
    function = namespace.get(name)
    if not accepts_context(function):
        raise ValueError(f"请定义同步单参数函数 {name}")
    return cast(Callable[[object], object], function)


def _custom_quotes(
    fetch: Callable[[object], object], symbols: list[str], *, allow_partial: bool = False
) -> dict[str, UnifiedPriceData]:
    values = fetch(symbols)
    if not isinstance(values, dict):
        raise ValueError("行情函数必须返回按标的索引的字典")
    quotes: dict[str, UnifiedPriceData] = {}
    for symbol in symbols:
        quote = values.get(symbol)
        if quote is None:
            if allow_partial:
                continue
            raise ValueError(f"缺少行情：{symbol}")
        if not isinstance(quote, UnifiedPriceData):
            raise ValueError(f"行情函数必须返回 UnifiedPriceData：{symbol}")
        quote.validate()
        quotes[symbol] = quote
    return quotes


def _account_assets(
    assets: SignalAssets | None, account_code: str | None, context: SignalAccountContext
) -> UnifiedAccountAssets:
    if account_code:
        fetch = source_function(account_code, "fetch_account")
        assets = parse_input(SignalAssets, fetch(context))
    if assets is None:
        raise ValueError("缺少账户资产")
    return UnifiedAccountAssets(
        total_asset=assets.total_asset,
        available_cash=assets.available_cash,
        market_value=None,
        positions=assets.positions if assets.positions is not None else [],
        currency=assets.currency,
        update_time=clock_now_iso(),
        source="signal_function" if account_code else "signal_manual",
        extra={"positions_known": assets.positions is not None},
    )


class _SignalQueries:
    """只提供组合计算所需的数据访问，行情按组合请求读取。"""

    def __init__(self, assets: UnifiedAccountAssets, code: str | None, pipe: Connection) -> None:
        self.assets = assets
        self.code = code
        self.market = _MarketPortfolioExecutor(pipe)
        self._fetch: Callable[[object], object] | None = None

    def get_account_assets(self) -> UnifiedAccountAssets:
        return self.assets

    def get_market_data(self, symbols: list[str]) -> dict[str, UnifiedPriceData]:
        if self.code is None:
            return self.market.get_market_data(symbols)
        if self._fetch is None:
            self._fetch = source_function(self.code, "fetch_quotes")
        return _custom_quotes(self._fetch, symbols)

    def get_pending_orders(self, symbol: str | None = None) -> list[UnifiedOrder]:
        return []

    def query_trades(self, symbol: str, order_id: str) -> list[TradeRecord]:
        return []


class _SignalContext(Context):
    @property
    def positions(self) -> list[Position]:
        if self.account.extra["positions_known"] is False:
            raise ValueError("该账户尚未提供实际持仓")
        return self.account.positions


def _run_child(
    pipe: Connection,
    manual_assets: SignalAssets | None,
    account_code: str | None,
    quote_code: str | None,
    metadata: SignalAccountContext,
    code: str | None,
) -> None:
    try:
        assets = _account_assets(manual_assets, account_code, metadata)
        target = None
        if code is not None:
            target = calculate_portfolio_target(code, _SignalContext(_SignalQueries(assets, quote_code, pipe)))
            if target.mode != "quantity":
                raise ValueError("调仓信号的组合函数必须返回数量模式")
        pipe.send(SignalCalculation(assets, target))
    except BaseException as error:  # noqa: BLE001 - 子进程负责回传用户函数错误
        pipe.send(error if isinstance(error, PortfolioFunctionError) else _error_from_exception(error))
    finally:
        pipe.close()


def _read_channel_quotes(source: SignalQuoteSource, symbols: list[str], timeout: float) -> dict[str, UnifiedPriceData]:
    if source.kind == "gm":
        if source.connection_mode == "terminal" and source.terminal_path is not None:
            return read_configured_market_data(("gm", source.gm_config()), symbols, timeout=timeout)
        return read_market_data("gm", source.gm_values(), symbols, timeout=timeout)
    return read_tushare_quotes(source.url, source.token, symbols, timeout)


def _run_quote_child(pipe: Connection, code: str, symbols: list[str]) -> None:
    try:
        pipe.send(_custom_quotes(source_function(code, "fetch_quotes"), symbols, allow_partial=True))
    except BaseException as error:  # noqa: BLE001 - 子进程负责回传用户函数错误
        pipe.send(_error_from_exception(error))
    finally:
        pipe.close()


def run_signal_quote_test(code: str, symbols: list[str], timeout: float) -> dict[str, UnifiedPriceData]:
    """单独执行行情草稿，沿用信号计算的进程隔离和超时回收。"""
    return cast(dict[str, UnifiedPriceData], _run_isolated_signal(_run_quote_child, (code, symbols), timeout))


def run_signal_account_test(code: str, context: SignalAccountContext, timeout: float) -> SignalAssets:
    """单独执行开户草稿里的账户函数，并保留未知持仓与空仓的区别。"""
    result = cast(SignalCalculation, _run_isolated_signal(_run_child, (None, code, None, context, None), timeout))
    snapshot = result.assets
    return SignalAssets(
        total_asset=snapshot.total_asset,
        available_cash=snapshot.available_cash,
        currency=snapshot.currency,
        positions=snapshot.positions if snapshot.extra["positions_known"] else None,
    )


def run_signal(
    config: SignalAccountConfig,
    metadata: SignalAccountContext,
    code: str | None,
    timeout: float,
) -> SignalCalculation:
    """用与组合试跑相同的进程隔离策略，超时后终止计算。"""
    source = config.quote_source
    quote_code = source.code if source.kind == "custom" else None
    read_quotes = partial(_read_channel_quotes, source) if quote_code is None and code is not None else None
    args = (config.manual_assets, config.account_code, quote_code, metadata, code)
    return cast(SignalCalculation, _run_isolated_signal(_run_child, args, timeout, read_quotes))


def _run_isolated_signal(
    target: Callable[..., None],
    args: tuple[object, ...],
    timeout: float,
    read_quotes: Callable[[list[str], float], dict[str, UnifiedPriceData]] | None = None,
) -> object:
    context = get_context("spawn")
    parent, child = context.Pipe(duplex=read_quotes is not None)
    process = context.Process(target=target, args=(child, *args), daemon=True)
    try:
        process.start()
        child.close()
        deadline = clock_monotonic() + timeout
        while (remaining := deadline - clock_monotonic()) > 0 and parent.poll(remaining):
            result = parent.recv()
            if isinstance(result, _QuoteRequest) and read_quotes is not None:
                remaining = deadline - clock_monotonic()
                if remaining <= 0:
                    break
                parent.send(_quote_reply(result, read_quotes, remaining))
                continue
            if isinstance(result, PortfolioFunctionError):
                raise result
            return result
        _stop_process(process)
        raise TimeoutError("调仓信号取数或计算超时")
    except (EOFError, BrokenPipeError) as error:
        raise ValueError("调仓信号计算进程异常退出") from error
    finally:
        parent.close()
        child.close()
        if process.pid is not None:
            process.join(timeout=PORTFOLIO_FUNCTION_IPC_GRACE_SECONDS)
            _stop_process(process)
        process.close()
