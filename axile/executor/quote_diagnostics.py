"""有界行情时间采样，辅助区分前置延迟、订阅问题和乱序回报。"""

from collections import OrderedDict, deque
from typing import Any


class QuoteDiagnostics:
    """按合约保留计数和最近时间帧；不保存原生 tick 或改变行情门禁。"""

    def __init__(self, max_symbols: int = 256, frames: int = 8) -> None:
        if max_symbols < 1 or frames < 1:
            raise ValueError("行情诊断上限须大于零")
        self.max_symbols = max_symbols
        self.frames = frames
        self._symbols: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def observe(self, symbol: str, *, event_time_ms: int, received_at: float, reason: str | None) -> None:
        """由持有行情锁的调用方传入已规范化时间和既有拒绝原因。"""
        row = self._symbols.pop(symbol, None)
        if row is None:
            row = {"received": 0, "accepted": 0, "rejected": 0, "frames": deque(maxlen=self.frames)}
        row["received"] += 1
        row["rejected" if reason else "accepted"] += 1
        row["frames"].append({"event_time_ms": event_time_ms, "received_at": received_at, "reason": reason})
        self._symbols[symbol] = row
        if len(self._symbols) > self.max_symbols:
            self._symbols.popitem(last=False)

    def snapshot(self, symbol: str) -> dict[str, Any]:
        """复制采样，淘汰或尚无数据时显式返回未采样。"""
        row = self._symbols.get(symbol)
        if row is None:
            return {"sampled": False, "max_symbols": self.max_symbols, "max_frames": self.frames}
        return {
            "sampled": True,
            "received": row["received"],
            "accepted": row["accepted"],
            "rejected": row["rejected"],
            "frames": [dict(frame) for frame in row["frames"]],
            "max_symbols": self.max_symbols,
            "max_frames": self.frames,
        }
