"""CTP 组合持仓识别与拆腿纯函数。"""

from __future__ import annotations

import re
from typing import Any

_PREFIXES = ("SP ", "SPC ", "SPD ", "IPS ", "BUL ", "STR ", "PRT ")
_LEG_PATTERN = re.compile(r"[A-Za-z]+\d+(?:-[CP]-?\d+)?")


def is_combination_instrument(instrument_id: str) -> bool:
    """判断合约代码是否表示组合合约。"""
    value = instrument_id.strip().upper()
    return value.startswith(_PREFIXES) or "&" in value


def parse_combination_legs(instrument_id: str) -> tuple[str, str] | None:
    """从组合代码解析两条单腿。"""
    legs = _LEG_PATTERN.findall(instrument_id)
    if len(legs) < 2:
        return None
    return legs[0], legs[1]


def split_combination_position(row: object) -> list[dict[str, Any]] | None:
    """将一条组合持仓帧拆为方向相反的两条单腿帧。"""
    instrument_id = str(getattr(row, "InstrumentID", "") or "")
    legs = parse_combination_legs(instrument_id)
    if not is_combination_instrument(instrument_id) or legs is None:
        return None
    data = {
        name: getattr(row, name)
        for name in dir(row)
        if not name.startswith("_") and not callable(getattr(row, name, None))
    }
    direction = str(data.get("PosiDirection", ""))
    reverse = "3" if direction == "2" else "2"
    result: list[dict[str, Any]] = []
    for leg, leg_direction in ((legs[0], direction), (legs[1], reverse)):
        leg_data = dict(data)
        leg_data["InstrumentID"] = leg
        leg_data["PosiDirection"] = leg_direction
        leg_data["combination_origin"] = instrument_id
        result.append(leg_data)
    return result


__all__ = ["is_combination_instrument", "parse_combination_legs", "split_combination_position"]
