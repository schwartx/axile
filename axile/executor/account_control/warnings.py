"""冻结每日额度预警事实；使用本地放行口径，不执行通知或柜台操作。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt


class AccountControlQuotaWarning(BaseModel):
    """一次达到预警线的账户级日额度快照。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    account_id: StrictInt
    execution_id: str
    operation: Literal["place_order", "cancel_order"]
    control_date: str
    timezone: str
    occurred_at_ms: StrictInt
    used: StrictInt = Field(ge=0)
    limit: StrictInt = Field(gt=0)
    warning_percent: StrictInt = Field(ge=1, le=99)
    threshold_count: StrictInt = Field(gt=0)
    remaining: StrictInt = Field(ge=0)
    source: Literal["axile_local_allowed"] = "axile_local_allowed"

    @property
    def event_id(self) -> str:
        """按账户、操作和控制窗口去重，调整比例不重复通知。"""
        return f"account-control.warning:{self.account_id}:{self.timezone}:{self.control_date}:{self.operation}"
