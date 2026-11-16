"""保留规划时价格币种到审计金额币种的换算依据。"""

from dataclasses import dataclass, field
from math import isfinite
from typing import Annotated

from msgspec import Meta

from axile.common.serialization import provided_fields


@dataclass(kw_only=True, frozen=True)
class PriceValueConversion:
    """一单位原生价格对应的账户计价金额；不改变报单与成交价格。"""

    price_currency: Annotated[str, Meta(min_length=1, description="交易所原生价格币种")]
    value_currency: Annotated[str, Meta(min_length=1, description="审计金额币种")]
    rate: Annotated[float, Meta(gt=0, description="原生价格到审计金额的汇率")]
    reference: Annotated[dict[str, object], Meta(description="规划时冻结的参考汇率证据")] = field(default_factory=dict)

    def validate(self) -> None:
        """在输入入口检查本对象的业务约束。"""
        if "rate" in provided_fields(self) and self.rate is not None and not isfinite(self.rate):
            raise ValueError("rate 必须是有限数值")
