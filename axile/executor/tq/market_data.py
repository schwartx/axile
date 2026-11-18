"""使用快期登录、通过独立天勤运行时读取行情。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from threading import Lock

from axile.channels.market_data import MarketDataStatus, MarketDataUnavailable
from axile.executor.algorithms.utils.clock import clock_monotonic
from axile.executor.instrument_catalog_records import normalize_catalog_symbol
from axile.executor.models.unified_input_accounts import TQAccountConfig
from axile.executor.models.unified_price import UnifiedPriceData
from axile.executor.tq.converters import quote_to_unified
from axile.executor.tq.runtime import TQRuntime
from axile.executor.tq.symbols import TQInstrument, TQSymbolResolver


@dataclass(frozen=True, slots=True)
class TQMarketDataConfig:
    """仅保存快期登录信息。"""

    username: str = field(repr=False)
    password: str = field(repr=False)

    @classmethod
    def from_account_config(cls, values: Mapping[str, object]) -> TQMarketDataConfig:
        """从已保存设置提取行情使用的快期凭据。"""
        username, password = values.get("tq_username"), values.get("tq_password")
        if not isinstance(username, str) or not isinstance(password, str):
            raise ValueError("缺少快期行情登录配置")
        return cls(
            TQAccountConfig.validate_tq_credential(username, "tq_username"),
            TQAccountConfig.validate_tq_credential(password, "tq_password"),
        )

    def build_api(self) -> object:
        """使用 SDK 本地占位账户，禁止登录期货资金账户或云端模拟账户。"""
        from tqsdk import TqApi, TqAuth, TqSim

        return TqApi(account=TqSim(), auth=TqAuth(self.username, self.password))

    def open(self) -> TQMarketData:
        """创建独立行情源。"""
        return TQMarketData(self)


class TQMarketData:
    """复用行情事件泵，断线期间撤销旧快照。"""

    def __init__(self, config: TQMarketDataConfig) -> None:
        self._config = config
        self._io = Lock()
        self._runtime: TQRuntime | None = None
        self._state: MarketDataStatus = "uninitialized"

    def status(self) -> MarketDataStatus:
        """只读行情前置就绪状态。"""
        runtime = self._runtime
        if runtime is not None:
            if not runtime.is_alive():
                return "unavailable"
            if runtime.startup_pending:
                return "connecting"
            return "ready" if runtime.market_synchronized else "disconnected"
        return self._state

    def snapshot(self, symbols: Sequence[str], *, timeout: float) -> dict[str, UnifiedPriceData]:
        """等待指定合约的行情快照。"""
        deadline = clock_monotonic() + timeout
        if not self._io.acquire(timeout=max(0.0, timeout)):
            raise TimeoutError("等待天勤行情查询超时")
        try:
            runtime = self._get_runtime(deadline)
            return self._read(runtime, symbols, deadline)
        finally:
            self._io.release()

    def _get_runtime(self, deadline: float) -> TQRuntime:
        if self._state == "disconnected":
            raise MarketDataUnavailable("天勤行情源已关闭")
        if self._runtime is None or not self._runtime.is_alive():
            self._runtime = TQRuntime(self._config.build_api, market_only=True, wait_for_startup=False)
        try:
            self._runtime.wait_ready(self._remaining(deadline))
        except RuntimeError:
            raise MarketDataUnavailable("快期行情登录失败") from None
        return self._runtime

    def _read(self, runtime: TQRuntime, symbols: Sequence[str], deadline: float) -> dict[str, UnifiedPriceData]:
        bare = [symbol for symbol in symbols if not symbol.partition(".")[0].isalpha() or "." not in symbol]
        try:
            records = runtime.query_catalog(bare, self._remaining(deadline)) if bare else []
            resolver = TQSymbolResolver([TQInstrument(row.key, row.symbol, row.exchange, row.kind) for row in records])
            native = {
                symbol: resolver.to_tq(symbol) if symbol in bare else normalize_catalog_symbol(symbol)
                for symbol in symbols
            }
            rows = runtime.quote_snapshots(list(native.values()), timeout=self._remaining(deadline))
        except RuntimeError:
            raise MarketDataUnavailable("天勤行情查询失败") from None
        if any(code not in rows for code in native.values()):
            self._remaining(deadline)
            raise MarketDataUnavailable("天勤未收到所需行情")
        result = {symbol: quote_to_unified(rows[code], resolver) for symbol, code in native.items()}
        for quote in result.values():
            if quote.last_price <= 0 or quote.timestamp <= 0:
                raise MarketDataUnavailable(f"天勤暂无 {quote.symbol} 的有效报价")
            quote.extra.update(source="tq.subscription", time_basis="exchange")
        return result

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - clock_monotonic()
        if remaining <= 0:
            raise TimeoutError("等待天勤行情查询超时")
        return remaining

    def close(self) -> None:
        """释放独立行情事件泵。"""
        with self._io:
            self._state = "disconnected"
            runtime, self._runtime = self._runtime, None
            if runtime is not None:
                runtime.close()
