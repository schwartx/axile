"""TqSdk 完整代码与 Axile 通用合约代码之间的映射."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TypeVar

from axile.channels.cn_futures import canonicalize_cn_futures_symbol, czce_is_option_instrument
from axile.executor.algorithms.utils.clock import clock_now
from axile.executor.models.unified_input import UnifiedStandardInput

_TRADE_CLASSES = frozenset({"FUTURE", "OPTION", "COMBINE"})
_TRADE_EXCHANGES = frozenset({"CFFEX", "SHFE", "DCE", "CZCE", "INE", "GFEX"})

_ValueT = TypeVar("_ValueT")


@dataclass(frozen=True, slots=True)
class TQInstrument:
    """一条不可变的天勤合约目录记录."""

    symbol: str
    instrument_id: str
    exchange_id: str
    ins_class: str
    expired: bool = False

    @property
    def tradable(self) -> bool:
        """返回该记录是否属于 v1 可交易的国内衍生品."""
        return not self.expired and self.ins_class in _TRADE_CLASSES and self.exchange_id in _TRADE_EXCHANGES


class TQSymbolResolver:
    """解析 CTP InstrumentID，并对未知和重名代码显式失败."""

    def __init__(
        self,
        instruments: list[TQInstrument],
        *,
        reference_year: int | None = None,
        loader: Callable[[str], list[TQInstrument]] | None = None,
    ) -> None:
        self._by_full: dict[str, TQInstrument] = {item.symbol: item for item in instruments}
        by_local: dict[str, list[TQInstrument]] = {}
        for item in instruments:
            by_local.setdefault(item.instrument_id, []).append(item)
        self._by_local: dict[str, list[TQInstrument]] = by_local
        self._reference_year: int = reference_year or clock_now().year
        self._loader = loader

    def _load_missing(self, symbol: str) -> None:
        if self._loader is None:
            return
        for item in self._loader(symbol):
            self._by_full[item.symbol] = item
            current = self._by_local.setdefault(item.instrument_id, [])
            current[:] = [old for old in current if old.symbol != item.symbol]
            current.append(item)

    def _czce_alias_candidates(self, symbol: str) -> list[TQInstrument]:
        exchange_id: str | None = None
        local_symbol = symbol
        if "." in symbol:
            exchange_id, local_symbol = symbol.split(".", 1)
            if exchange_id != "CZCE":
                return []
        native_symbol = canonicalize_cn_futures_symbol(local_symbol, reference_year=self._reference_year)
        if native_symbol == local_symbol:
            return []
        want_class = "OPTION" if czce_is_option_instrument(native_symbol) else "FUTURE"
        return [
            item
            for item in self._by_local.get(native_symbol, [])
            if item.exchange_id == "CZCE" and item.ins_class == want_class
        ]

    @staticmethod
    def _select(symbol: str, candidates: list[TQInstrument]) -> TQInstrument:
        if not candidates:
            raise ValueError(f"天勤合约目录中不存在品种: {symbol}")
        live = [item for item in candidates if not item.expired]
        selected = live or candidates
        if len(selected) != 1:
            names = ", ".join(sorted(item.symbol for item in selected))
            raise ValueError(f"品种 {symbol} 对应多个天勤代码，无法自动选择: {names}")
        return selected[0]

    def to_tq(self, symbol: str, *, for_trade: bool = False) -> str:
        """将通用 symbol 转换为 TqSdk 完整代码."""
        if symbol not in self._by_full and symbol not in self._by_local and not self._czce_alias_candidates(symbol):
            self._load_missing(symbol)
        if "." in symbol:
            item = self._by_full.get(symbol)
            if item is None:
                aliases = self._czce_alias_candidates(symbol)
                item = self._select(symbol, aliases) if aliases else None
            if item is None:
                if for_trade:
                    raise ValueError(f"品种 {symbol} 不属于当前 TqSdk 账户可交易的国内衍生品")
                return symbol
            if for_trade and not item.tradable:
                raise ValueError(f"品种 {item.symbol} 仅支持行情查询，不能下单")
            return item.symbol
        candidates = self._by_local.get(symbol, []) or self._czce_alias_candidates(symbol)
        item = self._select(symbol, candidates)
        if for_trade and not item.tradable:
            raise ValueError(f"品种 {item.symbol} 仅支持行情查询，不能下单")
        return item.symbol

    def to_axile(self, symbol: str) -> str:
        """将 TqSdk 完整代码转换为通用代码；非衍生品保留完整代码."""
        item = self._by_full.get(symbol)
        if item is not None and item.ins_class in _TRADE_CLASSES:
            return item.instrument_id
        return symbol

    def instrument(self, symbol: str) -> TQInstrument | None:
        """返回通用或完整代码对应的目录记录."""
        try:
            tq_symbol = self.to_tq(symbol)
            return self._by_full.get(tq_symbol)
        except ValueError:
            return None

    def normalize_input(self, standard_input: UnifiedStandardInput) -> UnifiedStandardInput:
        """复制并规范化标准输入中的全部 symbol 字段."""
        return standard_input.model_copy(
            update={
                "curr_target": self._normalize_mapping("curr_target", standard_input.curr_target),
                "last_target": self._normalize_mapping("last_target", standard_input.last_target),
                "symbol_algorithms": self._normalize_mapping("symbol_algorithms", standard_input.symbol_algorithms),
                "trade_rules": self._normalize_mapping("trade_rules", standard_input.trade_rules),
                "forbidden_symbols": self._normalize_list(standard_input.forbidden_symbols),
                "risk_symbols": self._normalize_list(standard_input.risk_symbols),
            }
        )

    def _normalize_mapping(self, field: str, values: Mapping[str, _ValueT]) -> dict[str, _ValueT]:
        normalized: dict[str, _ValueT] = {}
        sources: dict[str, str] = {}
        for raw_symbol, value in values.items():
            symbol = self.to_axile(self.to_tq(raw_symbol))
            if symbol in normalized and normalized[symbol] != value:
                raise ValueError(
                    f"TQ 输入字段 {field} 中的代码 {sources[symbol]} 与 {raw_symbol} 都对应 {symbol}，但配置不一致"
                )
            normalized[symbol] = value
            if symbol not in sources:
                sources[symbol] = raw_symbol
        return normalized

    def _normalize_list(self, values: list[str]) -> list[str]:
        return list(dict.fromkeys(self.to_axile(self.to_tq(symbol)) for symbol in values))


__all__ = ["TQInstrument", "TQSymbolResolver"]
