"""执行意图（intent）持久化模型.

Intent 是执行控制面的真源：一张被接受的调仓/清仓票一行。
``ExecuteRecord`` 仍是结果账，不承担排队与重启恢复。
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import Field

from axile.common.trade_channel import TradeChannel
from axile.domain.execution import ExecutionKind, ExecutionTaskStatus, ExecutionTerminateMode
from axile.server.db.models.base import Model, new_execution_id, now_str


class ExecutionIntent(Model):
    """账户上的一张执行票：排队、在跑或已终态.

    Notes
    -----
    部分唯一索引保证每账户至多一张 ``QUEUED``、至多一张
    ``RUNNING``/``TERMINATING``。终态行可保留对账，历史仍以 ``ExecuteRecord`` 为准。
    """

    id: Optional[int] = Field(default=None)
    execution_id: str = Field(default_factory=new_execution_id)
    account_id: int
    kind: ExecutionKind
    trigger_source: str
    status: ExecutionTaskStatus
    channel: Optional[TradeChannel] = Field(default=None)
    algorithm: Optional[str] = Field(default=None)
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=now_str)
    started_at: Optional[str] = Field(default=None)
    finished_at: Optional[str] = Field(default=None)
    error: Optional[str] = Field(default=None)
    cancel_requested_at: Optional[str] = Field(default=None)
    cancel_reason: Optional[str] = Field(default=None)
    terminate_mode: Optional[ExecutionTerminateMode] = Field(default=None)


__all__ = ["ExecutionIntent"]
