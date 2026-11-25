"""掘金行情进程只配置数据访问令牌和终端地址。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from math import isfinite
from multiprocessing import get_context
from multiprocessing.connection import Connection
from multiprocessing.context import SpawnContext, SpawnProcess
from pathlib import Path
from threading import Lock
from typing import cast

from axile.channels.market_data import MarketDataStatus, MarketDataUnavailable
from axile.common.gm_symbols import GM_SYMBOL_RESOLVER
from axile.executor.algorithms.utils.clock import clock_monotonic
from axile.executor.gm_terminal import gm_rpc_port, launch_gm_terminal, wait_for_gm_port
from axile.executor.models.unified_input_accounts import GMAccountConfig
from axile.executor.models.unified_price import UnifiedPriceData


@dataclass(frozen=True, slots=True)
class GMMarketDataConfig:
    """同一掘金终端和令牌可以复用行情进程。"""

    token: str = field(repr=False)
    address: str | None
    terminal_path: str | None = None

    @classmethod
    def from_account_config(
        cls, values: Mapping[str, object], *, terminal_path: str | None = None
    ) -> GMMarketDataConfig:
        """提取行情 SDK 配置，不读取交易账户 ID。"""
        token = values.get("token")
        mode = values.get("connection_mode")
        address = values.get("serv_addr")
        if not isinstance(token, str):
            raise ValueError("缺少掘金行情令牌")
        if mode not in ("terminal", "service") or mode == "service" and not isinstance(address, str):
            raise ValueError("缺少掘金行情终端设置")
        return cls(
            GMAccountConfig.validate_required_field(token, "token"),
            GMAccountConfig.validate_service_address(cast(str, address)) if mode == "service" else None,
            GMAccountConfig.validate_terminal_path(terminal_path) if mode == "terminal" else None,
        )

    def open(self) -> GMMarketData:
        """准备独立行情源，首次读取时启动子进程。"""
        return GMMarketData(self)


def _gm_quotes_worker(pipe: Connection, config: GMMarketDataConfig) -> None:
    """SDK 令牌及地址是进程全局状态，每个数据源单独持有。"""
    try:
        from gm.api import current, set_serv_addr, set_token  # type: ignore[import-untyped]

        from axile.executor.gm.common import from_gm_current

        set_token(config.token)
        if config.address:
            set_serv_addr(config.address)
        while True:
            symbols = pipe.recv()
            try:
                rows = current(symbols=symbols, include_call_auction=True)
            except Exception:
                pipe.send(None)
                continue
            quotes: dict[str, UnifiedPriceData] = {}
            for row in rows or ():
                try:
                    quote = from_gm_current(dict(row))
                    if isfinite(quote.last_price) and quote.last_price > 0 and quote.timestamp > 0:
                        quotes[str(quote.extra["gm_symbol"])] = quote
                except (KeyError, TypeError, ValueError, OverflowError, AttributeError):
                    continue
            pipe.send(quotes)
    except (EOFError, OSError, ImportError):
        pass
    finally:
        pipe.close()


class GMMarketData:
    """在单独子进程中执行有界行情请求。"""

    def __init__(self, config: GMMarketDataConfig) -> None:
        self._config = config
        self._io = Lock()
        self._process: SpawnProcess | None = None
        self._pipe: Connection | None = None
        self._status: MarketDataStatus = "uninitialized"

    def status(self) -> MarketDataStatus:
        """读取行情子进程的最近状态。"""
        process = self._process
        if process is not None:
            try:
                if process.exitcode is not None:
                    return "unavailable"
            except ValueError:
                # 另一线程已回收刚才读取的进程。
                pass
        return self._status

    def snapshot(
        self, symbols: Sequence[str], *, timeout: float, allow_partial: bool = False
    ) -> dict[str, UnifiedPriceData]:
        """读取掘金 current，诊断查询可保留已返回的部分标的。"""
        native = {symbol: GM_SYMBOL_RESOLVER.to_gm(symbol) for symbol in symbols}
        deadline = clock_monotonic() + timeout
        if not self._io.acquire(timeout=max(0.0, timeout)):
            raise TimeoutError("等待掘金行情查询超时")
        try:
            quotes = self._read(native, deadline)
            if not allow_partial and (missing := [symbol for symbol in native if symbol not in quotes]):
                raise MarketDataUnavailable(f"掘金暂无行情：{', '.join(missing)}")
            return quotes
        finally:
            self._io.release()

    def _read(self, native: dict[str, str], deadline: float) -> dict[str, UnifiedPriceData]:
        if self._status == "disconnected":
            raise MarketDataUnavailable("掘金行情源已关闭")
        if self._process is None or not self._process.is_alive():
            self._stop()
            self._status = "connecting"
        try:
            pipe = self._pipe if self._pipe is not None else self._start(deadline)
            pipe.send(list(dict.fromkeys(native.values())))
            if not pipe.poll(max(0.0, deadline - clock_monotonic())):
                raise TimeoutError("掘金行情查询超时")
            rows = pipe.recv()
        except TimeoutError:
            self._status = "unavailable"
            self._stop()
            raise
        except MarketDataUnavailable:
            self._status = "unavailable"
            raise
        except (EOFError, OSError, RuntimeError):
            self._status = "unavailable"
            self._stop()
            raise MarketDataUnavailable("掘金行情进程不可用") from None
        if rows is None:
            self._status = "unavailable"
            raise MarketDataUnavailable("掘金行情查询失败，请检查终端和行情权限")
        self._status = "ready"
        return {symbol: rows[code] for symbol, code in native.items() if code in rows}

    def _start(self, deadline: float) -> Connection:
        config = self._config
        if config.terminal_path is not None:
            directory = Path(config.terminal_path)
            try:
                port = gm_rpc_port(directory)
                if not wait_for_gm_port(port, 0.1):
                    launch_gm_terminal(directory)
                    if not wait_for_gm_port(port, deadline - clock_monotonic()):
                        raise TimeoutError("掘金本机终端仍在启动，请就绪后重试")
            except TimeoutError:
                raise
            except (OSError, ValueError, KeyError) as exc:
                raise MarketDataUnavailable(f"准备掘金本机终端失败：{exc}") from exc
            config = replace(config, address=f"127.0.0.1:{port}")
        context = cast(SpawnContext, get_context("spawn"))
        parent, child = cast("tuple[Connection, Connection]", context.Pipe())
        process = context.Process(target=_gm_quotes_worker, args=(child, config), daemon=True)
        try:
            process.start()
        except BaseException:
            parent.close()
            process.close()
            raise
        finally:
            child.close()
        self._process, self._pipe = process, parent
        return parent

    def _stop(self) -> None:
        pipe, self._pipe = self._pipe, None
        process, self._process = self._process, None
        if pipe is not None:
            pipe.close()
        if process is None:
            return
        process.join(0.15)
        if process.is_alive():
            process.kill()
            process.join()
        process.close()

    def close(self) -> None:
        """结束独立行情子进程。"""
        with self._io:
            self._status = "disconnected"
            self._stop()
