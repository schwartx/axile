"""用独立进程执行调仓信号的只读数据函数与组合。"""

from collections.abc import Callable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
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
from axile.common.serialization import copy_data, parse_input
from axile.executor.algorithms.utils.clock import clock_now_iso
from axile.executor.models.portfolio_target import PortfolioTarget, normalize_target_weights
from axile.executor.models.unified_account_assets import Position, PositionDirection, UnifiedAccountAssets
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
    follow_assets: UnifiedAccountAssets | None = None
    follow_ledger: dict[str, object] | None = None
    sizing_prices: dict[str, float] | None = None
    settlement_prices: dict[str, float] | None = None

    def snapshot_fields(self) -> dict[str, object]:
        """为信号快照保留原始口径、同轮换算的数量及用价。"""
        assert self.target is not None
        return {
            "target_mode": self.target.mode,
            "raw_weights": self.target.targets,
            "normalized_weights": self.target.targets if self.target.mode == "quantity" else self.normalized_weights,
            **({"signal_quantities": self.quantities} if self.target.mode == "weight" else {}),
            **({"sizing_prices": self.sizing_prices} if self.sizing_prices is not None else {}),
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

    def __init__(
        self, assets: UnifiedAccountAssets, code: str | None, pipe: Connection, *, reuse_quotes: bool = False
    ) -> None:
        self.assets = assets
        self.code = code
        self.reuse_quotes = reuse_quotes
        self.market = _MarketPortfolioExecutor(pipe)
        self._fetch: Callable[[object], object] | None = None
        self.quotes: dict[str, UnifiedPriceData] = {}

    def get_account_assets(self) -> UnifiedAccountAssets:
        return self.assets

    def get_market_data(self, symbols: list[str]) -> dict[str, UnifiedPriceData]:
        requested = symbols
        if self.reuse_quotes:
            symbols = [symbol for symbol in symbols if symbol not in self.quotes]
            if not symbols:
                return {symbol: self.quotes[symbol] for symbol in requested}
        if self.code is None:
            rows = self.market.get_market_data(symbols)
        else:
            if self._fetch is None:
                self._fetch = source_function(self.code, "fetch_quotes")
            rows = _custom_quotes(self._fetch, symbols)
        self.quotes.update(rows)
        return (
            {symbol: self.quotes[symbol] for symbol in requested if symbol in self.quotes}
            if self.reuse_quotes
            else rows
        )

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
) -> tuple[dict[str, float], dict[str, float], dict[str, float]]:
    weights = normalize_target_weights(
        targets, settings.long_leverage, settings.short_leverage, settings.weight_precision
    )
    active = [symbol for symbol, weight in weights.items() if weight]
    quantities = dict.fromkeys(weights, 0.0)
    prices: dict[str, float] = {}
    if not active:
        return weights, quantities, prices
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
        prices[symbol] = quote.last_price
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
    return weights, PortfolioTarget.validate_targets(quantities), prices


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _follow_prices(queries: _SignalQueries, positions: dict[str, float]) -> dict[str, Decimal]:
    missing = [symbol for symbol, quantity in positions.items() if quantity and symbol not in queries.quotes]
    if missing:
        queries.get_market_data(missing)
    prices = {}
    for symbol, quantity in positions.items():
        if not quantity:
            continue
        quote = queries.quotes.get(symbol)
        if quote is None or not isfinite(quote.last_price) or quote.last_price <= 0:
            raise ValueError(f"{symbol} 缺少可用于跟随记账的行情")
        prices[symbol] = Decimal(str(quote.last_price))
    return prices


def _valued_follow_assets(
    assets: UnifiedAccountAssets, quantities: dict[str, float], prices: dict[str, Decimal], cash: Decimal
) -> UnifiedAccountAssets:
    positions = [
        Position(
            symbol=symbol,
            direction=PositionDirection.LONG,
            volume=quantity,
            available_volume=0,
            market_value=float(_money(Decimal(str(quantity)) * prices[symbol])),
        )
        for symbol, quantity in quantities.items()
        if quantity
    ]
    market_value = sum((Decimal(str(position.market_value)) for position in positions), Decimal(0))
    return copy_data(
        assets,
        update={
            "total_asset": float(_money(cash + market_value)),
            "available_cash": float(cash),
            "market_value": float(market_value),
            "source": "signal_follow",
            "positions": positions,
            "extra": {"positions_known": True},
        },
    )


def _mark_follow_positions(queries: _SignalQueries) -> dict[str, float]:
    assets = queries.assets
    if assets.currency.strip().upper() != "CNY":
        raise ValueError("A 股跟随账本需要以 CNY 计价")
    if any(position.direction is PositionDirection.SHORT for position in assets.positions):
        raise ValueError("A 股跟随账本仅支持多头起始持仓")
    quantities: dict[str, float] = {}
    for position in assets.positions:
        quantities[position.symbol] = quantities.get(position.symbol, 0) + position.volume
    cash = _money(Decimal(str(assets.available_cash)))
    queries.assets = _valued_follow_assets(assets, quantities, _follow_prices(queries, quantities), cash)
    return quantities


def _settle_follow(
    result: SignalCalculation, queries: _SignalQueries, previous: dict[str, float], fee_bps: float
) -> None:
    targets = result.target_quantities()
    assets = result.assets
    status = "settled"
    fee = Decimal(0)
    cash = Decimal(str(assets.available_cash))
    positions = previous
    if any(quantity < 0 for quantity in targets.values()):
        status = "long_only"
    else:
        prices = _follow_prices(
            queries, previous | {symbol: quantity for symbol, quantity in targets.items() if quantity}
        )
        result.settlement_prices = {symbol: float(price) for symbol, price in prices.items()}
        changes = [
            (Decimal(str(targets.get(symbol, 0))) - Decimal(str(previous.get(symbol, 0)))) * price
            for symbol, price in prices.items()
        ]
        estimated_fee = _money(sum((abs(amount) for amount in changes), Decimal(0)) * Decimal(str(fee_bps)) / 10_000)
        remaining = _money(cash - sum(changes, Decimal(0)) - estimated_fee)
        if remaining < 0 and any(amount > 0 for amount in changes):
            status = "insufficient_cash"
        else:
            cash, fee, positions = remaining, estimated_fee, targets
            assets = _valued_follow_assets(assets, positions, prices, cash)
    result.follow_assets = assets
    result.follow_ledger = {
        "status": status,
        "positions": positions,
        "cash": assets.available_cash,
        "equity": assets.total_asset,
        "fee": float(fee),
        "fee_bps": fee_bps,
    }


def _run_child(
    pipe: Connection,
    manual_assets: SignalAssets | None,
    account_code: str | None,
    quote_code: str | None,
    metadata: SignalAccountContext,
    code: str | None,
    settings: ContextSettings | None = None,
    size_weights: bool = False,
    follow_fee_bps: float | None = None,
    settle_follow: bool = False,
) -> None:
    try:
        assets = _account_assets(manual_assets, account_code, metadata)
        queries = _SignalQueries(assets, quote_code, pipe, reuse_quotes=follow_fee_bps is not None)
        previous = _mark_follow_positions(queries) if follow_fee_bps is not None else {}
        result = SignalCalculation(queries.assets, None)
        if code is not None:
            context = _SignalContext(queries, settings=settings)
            result.target = calculate_portfolio_target(code, context)
            if size_weights and result.target.mode == "weight":
                result.normalized_weights, result.quantities, result.sizing_prices = _size_weight_target(
                    metadata["market"], result.target.targets, context.settings, queries
                )
            if settle_follow and follow_fee_bps is not None:
                _settle_follow(result, queries, previous, follow_fee_bps)
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
    follow_fee_bps: float | None = None,
    settle_follow: bool = False,
) -> SignalCalculation:
    """用与组合试跑相同的进程隔离策略，超时后终止计算。"""
    source = config.quote_source
    quote_code = source.code if source.kind == "custom" else None
    read_quotes = (
        partial(_read_channel_quotes, source)
        if quote_code is None and (code is not None or follow_fee_bps is not None)
        else None
    )
    args = (
        config.manual_assets,
        config.account_code,
        quote_code,
        metadata,
        code,
        settings,
        size_weights,
        follow_fee_bps,
        settle_follow,
    )
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
