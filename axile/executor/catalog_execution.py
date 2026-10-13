"""渠道共享的下单前资料屏障与执行固定副本。"""

from __future__ import annotations

from collections.abc import Mapping

from axile.domain.execution import ExecutionEventStatus, ExecutionEventType, ExecutionReasonFamily
from axile.executor.algorithms.utils.clock import clock_monotonic
from axile.executor.futures_combination import parse_combination_legs
from axile.executor.instrument_catalog import CatalogError, InstrumentCatalogProvider, LocalInstrumentCatalogProvider
from axile.executor.instrument_catalog_records import Instrument, normalize_catalog_symbol
from axile.executor.models.unified_input import UnifiedStandardInput


class CatalogExecutionMixin:
    """CTP/TQ 的公共准备步骤，不改变其他渠道。"""

    _catalog_requires_specs = False

    _instrument_provider: InstrumentCatalogProvider
    _catalog_records: dict[str, Instrument]
    _catalog_frozen: bool

    def _init_instrument_catalog(self) -> None:
        self._instrument_provider = LocalInstrumentCatalogProvider()
        self._catalog_records = {}
        self._catalog_frozen = False

    def set_instrument_catalog_provider(self, provider: InstrumentCatalogProvider) -> None:
        """连接前注入主服务共享目录。"""
        self._instrument_provider = provider

    def _catalog_checkpoint(self) -> None:
        getattr(self, "handle_termination_checkpoint")()

    def _install_catalog(self, rows: Mapping[str, Instrument]) -> None:
        raise NotImplementedError

    def _catalog_related_symbols(self) -> list[str]:
        raise NotImplementedError

    def _ensure_instrument_symbols(self, symbols: list[str], timeout: float = 15.0) -> None:
        """递归补齐期权标的及组合腿，并保留本次的独立引用。"""
        deadline = clock_monotonic() + timeout
        required = list(dict.fromkeys(symbols))
        while required:
            self._catalog_checkpoint()
            if self._catalog_frozen:
                missing = [s for s in required if self._catalog_record(s) is None]
                if missing:
                    raise CatalogError(f"执行固定资料中缺少合约: {', '.join(missing)}")
                return
            remaining = deadline - clock_monotonic()
            if remaining <= 0:
                raise CatalogError("等待合约资料超时", "CATALOG.TIMEOUT")
            rows = self._instrument_provider.ensure(
                required, self._catalog_checkpoint, remaining, require_specs=self._catalog_requires_specs
            )
            dependencies: list[str] = []
            for record in rows.values():
                underlying = str(record.extra.get("UnderlyingInstrID", "") or "")
                if record.kind == "OPTION" and underlying:
                    dependencies.append(f"{record.exchange}.{underlying.split('.')[-1]}")
                if record.kind == "COMBINE":
                    dependencies.extend(
                        f"{record.exchange}.{leg}" for leg in parse_combination_legs(record.symbol) or ()
                    )
            available = {alias for row in rows.values() for alias in (row.key, row.symbol)}
            missing = [s for s in dict.fromkeys(dependencies) if s not in available]
            if missing:
                required = list(dict.fromkeys([*required, *missing]))
                continue
            self._catalog_records.update({row.key: row for row in rows.values()})
            self._install_catalog(rows)
            return

    def _catalog_record(self, symbol: str) -> Instrument | None:
        normalized = normalize_catalog_symbol(symbol)
        matches = [r for r in self._catalog_records.values() if normalized in {r.key, r.symbol}]
        if len(matches) > 1:
            raise CatalogError(f"合约代码存在交易所歧义: {symbol}", "CATALOG.AMBIGUOUS")
        if matches and matches[0].expired():
            raise CatalogError(f"合约已下市: {symbol}", "CATALOG.EXPIRED")
        return matches[0] if matches else None

    def _catalog_event(
        self, event_type: ExecutionEventType, symbols: list[str], started: float, error: Exception | None = None
    ) -> None:
        runtime = getattr(self, "get_active_execution_runtime")()
        if runtime is None:
            return
        fallback = "CATALOG.TIMEOUT" if isinstance(error, TimeoutError) else "CATALOG.UNAVAILABLE"
        if error is None:
            fallback = "CATALOG.WAITING" if event_type is ExecutionEventType.INSTRUMENTS_WAITING else "CATALOG.READY"
        runtime.emit_audit_event(
            event_type=event_type,
            status=ExecutionEventStatus.ERROR if error else ExecutionEventStatus.INFO,
            reason_family=ExecutionReasonFamily.SYSTEM,
            reason_code=getattr(error, "reason_code", None) or fallback,
            details={
                "symbols": symbols,
                "elapsed_seconds": clock_monotonic() - started,
                "message": str(error)
                if error
                else "等待合约资料"
                if event_type is ExecutionEventType.INSTRUMENTS_WAITING
                else "合约资料就绪",
            },
        )

    def _prepare_instrument_catalog(self, standard_input: UnifiedStandardInput) -> None:
        """所有必要资料齐全后才允许 planning；取消及失败不发送订单。"""
        self._catalog_frozen = False
        self._catalog_records = {}
        symbols = []
        for values in (
            standard_input.curr_target,
            standard_input.last_target,
            standard_input.trade_rules,
            standard_input.symbol_algorithms,
            standard_input.forbidden_symbols,
            standard_input.risk_symbols,
        ):
            symbols.extend(values)
        started = clock_monotonic()
        self._catalog_prepare_deadline = started + 15.0
        self._catalog_event(ExecutionEventType.INSTRUMENTS_WAITING, symbols, started)
        try:
            symbols.extend(self._catalog_related_symbols())
            self._ensure_instrument_symbols(symbols, max(0.0, 15.0 - (clock_monotonic() - started)))
            self._catalog_checkpoint()
            self._catalog_frozen = True
        except Exception as exc:
            self._catalog_event(ExecutionEventType.INSTRUMENTS_FAILED, symbols, started, exc)
            raise
        finally:
            self._catalog_prepare_deadline = None
        self._catalog_event(ExecutionEventType.INSTRUMENTS_READY, symbols, started)

    def _release_instrument_catalog(self) -> None:
        """执行结束后解除固定资料，不清空主服务缓存。"""
        self._catalog_frozen = False
        self._catalog_prepare_deadline = None
