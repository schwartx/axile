"""提取持仓原生展示字段；缺失和非法值不补零，也不通过其他字段推算。"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import date, datetime


def native_value(row: object, field: str) -> object:
    """读取字典或 SDK 对象中的原生字段。"""
    return row.get(field) if isinstance(row, Mapping) else getattr(row, field, None)


def native_number(value: object) -> float | None:
    """保留有限数值和真实零，过滤空值、布尔值及 CTP 无效数值哨兵。"""
    if not isinstance(value, (int, float, str)) or isinstance(value, bool) or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and abs(number) < 1.7976931348623157e308 else None


def native_position_attributes(
    row: object, numbers: Mapping[str, str], texts: Mapping[str, str] | None = None
) -> dict[str, float | str]:
    """只提取明确列出的原生持仓字段，不替代口径或生成缺失值。"""
    result: dict[str, float | str] = {}
    for key, field in numbers.items():
        value = native_number(native_value(row, field))
        if value is not None:
            result[key] = value
    for key, field in (texts or {}).items():
        value = native_value(row, field)
        if isinstance(value, (datetime, date)):
            value = value.isoformat()
        if isinstance(value, str) and value:
            result[key] = value
    return result


def sum_native_position_attributes(rows: list[dict[str, float | str]], keys: tuple[str, ...]) -> dict[str, float]:
    """仅在每条分项均提供有效原生值时汇总可加字段，避免部分数据冒充完整合计。"""
    result: dict[str, float] = {}
    for key in keys:
        values = [row.get(key) for row in rows]
        if values and all(isinstance(value, float) for value in values):
            total = sum(value for value in values if isinstance(value, float))
            if math.isfinite(total):
                result[key] = total
    return result
