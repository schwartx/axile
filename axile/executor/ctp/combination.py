"""保留 CTP 组合工具的导入路径；实现供国内期货渠道共享。"""

from axile.executor.futures_combination import (
    is_combination_instrument,
    parse_combination_legs,
    split_combination_position,
)

__all__ = ["is_combination_instrument", "parse_combination_legs", "split_combination_position"]
