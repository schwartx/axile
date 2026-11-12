"""执行记录与执行审计数据库模型."""

from typing import Any, Dict, Optional

from pydantic import Field

from axile.common.trade_channel import TradeChannel
from axile.domain.execution import (
    ExecutionArtifactType,
    ExecutionEventStatus,
    ExecutionEventType,
    ExecutionReasonFamily,
    ExecutionTerminateMode,
)
from axile.server.db.models.base import (
    Model,
    new_execution_id,
    now_str,
)


class ExecuteRecordBase(Model):
    """持久化执行记录的共用字段."""

    execution_id: Optional[str] = Field(
        default_factory=new_execution_id, description="执行链路唯一标识, 用于关联执行事件与执行附件"
    )
    is_success: int
    input_id: str | None = Field(default=None)
    result_input_id: str | None = Field(default=None)
    status: str | None = Field(default=None)
    task_status: str | None = Field(default=None)
    error: str | None = Field(default=None)
    outcome: str | None = Field(default=None)
    outcome_reason: str | None = Field(default=None)
    reason_code: str | None = Field(default=None)
    msg: str | None = Field(default=None)
    execution_kind: str | None = Field(default=None)
    channel_type: str | None = Field(default=None)
    execution_time: float | None = Field(default=None)
    success: bool | None = Field(default=None)
    facts_complete: bool = Field(default=False, exclude=True)
    display_status: str | None = Field(default=None)
    display_error: str | None = Field(default=None)
    display_outcome: str | None = Field(default=None)
    display_outcome_reason: str | None = Field(default=None)
    termination_reason: str | None = Field(default=None)
    termination_mode: str | None = Field(default=None)
    termination_trigger: str | None = Field(default=None)
    termination_requested_at: str | None = Field(default=None)
    termination_acked_at: str | None = Field(default=None)
    termination_finished_at: str | None = Field(default=None)
    termination_cancel_attempted: bool | None = Field(default=None)
    termination_forced: bool | None = Field(default=None)
    termination_cancel_unconfirmed: bool | None = Field(default=None)
    result_input_kind: str = Field(default="absent", exclude=True)
    result_input_value: Any = Field(default=None, exclude=True)
    audit_evidence: dict[str, Any] = Field(default_factory=dict, exclude=True)
    termination_evidence: dict[str, Any] = Field(default_factory=dict, exclude=True)
    termination_kind: str = Field(default="absent", exclude=True)
    termination_cancel_ids_kind: str = Field(default="absent", exclude=True)
    symbol: str | None = Field(default=None, exclude=True)
    technical_detail: str | None = Field(default=None, exclude=True)
    termination_cancel_count: int | None = Field(default=None, exclude=True)


class ExecutionEventBase(Model):
    """持久化执行审计事件的共用字段."""

    execution_id: str = Field(description="执行链路唯一标识")
    account_id: int = Field(description="账户 ID")
    channel: TradeChannel = Field(description="交易渠道")
    algorithm: str = Field(description="执行算法名称")
    schema_version: int = Field(default=1, description="事件协议版本")
    event_uid: str = Field(description="事件唯一键，用于幂等写入")
    event_type: ExecutionEventType = Field(description="事件类型")
    status: ExecutionEventStatus = Field(description="事件状态")
    reason_family: ExecutionReasonFamily = Field(description="原因大类")
    reason_code: str = Field(description="原因码，可带渠道命名空间")
    symbol: Optional[str] = Field(default=None, description="交易标的")
    intent_id: Optional[str] = Field(default=None, description="执行意图 ID")
    order_id: Optional[str] = Field(default=None, description="渠道订单号")
    client_order_id: Optional[str] = Field(default=None, description="客户端订单号")
    ts_local_created: str = Field(default_factory=now_str, description="本地生成事件时间")
    ts_exchange: Optional[str] = Field(default=None, description="交易所或柜台时间")
    ts_persisted: str = Field(default_factory=now_str, description="事件持久化时间")
    seq: int = Field(default=0, description="同一次执行中的单调递增序号")
    details: Dict[str, Any] = Field(default={}, description="结构化上下文详情")


class ExecutionEvent(ExecutionEventBase):
    """执行审计事件."""

    id: Optional[int] = Field(default=None)


class ExecutionArtifactBase(Model):
    """持久化执行审计附件的共用字段."""

    execution_id: str = Field(description="执行链路唯一标识")
    artifact_type: ExecutionArtifactType = Field(description="附件类型")
    schema_version: int = Field(default=1, description="附件协议版本")
    content: Dict[str, Any] = Field(default={}, description="附件内容")
    created_at: str = Field(default_factory=now_str, description="创建时间")


class ExecutionArtifact(ExecutionArtifactBase):
    """执行审计附件."""

    id: Optional[int] = Field(default=None)


class ExecutionTerminateRequest(Model):
    """请求终止后台执行任务时的入参."""

    reason: Optional[str] = None
    mode: ExecutionTerminateMode = Field(default=ExecutionTerminateMode.GRACEFUL)
