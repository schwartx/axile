"""跨 CTP/TQ 的不可变合约基础资料，不包含行情或账户权限。"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any
from zoneinfo import ZoneInfo

from axile.channels.cn_futures import canonicalize_cn_futures_symbol
from axile.executor.algorithms.utils.clock import clock_now

EXCHANGES = ("CFFEX", "SHFE", "DCE", "CZCE", "INE", "GFEX")
_CTP_CLASSES = {"1": "FUTURE", "2": "OPTION", "3": "COMBINE", "6": "OPTION"}
_SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class InstrumentRecord:
    """现有 CTP 转换器使用的原生字段只读视图。"""

    values: Mapping[str, object]

    def __getattr__(self, name: str) -> Any:
        """保持现有原生字段属性读取契约。"""
        try:
            return self.values[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


def _number(value: object) -> float:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("合约基础规格须为数值")
    return float(value or 0)


def normalize_catalog_symbol(symbol: str) -> str:
    """保留交易所限定，规范郑商所年份别名。"""
    exchange, separator, local = symbol.partition(".")
    if not separator:
        local, exchange = exchange, ""
    local = canonicalize_cn_futures_symbol(local, reference_year=clock_now(tz=_SHANGHAI).year)
    return f"{exchange}.{local}" if exchange else local


@dataclass(frozen=True, slots=True)
class Instrument:
    """统一合约记录；类型扩展只保留静态字段。"""

    exchange: str
    symbol: str
    kind: str
    multiplier: float | None = None
    tick: float | None = None
    expiry: str = ""
    extra: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """冻结扩展字段，防止执行副本修改共享记录。"""
        object.__setattr__(self, "extra", MappingProxyType(dict(self.extra)))

    @property
    def key(self) -> str:
        """返回无歧义的目录键。"""
        return f"{self.exchange}.{self.symbol}"

    def expired(self) -> bool:
        """日期型到期信息在到期日结束后才失效。"""
        now = clock_now(tz=_SHANGHAI)
        return bool(self.expiry and self.expiry < now.strftime("%Y%m%d"))

    @property
    def has_specs(self) -> bool:
        """CTP 使用目录前需要完整的乘数和价格步长。"""
        return all(value is not None and math.isfinite(value) and value > 0 for value in (self.multiplier, self.tick))

    def validate(self, *, require_specs: bool = False) -> None:
        """目录保留原生零值规格；执行需要时单独检查正数规格。"""
        if self.exchange not in EXCHANGES or not self.symbol or self.kind not in {"FUTURE", "OPTION", "COMBINE"}:
            raise ValueError(f"无效合约标识或类型: {self.key}")
        if (require_specs and not self.has_specs) or any(
            value is not None and (not math.isfinite(value) or value < 0) for value in (self.multiplier, self.tick)
        ):
            raise ValueError(f"合约基础规格缺失: {self.key}")
        if self.expired():
            raise ValueError(f"合约已下市: {self.key}")

    def wire(self) -> dict[str, object]:
        """生成仅用于受信任 worker IPC 的普通数据。"""
        return {
            "exchange": self.exchange,
            "symbol": self.symbol,
            "kind": self.kind,
            "multiplier": self.multiplier,
            "tick": self.tick,
            "expiry": self.expiry,
            "extra": dict(self.extra),
        }

    @classmethod
    def from_wire(cls, row: Mapping) -> Instrument:
        """还原记录并冻结扩展字段。"""
        values = dict(row)
        values["extra"] = MappingProxyType(dict(values.get("extra", {})))
        return cls(**values)

    def ctp(self) -> InstrumentRecord:
        """适配现有 CTP 转换器的字段读取契约。"""
        return InstrumentRecord(
            MappingProxyType(
                {
                    **self.extra,
                    "InstrumentID": self.symbol,
                    "ExchangeID": self.exchange,
                    "ProductClass": self.extra.get(
                        "ProductClass", {"FUTURE": "1", "OPTION": "2", "COMBINE": "3"}[self.kind]
                    ),
                    "VolumeMultiple": self.multiplier,
                    "PriceTick": self.tick,
                    "ExpireDate": self.expiry,
                }
            )
        )


def from_ctp(row: Mapping[str, object]) -> Instrument | None:
    """转换原生 CTP 静态资料，按生命周期过滤下市合约。"""
    kind = _CTP_CLASSES.get(str(row.get("ProductClass", "")))
    if kind is None or str(row.get("InstLifePhase", "")) == "3":
        return None
    excluded = {"IsTrading", "LongMarginRatio", "ShortMarginRatio"}
    record = Instrument(
        str(row.get("ExchangeID", "")),
        str(row.get("InstrumentID", "")),
        kind,
        _number(row.get("VolumeMultiple")),
        _number(row.get("PriceTick")),
        str(row.get("ExpireDate", "") or ""),
        extra=MappingProxyType({k: v for k, v in row.items() if k not in excluded}),
    )
    return None if record.expired() else record
