"""GM 原生证券代码与 Axile 通用代码之间的映射."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import TypeVar

from axile.common.serialization import copy_data
from axile.executor.models.unified_input import UnifiedStandardInput

_AXILE_TO_GM_EXCHANGE = {"SH": "SHSE", "SZ": "SZSE", "BJ": "BJSE"}
_GM_TO_AXILE_EXCHANGE = {value: key for key, value in _AXILE_TO_GM_EXCHANGE.items()}
_AXILE_PATTERN = re.compile(r"^(?P<code>\d{6})\.(?P<exchange>SH|SZ|BJ)$")
_GM_PATTERN = re.compile(r"^(?P<exchange>SHSE|SZSE|BJSE)\.(?P<code>\d{6})$")

_ValueT = TypeVar("_ValueT")


class GMSymbolResolver:
    """在 Axile A 股代码与 GM 原生代码之间执行无状态转换."""

    def to_gm(self, symbol: str) -> str:
        """将受支持的通用或 GM symbol 转换为 GM 原生格式."""
        gm_match = _GM_PATTERN.fullmatch(symbol)
        if gm_match is not None:
            return symbol

        axile_match = _AXILE_PATTERN.fullmatch(symbol)
        if axile_match is None:
            raise ValueError(f"GM 股票代码格式或交易所不受支持: {symbol}")

        exchange = _AXILE_TO_GM_EXCHANGE[axile_match.group("exchange")]
        return f"{exchange}.{axile_match.group('code')}"

    def to_axile(self, symbol: str) -> str:
        """将已知 GM A 股代码转为通用格式，未知代码保持原样."""
        axile_match = _AXILE_PATTERN.fullmatch(symbol)
        if axile_match is not None:
            return symbol

        gm_match = _GM_PATTERN.fullmatch(symbol)
        if gm_match is None:
            return symbol

        exchange = _GM_TO_AXILE_EXCHANGE[gm_match.group("exchange")]
        return f"{gm_match.group('code')}.{exchange}"

    def normalize_input(self, standard_input: UnifiedStandardInput) -> UnifiedStandardInput:
        """复制并规范化标准输入中的全部 symbol 字段."""
        return copy_data(
            standard_input,
            update={
                "curr_target": self._normalize_mapping("curr_target", standard_input.curr_target),
                "last_target": self._normalize_mapping("last_target", standard_input.last_target),
                "symbol_algorithms": self._normalize_mapping("symbol_algorithms", standard_input.symbol_algorithms),
                "trade_rules": self._normalize_mapping("trade_rules", standard_input.trade_rules),
                "forbidden_symbols": self._normalize_list(standard_input.forbidden_symbols),
                "risk_symbols": self._normalize_list(standard_input.risk_symbols),
            },
        )

    def _normalize_mapping(self, field: str, values: Mapping[str, _ValueT]) -> dict[str, _ValueT]:
        normalized: dict[str, _ValueT] = {}
        sources: dict[str, str] = {}
        for raw_symbol, value in values.items():
            symbol = self.to_axile(self.to_gm(raw_symbol))
            if symbol in normalized and normalized[symbol] != value:
                raise ValueError(
                    f"GM 输入字段 {field} 中的代码 {sources[symbol]} 与 {raw_symbol} 都对应 {symbol}，但配置不一致"
                )
            normalized[symbol] = value
            sources.setdefault(symbol, raw_symbol)
        return normalized

    def _normalize_list(self, values: list[str]) -> list[str]:
        return list(dict.fromkeys(self.to_axile(self.to_gm(symbol)) for symbol in values))


GM_SYMBOL_RESOLVER = GMSymbolResolver()


def normalize_gm_standard_input(standard_input: UnifiedStandardInput) -> UnifiedStandardInput:
    """返回使用 Axile A 股代码的 GM 标准输入副本."""
    return GM_SYMBOL_RESOLVER.normalize_input(standard_input)


__all__ = ["GMSymbolResolver", "GM_SYMBOL_RESOLVER", "normalize_gm_standard_input"]
