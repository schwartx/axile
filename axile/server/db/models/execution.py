"""执行记录与执行审计数据库模型."""

from dataclasses import dataclass, field
from typing import Annotated, Any, Dict, Optional

from msgspec import Meta

from axile.common.trade_channel import TradeChannel
from axile.domain.execution import (
    ExecutionArtifactType,
    ExecutionEventStatus,
    ExecutionEventType,
    ExecutionReasonFamily,
    ExecutionTerminateMode,
)
from axile.server.db.models.base import (
    new_execution_id,
    now_str,
)


@dataclass(kw_only=True)
class ExecuteRecordBase:
    """持久化执行记录的共用字段."""

    execution_id: Annotated[Optional[str], Meta(description="执行链路唯一标识, 用于关联执行事件与执行附件")] = field(
        default_factory=new_execution_id
    )
    is_success: int
    input_id: str | None = field(default=None)
    result_input_id: str | None = field(default=None)
    status: str | None = field(default=None)
    task_status: str | None = field(default=None)
    error: str | None = field(default=None)
    outcome: str | None = field(default=None)
    outcome_reason: str | None = field(default=None)
    reason_code: str | None = field(default=None)
    msg: str | None = field(default=None)
    execution_kind: str | None = field(default=None)
    channel_type: str | None = field(default=None)
    execution_time: float | None = field(default=None)
    success: bool | None = field(default=None)
    facts_complete: bool = field(default=False, metadata={"exclude": True})
    display_status: str | None = field(default=None)
    display_error: str | None = field(default=None)
    display_outcome: str | None = field(default=None)
    display_outcome_reason: str | None = field(default=None)
    termination_reason: str | None = field(default=None)
    termination_mode: str | None = field(default=None)
    termination_trigger: str | None = field(default=None)
    termination_requested_at: str | None = field(default=None)
    termination_acked_at: str | None = field(default=None)
    termination_finished_at: str | None = field(default=None)
    termination_cancel_attempted: bool | None = field(default=None)
    termination_forced: bool | None = field(default=None)
    termination_cancel_unconfirmed: bool | None = field(default=None)
    result_input_kind: str = field(default="absent", metadata={"exclude": True})
    result_input_value: Any = field(default=None, metadata={"exclude": True})
    audit_evidence: dict[str, Any] = field(default_factory=dict, metadata={"exclude": True})
    termination_evidence: dict[str, Any] = field(default_factory=dict, metadata={"exclude": True})
    termination_kind: str = field(default="absent", metadata={"exclude": True})
    termination_cancel_ids_kind: str = field(default="absent", metadata={"exclude": True})
    symbol: str | None = field(default=None, metadata={"exclude": True})
    technical_detail: str | None = field(default=None, metadata={"exclude": True})
    termination_cancel_count: int | None = field(default=None, metadata={"exclude": True})


@dataclass(kw_only=True)
class ExecutionEventBase:
    """持久化执行审计事件的共用字段."""

    execution_id: Annotated[str, Meta(description="执行链路唯一标识")]
    account_id: Annotated[int, Meta(description="账户 ID")]
    channel: Annotated[TradeChannel, Meta(description="交易渠道")]
    algorithm: Annotated[str, Meta(description="执行算法名称")]
    schema_version: Annotated[int, Meta(description="事件协议版本")] = field(default=1)
    event_uid: Annotated[str, Meta(description="事件唯一键，用于幂等写入")]
    event_type: Annotated[ExecutionEventType, Meta(description="事件类型")]
    status: Annotated[ExecutionEventStatus, Meta(description="事件状态")]
    reason_family: Annotated[ExecutionReasonFamily, Meta(description="原因大类")]
    reason_code: Annotated[str, Meta(description="原因码，可带渠道命名空间")]
    symbol: Annotated[Optional[str], Meta(description="交易标的")] = field(default=None)
    intent_id: Annotated[Optional[str], Meta(description="执行意图 ID")] = field(default=None)
    order_id: Annotated[Optional[str], Meta(description="渠道订单号")] = field(default=None)
    client_order_id: Annotated[Optional[str], Meta(description="客户端订单号")] = field(default=None)
    ts_local_created: Annotated[str, Meta(description="本地生成事件时间")] = field(default_factory=now_str)
    ts_exchange: Annotated[Optional[str], Meta(description="交易所或柜台时间")] = field(default=None)
    ts_persisted: Annotated[str, Meta(description="事件持久化时间")] = field(default_factory=now_str)
    seq: Annotated[int, Meta(description="同一次执行中的单调递增序号")] = field(default=0)
    details: Annotated[Dict[str, Any], Meta(description="结构化上下文详情")] = field(default_factory=lambda: {})


@dataclass(kw_only=True)
class ExecutionEvent(ExecutionEventBase):
    """执行审计事件."""

    id: Optional[int] = field(default=None)


@dataclass(kw_only=True)
class ExecutionArtifactBase:
    """持久化执行审计附件的共用字段."""

    execution_id: Annotated[str, Meta(description="执行链路唯一标识")]
    artifact_type: Annotated[ExecutionArtifactType, Meta(description="附件类型")]
    schema_version: Annotated[int, Meta(description="附件协议版本")] = field(default=1)
    content: Annotated[Dict[str, Any], Meta(description="附件内容")] = field(default_factory=lambda: {})
    created_at: Annotated[str, Meta(description="创建时间")] = field(default_factory=now_str)


@dataclass(kw_only=True)
class ExecutionArtifact(ExecutionArtifactBase):
    """执行审计附件."""

    id: Optional[int] = field(default=None)


@dataclass(kw_only=True)
class ExecutionTerminateRequest:
    """请求终止后台执行任务时的入参."""

    reason: Optional[str] = None
    mode: ExecutionTerminateMode = field(default=ExecutionTerminateMode.GRACEFUL)
