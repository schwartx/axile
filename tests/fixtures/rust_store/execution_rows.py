"""独立执行记录造数模型；不参与生产生命周期回执。"""

from pydantic import Field

from axile.server.db.models.base import now_str
from axile.server.db.models.execution import ExecuteRecordBase


class ExecuteRecord(ExecuteRecordBase):
    """执行记录及其账户身份与创建时间。"""

    id: int | None = Field(default=None)
    account_id: int
    created_at: str = Field(default_factory=now_str)
