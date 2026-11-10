"""有界行情时间采样，辅助区分前置延迟、订阅问题和乱序回报。"""

from collections import OrderedDict
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
        """行情回调发布完整采样，读取方可直接复制已发布记录。"""
        previous = self._symbols.get(symbol)
        frames = previous["frames"] if previous is not None else ()
        frame = {"event_time_ms": event_time_ms, "received_at": received_at, "reason": reason}
        self._symbols[symbol] = {
            "received": (previous["received"] if previous is not None else 0) + 1,
            "accepted": (previous["accepted"] if previous is not None else 0) + int(reason is None),
            "rejected": (previous["rejected"] if previous is not None else 0) + int(reason is not None),
            "frames": (*frames, frame)[-self.frames :],
        }
        self._symbols.move_to_end(symbol)
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
