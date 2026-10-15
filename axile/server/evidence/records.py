"""将分合约、扁平和混合结果读取为同一组原生记录与快照。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Literal

RecordIdentity = tuple[str, str, str, str]


def _identity_text(value: Any) -> str:
    """只接受明确的文本或整数标识，不将空白、布尔或复合值猜成身份。"""
    if isinstance(value, str):
        return value if value.strip() else ""
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return ""


def _record_identity(row: dict[str, Any], identity_field: str) -> RecordIdentity | None:
    """在执行范围内，以实际合约、原生编号及已有的交易所和交易日隔离身份。"""
    record_id = _identity_text(row.get(identity_field))
    symbol = _identity_text(row.get("symbol"))
    if not record_id or not symbol:
        return None
    extra = row.get("extra")
    extra = extra if isinstance(extra, dict) else {}
    exchange = _identity_text(row.get("exchange_id") or extra.get("exchange_id") or extra.get("ExchangeID"))
    day = _identity_text(row.get("trading_day") or extra.get("trading_day") or extra.get("TradingDay"))
    return symbol, record_id, exchange, day


@dataclass
class RecordRows:
    """分开维护原生记录笔数和证据快照；未知身份的每次观察单独保留。"""

    identity_field: Literal["order_id", "trade_id"]
    rows: list[dict[str, Any]] = field(default_factory=list, init=False)
    unidentified: int = field(default=0, init=False)
    _snapshots: dict[RecordIdentity, bytes | set[bytes]] = field(default_factory=dict, init=False, repr=False)

    @property
    def count(self) -> int:
        """已知身份只计一笔；未知身份按保留的观察次数计数，不虚构关联。"""
        return len(self._snapshots) + self.unidentified

    @property
    def variants(self) -> list[dict[str, Any]]:
        """列出同一身份下的不同快照，差异本身及来源仍在 CSV 中保留。"""
        return [
            {
                "symbol": symbol,
                self.identity_field: record_id,
                "exchange_id": exchange or None,
                "trading_day": day or None,
                "snapshots": len(snapshots),
            }
            for (symbol, record_id, exchange, day), snapshots in self._snapshots.items()
            if isinstance(snapshots, set)
        ]

    def _observe_snapshot(self, identity: RecordIdentity, fingerprint: bytes) -> bool:
        """常见的单快照只存摘要，出现变体时才分配集合。"""
        snapshots = self._snapshots.get(identity)
        if snapshots is None:
            self._snapshots[identity] = fingerprint
            return True
        if isinstance(snapshots, bytes):
            if snapshots == fingerprint:
                return False
            self._snapshots[identity] = {snapshots, fingerprint}
            return True
        if fingerprint in snapshots:
            return False
        snapshots.add(fingerprint)
        return True

    def append(self, raw: dict[str, Any], symbol: str, source: str, index: int) -> None:
        """先解析实际合约，再按身份索引比较快照，最后附加导出来源。"""
        row = dict(raw)
        row["symbol"] = _identity_text(raw.get("symbol")) or symbol
        # 导出定位字段不是柜台快照的一部分；不因同一记录来自不同结构而制造差异。
        row.pop("source_path", None)
        row.pop("source_record_id", None)
        identity = _record_identity(row, self.identity_field)
        if identity is None:
            self.unidentified += 1
        else:
            # 索引只保留固定长度摘要，避免再保存一份可能很大的原生 extra JSON。
            fingerprint = hashlib.sha256(json.dumps(row, ensure_ascii=False, sort_keys=True).encode()).digest()
            if not self._observe_snapshot(identity, fingerprint):
                return
        row["source_path"] = source
        native_id = raw.get(self.identity_field)
        row["source_record_id"] = native_id if _identity_text(native_id) else index
        self.rows.append(row)


def _result_groups(result: dict[str, Any], source: str) -> Iterator[tuple[str, dict[str, Any], str]]:
    """按明确的分组上下文读取；顶层只补充记录，不覆盖已读取的分合约证据。"""
    symbols = result.get("symbol_results")
    if isinstance(symbols, dict):
        for key, data in symbols.items():
            if not isinstance(data, dict):
                continue
            symbol = _identity_text(data.get("symbol")) or _identity_text(key)
            token = str(key).replace("~", "~0").replace("/", "~1")
            yield symbol, data, f"{source}/symbol_results/{token}"
    yield _identity_text(result.get("symbol")), result, source


def normalize_result_records(result: dict[str, Any], source: str) -> tuple[RecordRows, RecordRows]:
    """一次遍历归一两种布局；相同身份的重复副本合并，独有记录与不同快照保留。"""
    orders = RecordRows("order_id")
    trades = RecordRows("trade_id")
    for symbol, data, path in _result_groups(result, source):
        for name, records in (("orders", orders), ("trades", trades)):
            values = data.get(name)
            if not isinstance(values, list):
                continue
            for index, raw in enumerate(values):
                if isinstance(raw, dict):
                    records.append(raw, symbol, f"{path}/{name}/{index}", index)
    return orders, trades
