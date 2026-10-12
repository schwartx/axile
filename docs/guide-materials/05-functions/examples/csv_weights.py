"""从本地 CSV 读取教学信号；文件位置相对 Axile 服务的运行目录。"""

import csv
import math
from pathlib import Path

from axile.server.context import Context


def calculate_portfolio(context: Context) -> dict[str, float]:
    """读取明确的目标权重；缺失或空文件报错，避免意外返回空仓目标。"""
    path = Path("signals/target_weights.csv")
    target: dict[str, float] = {}
    with path.open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            symbol = row["symbol"].strip()
            weight = float(row["target_weight"])
            if not symbol or symbol in target:
                raise ValueError(f"品种为空或重复: {symbol!r}")
            if not math.isfinite(weight):
                raise ValueError(f"目标权重不是有限数字: {symbol}")
            target[symbol] = weight
    if not target:
        raise ValueError("信号文件没有目标，不自动视为空仓指令")
    return target
