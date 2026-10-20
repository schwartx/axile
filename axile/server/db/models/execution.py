"""执行记录与执行审计数据库模型."""

from typing import TYPE_CHECKING, Any, ClassVar, Dict, List, Optional, Sequence

from sqlalchemy import JSON as SA_JSON
from sqlalchemy import Boolean, Column, Float, ForeignKey, Index, Integer, Text, UniqueConstraint
from sqlalchemy import Enum as SAEnum
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import relationship
from sqlmodel import Field, Relationship, SQLModel

from axile.common.trade_channel import TradeChannel
from axile.domain.execution import (
    ExecutionArtifactType,
    ExecutionEventStatus,
    ExecutionEventType,
    ExecutionKind,
    ExecutionReasonFamily,
    ExecutionTaskStatus,
    ExecutionTerminateMode,
)
from axile.server.db.models.base import (
    new_execution_id,
    now_str,
)

if TYPE_CHECKING:
    from axile.server.db.models.account import Account


class ExecuteRecordBase(SQLModel):
    """持久化执行记录的共用字段."""

    execution_id: Optional[str] = Field(
        default_factory=new_execution_id,
        sa_column=Column(Text, nullable=True),
        description="执行链路唯一标识, 用于关联执行事件与执行附件",
    )
    is_success: int = Field(sa_column=Column(Integer, nullable=False))
    input_id: str | None = Field(default=None, sa_column=Column(Text, ForeignKey("execution_input.id")))
    result_input_id: str | None = Field(default=None, sa_column=Column(Text, ForeignKey("execution_input.id")))
    status: str | None = Field(default=None, sa_column=Column(Text))
    task_status: str | None = Field(default=None, sa_column=Column(Text))
    error: str | None = Field(default=None, sa_column=Column(Text))
    outcome: str | None = Field(default=None, sa_column=Column(Text))
    outcome_reason: str | None = Field(default=None, sa_column=Column(Text))
    reason_code: str | None = Field(default=None, sa_column=Column(Text))
    msg: str | None = Field(default=None, sa_column=Column(Text))
    execution_kind: str | None = Field(default=None, sa_column=Column(Text))
    channel_type: str | None = Field(default=None, sa_column=Column(Text))
    execution_time: float | None = Field(default=None, sa_column=Column(Float))
    success: bool | None = Field(default=None, sa_column=Column(Boolean))
    facts_complete: bool = Field(
        default=False, exclude=True, sa_column=Column(Boolean, nullable=False, server_default="0")
    )
    display_status: str | None = Field(default=None, sa_column=Column(Text))
    display_error: str | None = Field(default=None, sa_column=Column(Text))
    display_outcome: str | None = Field(default=None, sa_column=Column(Text))
    display_outcome_reason: str | None = Field(default=None, sa_column=Column(Text))
    termination_reason: str | None = Field(default=None, sa_column=Column(Text))
    termination_mode: str | None = Field(default=None, sa_column=Column(Text))
    termination_trigger: str | None = Field(default=None, sa_column=Column(Text))
    termination_requested_at: str | None = Field(default=None, sa_column=Column(Text))
    termination_acked_at: str | None = Field(default=None, sa_column=Column(Text))
    termination_finished_at: str | None = Field(default=None, sa_column=Column(Text))
    termination_cancel_attempted: bool | None = Field(default=None, sa_column=Column(Boolean))
    termination_forced: bool | None = Field(default=None, sa_column=Column(Boolean))
    termination_cancel_unconfirmed: bool | None = Field(default=None, sa_column=Column(Boolean))
    result_input_kind: str = Field(default="absent", exclude=True, sa_column=Column(Text, nullable=False))
    result_input_value: Any = Field(default=None, exclude=True, sa_column=Column(SA_JSON))
    audit_evidence: dict[str, Any] = Field(
        default_factory=dict, exclude=True, sa_column=Column(SA_JSON, nullable=False)
    )
    termination_evidence: dict[str, Any] = Field(
        default_factory=dict, exclude=True, sa_column=Column(SA_JSON, nullable=False)
    )
    termination_kind: str = Field(default="absent", exclude=True, sa_column=Column(Text, nullable=False))
    termination_cancel_ids_kind: str = Field(default="absent", exclude=True, sa_column=Column(Text, nullable=False))
    symbol: str | None = Field(default=None, exclude=True, sa_column=Column(Text))
    technical_detail: str | None = Field(default=None, exclude=True, sa_column=Column(Text))
    termination_cancel_count: int | None = Field(default=None, exclude=True, sa_column=Column(Integer))


class ExecuteRecord(ExecuteRecordBase, table=True):
    """
    执行记录.

    Notes
    -----
    索引按实际查询形状选取（见 ``axile.server.repositories``）：

    - ``execution_id`` 唯一索引：保证一次执行只对应一条汇总记录，并支持按执行标识
      稳定定位记录。
    - ``(account_id, id)``：``get_recent_execute_records_by_account_id`` 等
      「按账户过滤 + 按 id 倒序 + limit」的主力路径，仪表盘每个账户都会走一次。
    - ``(account_id, created_at)``：按账户和创建时间范围读取执行记录。
    - ``(account_id, is_success, id)``：``get_latest_success_execute_record_by_account_id``
      按成败过滤后仍取最新一条；把 ``is_success`` 放在 ``id`` 之前，等值列在前、
      排序列在后，才能既走等值定位又免掉额外排序。

    没有这些复合索引时，上述查询只能靠 ``account_id`` 外键（SQLite 不自动为外键建
    索引）退化为大范围扫描。列表与状态读取只加载本表的紧凑字段，明细按需查询。
    """

    __table_args__ = (
        Index("ix_executerecord_execution_id", "execution_id", unique=True),
        Index("ix_executerecord_account_id_id", "account_id", "id"),
        Index("ix_executerecord_account_id_created_at", "account_id", "created_at"),
        Index("ix_executerecord_account_id_success_id", "account_id", "is_success", "id"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    account_id: int = Field(sa_column=Column(Integer, ForeignKey("account.id"), nullable=False))
    created_at: str = Field(default_factory=now_str, sa_column=Column(Text, nullable=False))

    account: "Account" = Relationship(
        sa_relationship=relationship(
            "Account",
            back_populates="execute_records",
        )
    )


class ExecuteRecordPublic(ExecuteRecordBase):
    """执行记录的公开表示."""

    id: Optional[int]
    account_id: int
    created_at: str


class ExecuteRecordListPublic(SQLModel):
    """执行记录的分页列表响应."""

    data: List[ExecuteRecordPublic]
    count: int


class ExecutionEventBase(SQLModel):
    """持久化执行审计事件的共用字段."""

    execution_id: str = Field(sa_column=Column(Text, nullable=False), description="执行链路唯一标识")
    account_id: int = Field(
        sa_column=Column(Integer, ForeignKey("account.id", ondelete="CASCADE"), nullable=False),
        description="账户 ID",
    )
    channel: TradeChannel = Field(sa_column=Column(Text, nullable=False), description="交易渠道")
    algorithm: str = Field(sa_column=Column(Text, nullable=False), description="执行算法名称")
    schema_version: int = Field(
        default=1,
        sa_column=Column(Integer, nullable=False),
        description="事件协议版本",
    )
    event_uid: str = Field(sa_column=Column(Text, nullable=False), description="事件唯一键，用于幂等写入")
    event_type: ExecutionEventType = Field(
        sa_column=Column(SAEnum(ExecutionEventType, name="executioneventtype"), nullable=False),
        description="事件类型",
    )
    status: ExecutionEventStatus = Field(
        sa_column=Column(SAEnum(ExecutionEventStatus, name="executioneventstatus"), nullable=False),
        description="事件状态",
    )
    reason_family: ExecutionReasonFamily = Field(
        sa_column=Column(SAEnum(ExecutionReasonFamily, name="executionreasonfamily"), nullable=False),
        description="原因大类",
    )
    reason_code: str = Field(sa_column=Column(Text, nullable=False), description="原因码，可带渠道命名空间")
    symbol: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True), description="交易标的")
    intent_id: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True), description="执行意图 ID")
    order_id: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True), description="渠道订单号")
    client_order_id: Optional[str] = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="客户端订单号",
    )
    ts_local_created: str = Field(
        default_factory=now_str,
        sa_column=Column(Text, nullable=False),
        description="本地生成事件时间",
    )
    ts_exchange: Optional[str] = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="交易所或柜台时间",
    )
    ts_persisted: str = Field(
        default_factory=now_str,
        sa_column=Column(Text, nullable=False),
        description="事件持久化时间",
    )
    seq: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False),
        description="同一次执行中的单调递增序号",
    )
    details: Dict[str, Any] = Field(
        default={},
        sa_column=Column(SA_JSON, nullable=False),
        description="结构化上下文详情",
    )


class ExecutionEvent(ExecutionEventBase, AsyncAttrs, table=True):
    """执行审计事件."""

    __tablename__: ClassVar[str] = "execution_event"
    __table_args__ = (
        UniqueConstraint("event_uid", name="uq_execution_event_event_uid"),
        Index("ix_execution_event_execution_seq", "execution_id", "seq"),
        Index("ix_execution_event_account_created", "account_id", "ts_local_created"),
        Index("ix_execution_event_type_created", "event_type", "ts_local_created"),
        Index("ix_execution_event_reason_created", "reason_code", "ts_local_created"),
        Index("ix_execution_event_symbol_created", "symbol", "ts_local_created"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)


class ExecutionEventPublic(ExecutionEventBase):
    """持久化执行审计事件的公开表示."""

    id: Optional[int]


class ExecutionEventListPublic(SQLModel):
    """执行事件的分页列表响应."""

    data: List[ExecutionEventPublic]
    count: int


class ExecutionArtifactBase(SQLModel):
    """持久化执行审计附件的共用字段."""

    execution_id: str = Field(sa_column=Column(Text, nullable=False), description="执行链路唯一标识")
    artifact_type: ExecutionArtifactType = Field(
        sa_column=Column(SAEnum(ExecutionArtifactType, name="executionartifacttype"), nullable=False),
        description="附件类型",
    )
    schema_version: int = Field(
        default=1,
        sa_column=Column(Integer, nullable=False),
        description="附件协议版本",
    )
    content: Dict[str, Any] = Field(
        default={},
        sa_column=Column(SA_JSON, nullable=False),
        description="附件内容",
    )
    created_at: str = Field(
        default_factory=now_str,
        sa_column=Column(Text, nullable=False),
        description="创建时间",
    )


class ExecutionArtifact(ExecutionArtifactBase, AsyncAttrs, table=True):
    """执行审计附件."""

    __tablename__: ClassVar[str] = "execution_artifact"
    __table_args__ = (
        UniqueConstraint("execution_id", "artifact_type", name="uq_execution_artifact_execution_type"),
        Index("ix_execution_artifact_execution", "execution_id"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)


class ExecutionArtifactPublic(ExecutionArtifactBase):
    """持久化执行附件的公开表示."""

    id: Optional[int]


class ExecutionArtifactListPublic(SQLModel):
    """执行附件的分页列表响应."""

    data: List[ExecutionArtifactPublic]
    count: int


class ExecutionTriggerResponse(SQLModel):
    """异步入队执行任务后返回的响应."""

    message: str
    execution_id: str
    account_id: int
    accepted: str = "created"


class ExecutionTerminateRequest(SQLModel):
    """请求终止后台执行任务时的入参."""

    reason: Optional[str] = None
    mode: ExecutionTerminateMode = Field(default=ExecutionTerminateMode.GRACEFUL)


class ExecutionTerminateResponse(SQLModel):
    """终止请求受理后的响应."""

    message: str
    account_id: int
    execution_id: str | None = None
    status: ExecutionTaskStatus | None = None


class ExecutionStatusPublic(SQLModel):
    """轮询接口使用的公开执行状态载荷."""

    execution_id: str
    account_id: int
    execution_kind: Optional[ExecutionKind] = None
    status: ExecutionTaskStatus
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    error: Optional[str] = None
    output_status: Optional[str] = None
    outcome: Optional[str] = None
    outcome_reason: Optional[str] = None
    outcome_symbols: list[str] = Field(default_factory=list)
    record_id: Optional[int] = None
    is_success: Optional[int] = None
    cancel_requested_at: Optional[str] = None
    cancel_reason: Optional[str] = None
    terminate_mode: Optional[ExecutionTerminateMode] = None


class Message(SQLModel):
    """变更类接口使用的简单消息封装."""

    message: str


class ListResponse(SQLModel):
    """不分页列表响应的简单封装."""

    data: Sequence[Any]


class ListResponsePage(SQLModel):
    """分页列表响应封装."""

    data: Sequence[Any]
    count: int
