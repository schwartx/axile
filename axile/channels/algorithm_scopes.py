"""按渠道校验账户算法品种，并将实际合约归入同一品种。"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, TypeVar

from axile.executor.ctp_product_sessions import CTP_PRODUCT_SESSIONS

if TYPE_CHECKING:
    from axile.channels.contracts import ChannelPlugin

_Value = TypeVar("_Value")
FUTURES_SCOPE_PATTERN = r"(?:[A-Za-z]+\.)?[A-Za-z]+"
GM_SCOPE_PATTERN = r"(?:[0-9]{6}\.(?:SH|SZ|BJ)|(?:SHSE|SZSE|BJSE)\.[0-9]{6})"


def futures_algorithm_scope(symbol: str) -> str:
    """将期货品种或月份合约归入交易所限定的品种，排除期权和组合。"""
    match = re.fullmatch(r"(?:(?P<exchange>[A-Za-z]+)\.)?(?P<product>[A-Za-z]+)(?:[0-9]{3,4})?", symbol.strip())
    if match is None:
        raise ValueError(f"不是期货品种或月份合约: {symbol}")
    exchange = (match.group("exchange") or "").upper()
    product = match.group("product").lower()
    matches = [
        (market, code)
        for market, code in CTP_PRODUCT_SESSIONS
        if code.lower() == product and (not exchange or market == exchange)
    ]
    if len(matches) != 1:
        raise ValueError(f"期货品种未知或交易所不明确: {symbol}")
    market, code = matches[0]
    return f"{market}.{code}"


def gm_algorithm_scope(symbol: str) -> str:
    """复用掘金代码转换，统一使用 Axile 的完整证券代码。"""
    from axile.common.gm_symbols import GM_SYMBOL_RESOLVER

    native = GM_SYMBOL_RESOLVER.to_gm(symbol.strip().upper())
    return GM_SYMBOL_RESOLVER.to_axile(native)


def uppercase_algorithm_scope(symbol: str) -> str:
    """将大小写不敏感渠道的完整标的代码统一为大写。"""
    return symbol.strip().upper()


def normalize_algorithm_scopes(plugin: ChannelPlugin, values: dict[str, _Value]) -> dict[str, _Value]:
    """按渠道描述校验配置键，并拒绝不同写法指向同一品种。"""
    capability = plugin.descriptor.algorithm_scope
    if values and capability is None:
        raise ValueError(f"{plugin.descriptor.label} 尚未声明品种算法代码格式")
    result = {}
    for raw, value in values.items():
        if capability is None or len(raw) > 80 or re.fullmatch(capability.pattern, raw.strip(), re.IGNORECASE) is None:
            raise ValueError(f"品种代码格式不符合 {plugin.descriptor.label} 渠道: {raw}")
        key = plugin.algorithm_scope_key(raw)
        if key in result:
            raise ValueError(f"品种算法配置重复: {raw} 对应 {key}")
        result[key] = value
    return result
