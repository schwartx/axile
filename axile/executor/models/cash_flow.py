"""渠道与账户流水共用的已发生资金变动。"""

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class CashFlowInput(BaseModel):
    """币种由账户确定，金额始终为正，方向独立表达。"""

    model_config = ConfigDict(extra="forbid")

    direction: Literal["deposit", "withdraw"]
    amount: float = Field(gt=0, lt=1e100, allow_inf_nan=False, strict=True)
    occurred_at: AwareDatetime
    note: str = Field(default="", max_length=500)


class ChannelCashFlow(CashFlowInput):
    """渠道提供稳定流水号；不携带银行账号等原始字段。"""

    external_id: str = Field(min_length=1, max_length=200)

    cancelled: bool = Field(default=False, strict=True)
