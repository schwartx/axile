"""按事实修订号查询成本；明细从规范事实按需计算。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from axile.server.db.models.performance import RangeKey


class CostQuery(BaseModel):
    """事实修订号与成本筛选条件。"""

    model_config = ConfigDict(extra="forbid")
    version: str = Field(pattern=r"^[0-9]{1,19}$")
    range: RangeKey = "all"
    dimension: Literal["execution", "symbol", "trade", "summary"] = "execution"
    sort: Literal["time", "cost"] = "time"
    day: str | None = None
    start: float | None = Field(default=None, allow_inf_nan=False)
    end: float | None = Field(default=None, allow_inf_nan=False)
    record_id: int | None = Field(default=None, gt=0, le=2**63 - 1)
    symbol: str | None = None
    symbol_search: str | None = None
    side: Literal["buy", "sell", "none"] | None = None
    limit: int = Field(default=20, ge=1, le=100)
    cursor: str | None = Field(default=None, max_length=1024)

    @model_validator(mode="after")
    def boundaries(self):
        """Reject ambiguous or malformed time selectors."""
        if (self.start is None) != (self.end is None):
            raise ValueError("start and end must define a nonempty interval")
        if self.start is not None and self.end is not None and self.start >= self.end:
            raise ValueError("start and end must define a nonempty interval")
        if self.day:
            if self.start is not None:
                raise ValueError("day and interval cannot be combined")
            from datetime import date

            if date.fromisoformat(self.day).isoformat() != self.day:
                raise ValueError("day must be YYYY-MM-DD")
        return self
