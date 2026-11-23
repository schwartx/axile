"""用独立进程执行调仓信号的只读数据函数与数量组合。"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from multiprocessing import get_context
from multiprocessing.connection import Connection
from typing import cast

from axile.channels.rebalance_signal import SignalAccountConfig, SignalAssets
from axile.common.function_contract import accepts_context
from axile.common.serialization import parse_input
from axile.executor.algorithms.utils.clock import clock_now_iso
from axile.executor.models.portfolio_target import PortfolioTarget
from axile.executor.models.unified_account_assets import Position, UnifiedAccountAssets
from axile.executor.models.unified_order import TradeRecord, UnifiedOrder
from axile.executor.models.unified_price import UnifiedPriceData
from axile.server.context import Context
from axile.server.portfolio_function import PortfolioFunctionError, _error_from_exception, calculate_portfolio_target
from axile.server.portfolio_runner import PORTFOLIO_FUNCTION_IPC_GRACE_SECONDS, _stop_process


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


def _account_assets(config: SignalAccountConfig, context: dict[str, object]) -> UnifiedAccountAssets:
    assets = config.manual_assets
    if config.account_code:
        fetch = source_function(config.account_code, "fetch_account")
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
        source="signal_function" if config.account_code else "signal_manual",
        extra={"positions_known": assets.positions is not None},
    )


class _SignalQueries:
    """只提供组合计算所需的数据访问，行情按组合请求读取。"""

    def __init__(self, assets: UnifiedAccountAssets, code: str) -> None:
        self.assets = assets
        self.code = code
        self._fetch: Callable[[object], object] | None = None

    def get_account_assets(self) -> UnifiedAccountAssets:
        return self.assets

    def get_market_data(self, symbols: list[str]) -> dict[str, UnifiedPriceData]:
        if self._fetch is None:
            self._fetch = source_function(self.code, "fetch_quotes")
        values = self._fetch(symbols)
        if not isinstance(values, dict):
            raise ValueError("行情函数必须返回按标的索引的字典")
        return {symbol: _price(symbol, values.get(symbol)) for symbol in symbols}

    def get_pending_orders(self, symbol: str | None = None) -> list[UnifiedOrder]:
        return []

    def query_trades(self, symbol: str, order_id: str) -> list[TradeRecord]:
        return []


def _price(symbol: str, value: object) -> UnifiedPriceData:
    if not isinstance(value, dict):
        raise ValueError(f"缺少行情：{symbol}")
    price, update_time = value.get("last_price"), value.get("update_time")
    if isinstance(price, bool) or not isinstance(price, (int, float)) or not isfinite(price) or price <= 0:
        raise ValueError(f"行情价格无效：{symbol}")
    if not isinstance(update_time, str):
        raise ValueError(f"行情缺少时间：{symbol}")
    observed = datetime.fromisoformat(update_time)
    if observed.utcoffset() is None:
        raise ValueError(f"行情时间需要时区：{symbol}")
    return UnifiedPriceData(
        symbol=symbol,
        last_price=float(price),
        bids=[],
        asks=[],
        volume=0,
        update_time=observed.isoformat(),
        timestamp=int(observed.timestamp() * 1000),
    )


class _SignalContext(Context):
    @property
    def positions(self) -> list[Position]:
        if self.account.extra["positions_known"] is False:
            raise ValueError("该账户尚未提供实际持仓")
        return self.account.positions


def _run_child(
    pipe: Connection,
    config: SignalAccountConfig,
    metadata: dict[str, object],
    code: str | None,
) -> None:
    try:
        assets = _account_assets(config, metadata)
        target = None
        if code is not None:
            target = calculate_portfolio_target(code, _SignalContext(_SignalQueries(assets, config.quote_code)))
            if target.mode != "quantity":
                raise ValueError("调仓信号的组合函数必须返回数量模式")
        pipe.send(SignalCalculation(assets, target))
    except BaseException as error:  # noqa: BLE001 - 子进程负责回传用户函数错误
        pipe.send(error if isinstance(error, PortfolioFunctionError) else _error_from_exception(error))
    finally:
        pipe.close()


def run_signal(
    config: SignalAccountConfig,
    metadata: dict[str, object],
    code: str | None,
    timeout: float,
) -> SignalCalculation:
    """用与组合试跑相同的进程隔离策略，超时后终止计算。"""
    context = get_context("spawn")
    reader, writer = context.Pipe(duplex=False)
    process = context.Process(target=_run_child, args=(writer, config, metadata, code), daemon=True)
    try:
        process.start()
        writer.close()
        if not reader.poll(timeout):
            raise TimeoutError("调仓信号取数或计算超时")
        result = reader.recv()
        if isinstance(result, PortfolioFunctionError):
            raise result
        return cast(SignalCalculation, result)
    except EOFError as error:
        raise ValueError("调仓信号计算进程异常退出") from error
    finally:
        reader.close()
        writer.close()
        if process.pid is not None:
            process.join(timeout=PORTFOLIO_FUNCTION_IPC_GRACE_SECONDS)
            _stop_process(process)
        process.close()
