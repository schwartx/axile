"""渠道与账户流水共用的已发生资金变动。"""

from dataclasses import dataclass, field
from datetime import datetime
from math import isfinite
from typing import Annotated, Literal

from msgspec import Meta

from axile.common.serialization import provided_fields


@dataclass(kw_only=True)
class CashFlowInput:
    """币种由账户确定，金额始终为正，方向独立表达。"""

    direction: Literal["deposit", "withdraw"]
    amount: Annotated[float, Meta(gt=0, lt=1e100)]
    occurred_at: Annotated[datetime, Meta(tz=True)]
    note: Annotated[str, Meta(max_length=500)] = field(default="")

    __forbid_unknown__ = True

    def validate(self) -> None:
        """在输入入口检查本对象的业务约束。"""
        if "amount" in provided_fields(self) and self.amount is not None and not isfinite(self.amount):
            raise ValueError("amount 必须是有限数值")


@dataclass(kw_only=True)
class ChannelCashFlow(CashFlowInput):
    """渠道提供稳定流水号；不携带银行账号等原始字段。"""

    external_id: Annotated[str, Meta(min_length=1, max_length=200)]

    cancelled: bool = field(default=False)
