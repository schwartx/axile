"""用独立进程执行调仓信号的只读数据函数与组合。"""

from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from math import isfinite, trunc
from multiprocessing.connection import Connection
from typing import cast

from axile.channels import get_channel
from axile.channels.rebalance_signal import (
    SignalAccountConfig,
    SignalAccountContext,
    SignalAssets,
    SignalQuoteSource,
    signal_market_descriptor,
)
from axile.common.function_contract import accepts_context
from axile.common.serialization import parse_input
from axile.executor.algorithms.utils.clock import clock_now_iso
from axile.executor.models.portfolio_target import PortfolioTarget, normalize_target_weights
from axile.executor.models.unified_account_assets import Position, UnifiedAccountAssets
from axile.executor.models.unified_order import TradeRecord, UnifiedOrder
from axile.executor.models.unified_price import UnifiedPriceData
from axile.server.context import Context, ContextSettings
from axile.server.market_data import read_configured_market_data
from axile.server.portfolio_function import PortfolioFunctionError, _error_from_exception, calculate_portfolio_target
from axile.server.portfolio_runner import (
    QuoteReader,
    _MarketPortfolioExecutor,
    run_portfolio_child,
)
from axile.server.tushare_quotes import read_tushare_quotes


@dataclass
class SignalCalculation:
    """同一轮计算使用的账户观测与目标。"""

    assets: UnifiedAccountAssets
    target: PortfolioTarget | None
    normalized_weights: dict[str, float] | None = None
    quantities: dict[str, float] | None = None

    def snapshot_fields(self) -> dict[str, object]:
        """为信号快照保留原始口径和同轮换算的数量。"""
        assert self.target is not None
        return {
            "target_mode": self.target.mode,
            "raw_weights": self.target.targets,
            "normalized_weights": self.target.targets if self.target.mode == "quantity" else self.normalized_weights,
            **({"signal_quantities": self.quantities} if self.target.mode == "weight" else {}),
        }

    def target_quantities(self) -> dict[str, float]:
        """返回信号推送使用的目标数量。"""
        return (
            self.target.targets if self.target is not None and self.target.mode == "quantity" else self.quantities or {}
        )


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


def _read_account_function(code: str, context: SignalAccountContext) -> SignalAssets:
    return parse_input(SignalAssets, source_function(code, "fetch_account")(context))


def _account_assets(
    assets: SignalAssets | None, account_code: str | None, context: SignalAccountContext
) -> UnifiedAccountAssets:
    if account_code:
        assets = _read_account_function(account_code, context)
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
        self.quotes: dict[str, UnifiedPriceData] = {}

    def get_account_assets(self) -> UnifiedAccountAssets:
        return self.assets

    def get_market_data(self, symbols: list[str]) -> dict[str, UnifiedPriceData]:
        if self.code is None:
            rows = self.market.get_market_data(symbols)
        else:
            if self._fetch is None:
                self._fetch = source_function(self.code, "fetch_quotes")
            rows = _custom_quotes(self._fetch, symbols)
        self.quotes.update(rows)
        return rows

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


def _size_weight_target(
    market: str, targets: dict[str, float], settings: ContextSettings, queries: _SignalQueries
) -> tuple[dict[str, float], dict[str, float]]:
    weights = normalize_target_weights(
        targets, settings.long_leverage, settings.short_leverage, settings.weight_precision
    )
    active = [symbol for symbol, weight in weights.items() if weight]
    quantities = dict.fromkeys(weights, 0.0)
    if not active:
        return weights, quantities
    descriptor = signal_market_descriptor(market)
    assets = queries.assets
    if assets.total_asset < 0:
        raise ValueError("账户总权益为负，无法按权重换算目标数量")
    if descriptor.currency and assets.currency.strip().upper() != descriptor.currency.upper():
        raise ValueError(f"{market} 的目标换算需要以 {descriptor.currency} 计价的账户权益")
    missing = [symbol for symbol in active if symbol not in queries.quotes]
    if missing:
        queries.get_market_data(missing)
    channel = get_channel(descriptor.channel)
    for symbol in active:
        quote = queries.quotes.get(symbol)
        if quote is None or not isfinite(quote.last_price) or quote.last_price <= 0:
            raise ValueError(f"{symbol} 缺少有效价格，无法换算目标数量")
        unit_value = quote.last_price
        if descriptor.units.quantity_kind == "contract":
            multiplier = quote.extra.get("volume_multiple")
            if (
                isinstance(multiplier, bool)
                or not isinstance(multiplier, int | float)
                or not 0 < multiplier < float("inf")
            ):
                raise ValueError(
                    f"{symbol} 缺少有效合约乘数，无法换算目标手数。"
                    "使用 CTP 独立行情时，可通过已连接的期货账户刷新共享合约目录，或让组合返回数量目标。"
                )
            unit_value *= multiplier
        if channel.quantize_target_quantity is not None:
            quantity = channel.quantize_target_quantity(weights[symbol], assets.total_asset, unit_value)
        else:
            raw_quantity = assets.total_asset * weights[symbol] / unit_value
            quantity = trunc(raw_quantity / 100) * 100.0 if descriptor.units.quantity_kind == "share" else raw_quantity
        quantities[symbol] = quantity
    return weights, PortfolioTarget.validate_targets(quantities)


def _run_child(
    pipe: Connection,
    manual_assets: SignalAssets | None,
    account_code: str | None,
    quote_code: str | None,
    metadata: SignalAccountContext,
    code: str | None,
    settings: ContextSettings | None = None,
    size_weights: bool = False,
) -> None:
    try:
        assets = _account_assets(manual_assets, account_code, metadata)
        result = SignalCalculation(assets, None)
        if code is not None:
            queries = _SignalQueries(assets, quote_code, pipe)
            context = _SignalContext(queries, settings=settings)
            result.target = calculate_portfolio_target(code, context)
            if size_weights and result.target.mode == "weight":
                result.normalized_weights, result.quantities = _size_weight_target(
                    metadata["market"], result.target.targets, context.settings, queries
                )
        pipe.send(result)
    except BaseException as error:  # noqa: BLE001 - 子进程负责回传用户函数错误
        pipe.send(error if isinstance(error, PortfolioFunctionError) else _error_from_exception(error))
    finally:
        pipe.close()


def _read_channel_quotes(source: SignalQuoteSource, symbols: list[str], timeout: float) -> dict[str, UnifiedPriceData]:
    if source.kind == "gm":
        return read_configured_market_data(("gm", source.gm_config()), symbols, timeout=timeout)
    if source.kind in ("ctp", "tq"):
        rows = read_configured_market_data((source.kind, source.futures_config()), symbols, timeout=timeout)
        if source.kind == "ctp":
            from axile.server.execution.worker_backend.manager import get_worker_backend_manager

            catalog = get_worker_backend_manager().instrument_catalog
            for quote in rows.values():
                if (
                    (exchange := quote.extra.get("exchange_id"))
                    and (instrument := catalog.cached(f"{exchange}.{quote.symbol}"))
                    and instrument.has_specs
                ):
                    quote.extra["volume_multiple"] = instrument.multiplier
        return rows
    return read_tushare_quotes(source.url, source.token, symbols, timeout)


def _read_quote_test(code: str, symbols: list[str]) -> dict[str, UnifiedPriceData]:
    return _custom_quotes(source_function(code, "fetch_quotes"), symbols, allow_partial=True)


def _run_test_child(pipe: Connection, read: Callable[[], object]) -> None:
    try:
        pipe.send(read())
    except BaseException as error:  # noqa: BLE001 - 子进程负责回传用户函数错误
        pipe.send(error if isinstance(error, PortfolioFunctionError) else _error_from_exception(error))
    finally:
        pipe.close()


def run_signal_quote_test(code: str, symbols: list[str], timeout: float) -> dict[str, UnifiedPriceData]:
    """单独执行行情草稿，沿用信号计算的进程隔离和超时回收。"""
    read = partial(_read_quote_test, code, symbols)
    return cast(dict[str, UnifiedPriceData], _run_isolated_signal(_run_test_child, (read,), timeout))


def run_signal_account_test(code: str, context: SignalAccountContext, timeout: float) -> SignalAssets:
    """单独执行开户草稿里的账户函数，并保留未知持仓与空仓的区别。"""
    read = partial(_read_account_function, code, context)
    return cast(SignalAssets, _run_isolated_signal(_run_test_child, (read,), timeout))


def run_signal(
    config: SignalAccountConfig,
    metadata: SignalAccountContext,
    code: str | None,
    timeout: float,
    settings: ContextSettings | None = None,
    *,
    size_weights: bool = False,
) -> SignalCalculation:
    """用与组合试跑相同的进程隔离策略，超时后终止计算。"""
    source = config.quote_source
    quote_code = source.code if source.kind == "custom" else None
    read_quotes = partial(_read_channel_quotes, source) if quote_code is None and code is not None else None
    args = (config.manual_assets, config.account_code, quote_code, metadata, code, settings, size_weights)
    return cast(SignalCalculation, _run_isolated_signal(_run_child, args, timeout, read_quotes))


def _run_isolated_signal(
    target: Callable[..., None],
    args: tuple[object, ...],
    timeout: float,
    read_quotes: QuoteReader | None = None,
) -> object:
    try:
        return run_portfolio_child(target, args, timeout, read_quotes)
    except TimeoutError as error:
        raise TimeoutError("调仓信号取数或计算超时") from error
    except (EOFError, BrokenPipeError) as error:
        raise ValueError("调仓信号计算进程异常退出") from error
