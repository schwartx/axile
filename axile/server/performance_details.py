"""按事实修订号查询成本；明细从规范事实按需计算。"""

from dataclasses import dataclass, field
from math import isfinite
from typing import Annotated, Literal

from msgspec import Meta

from axile.common.serialization import provided_fields
from axile.server.db.models.performance import RangeKey


@dataclass(kw_only=True)
class CostSelection:
    """成本查询和本地输入共用的精确选区。"""

    day: str | None = None
    start: float | None = field(default=None)
    end: float | None = field(default=None)
    record_id: Annotated[int, Meta(gt=0, le=2**63 - 1)] | None = field(default=None)

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

    __forbid_unknown__ = True

    def validate(self) -> None:
        """在输入入口检查本对象的业务约束。"""
        if "start" in provided_fields(self) and self.start is not None and not isfinite(self.start):
            raise ValueError("start 必须是有限数值")
        if "end" in provided_fields(self) and self.end is not None and not isfinite(self.end):
            raise ValueError("end 必须是有限数值")
        self.boundaries()


@dataclass(kw_only=True)
class CostQuery(CostSelection):
    """事实修订号与成本筛选条件。"""

    version: Annotated[str, Meta(pattern="^[0-9]{1,19}$")]
    range: RangeKey = "all"
    dimension: Literal["execution", "symbol", "trade", "summary"] = "execution"
    sort: Literal["time", "cost"] = "time"
    symbol: str | None = None
    symbol_search: str | None = None
    side: Literal["buy", "sell", "none"] | None = None
    limit: Annotated[int, Meta(ge=1, le=100)] = field(default=20)
    cursor: Annotated[str, Meta(max_length=1024)] | None = field(default=None)

    def validate(self) -> None:
        """在输入入口检查本对象的业务约束。"""
        super().validate()
