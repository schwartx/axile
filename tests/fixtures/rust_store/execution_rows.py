"""独立执行记录造数模型；不参与生产生命周期回执。"""

from dataclasses import dataclass, field

from axile.server.db.models.base import now_str
from axile.server.db.models.execution import ExecuteRecordBase


@dataclass(kw_only=True)
class ExecuteRecord(ExecuteRecordBase):
    """执行记录及其账户身份与创建时间。"""

    id: int | None = field(default=None)
    account_id: int
    created_at: str = field(default_factory=now_str)
