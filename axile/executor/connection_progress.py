"""渠道共用的连接步骤、详情与过程快照；不保存连接凭据。"""

from __future__ import annotations

import threading
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Literal
from uuid import uuid4

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from axile.executor.algorithms.utils.clock import clock_now_iso

type ConnectionPurpose = Literal["connect", "recovery", "validation"]
type ConnectionStepUpdate = Literal["running", "passed", "skipped"]
type ConnectionRunResult = Literal["succeeded", "failed", "cancelled"]


class ConnectionStepDefinition(BaseModel):
    """渠道声明的一个真实连接阶段。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    label: str = Field(min_length=1)


class ConnectionStepError(BaseModel):
    """错误摘要与完整柜台原文分开传递。"""

    reason_code: str
    message: str
    error_id: int | None = None
    details: str | None = None
    traceback: str | None = None


class ConnectionDetailRow(BaseModel):
    """保持渠道声明顺序的键值行，允许同名字段与空值。"""

    key: str
    value: str | int | float | bool | None


class ConnectionStepDetails(BaseModel):
    """渠道提供展示内容，服务与前端不解释业务字段。"""

    title: str
    rows: list[ConnectionDetailRow]


class ConnectionTableColumn(BaseModel):
    """通用记录列的名称与排序类型。"""

    key: str
    label: str
    type: Literal["text", "number", "date", "time"] = "text"
    filterable: bool = False


class ConnectionTableSummary(BaseModel):
    """轮询只返回表格描述，完整数据按需读取。"""

    kind: Literal["table"] = "table"
    title: str
    summary: str
    description: str | None = None
    columns: list[ConnectionTableColumn]
    row_count: int


class ConnectionTable(ConnectionTableSummary):
    """连接时的只读记录快照，不通过此接口触发柜台查询。"""

    rows: list[list[str | int | float | bool | None]]


class ConnectionStep(BaseModel):
    """阶段的完整快照。"""

    id: str
    label: str
    status: Literal["pending", "running", "passed", "failed", "skipped"] = "pending"
    started_at: str | None = None
    finished_at: str | None = None
    error: ConnectionStepError | None = None
    details: ConnectionStepDetails | ConnectionTableSummary | None = None


class ConnectionRun(BaseModel):
    """一次连接的完整进度，独立于账户当前的可用状态。"""

    run_id: str = Field(default_factory=lambda: uuid4().hex)
    channel: str
    purpose: Literal["connect", "recovery", "validation"] = "connect"
    status: Literal["queued", "running", "succeeded", "failed", "cancelled"] = "running"
    steps: list[ConnectionStep]
    created_at: str = Field(default_factory=clock_now_iso)
    finished_at: str | None = None
    error: ConnectionStepError | None = None


ACCOUNT_STEPS = (
    ConnectionStepDefinition(id="account_assets", label="核对账户资产"),
    ConnectionStepDefinition(id="pending_orders", label="核对未完成委托"),
    ConnectionStepDefinition(id="ready", label="账户就绪"),
)
RECOVERY_STEPS = (ConnectionStepDefinition(id="recover_connection", label="恢复账户通道"), *ACCOUNT_STEPS)


def connection_plan(channel: str, purpose: ConnectionPurpose = "connect") -> tuple[ConnectionStepDefinition, ...]:
    """按渠道及用途选择步骤，不把验证附加检查混入常驻连接。"""
    if purpose == "recovery":
        return RECOVERY_STEPS
    from axile.channels import get_channel

    descriptor = get_channel(channel).descriptor
    steps = descriptor.connection_steps
    if purpose == "validation" and descriptor.connection_validation is not None:
        steps = descriptor.connection_validation.steps
    return (*steps, *ACCOUNT_STEPS)


def connection_step_error(error: BaseException) -> ConnectionStepError:
    """保留原始异常链与柜台错误码，分类不依赖展示文案。"""
    code = "CONNECTION.FAILED"
    error_id = getattr(error, "error_id", None)
    if isinstance(error, (ImportError, OSError)):
        code = "CONNECTION.SDK_UNAVAILABLE"
    elif isinstance(error, TimeoutError):
        code = "CONNECTION.STAGE_TIMEOUT"
    elif isinstance(error_id, int):
        code = "CONNECTION.BROKER_REJECTED"
    return ConnectionStepError(
        reason_code=code,
        message=str(getattr(error, "execution_error", None) or error or type(error).__name__),
        error_id=error_id if isinstance(error_id, int) else None,
        details=str(getattr(error, "broker_message", None) or error),
        traceback="".join(traceback.format_exception(error)),
    )


class ConnectionProgressTracker:
    """序列化原生回调；快照自包含，采样合并不会丢失已完成步骤。"""

    def __init__(
        self,
        channel: str,
        purpose: ConnectionPurpose = "connect",
        observer: Callable[[dict], None] | None = None,
        *,
        run: ConnectionRun | None = None,
    ):
        self._lock = threading.RLock()
        self.observer = observer
        self.run = run or ConnectionRun(
            channel=channel,
            purpose=purpose,
            steps=[ConnectionStep(**step.model_dump()) for step in connection_plan(channel, purpose)],
        )
        self.tables: dict[str, ConnectionTable] = {}

    def snapshot(self, *, tables: bool = False) -> dict:
        """公开快照只带摘要；完整表格仅通过 IPC 和按需查询传递。"""
        with self._lock:
            data = self.run.model_dump(mode="json")
            if tables:
                data["tables"] = {key: table.model_dump(mode="json") for key, table in self.tables.items()}
            return data

    def publish(self) -> None:
        """提交不可变副本，调用方无需持有记录器内部对象。"""
        if self.observer is not None:
            try:
                self.observer(self.snapshot(tables=True))
            except Exception:
                logger.exception("连接进度观察者记录失败，不改变原始连接结果")

    def step(self, step_id: str, status: ConnectionStepUpdate, details: dict | None = None) -> None:
        """只接受本次计划中的步骤；终结后忽略迟到事件。"""
        with self._lock:
            if self.run.status not in {"queued", "running"}:
                return
            step = next((row for row in self.run.steps if row.id == step_id), None)
            if step is None or step.status in {"passed", "failed", "skipped"}:
                return
            if status not in {"running", "passed", "skipped"}:
                raise ValueError(f"无效的连接步骤状态: {status}")
            step.status = status
            step.started_at = step.started_at or clock_now_iso()
            if status != "running":
                step.finished_at = clock_now_iso()
            if details is not None:
                if details.get("kind") == "table":
                    table = ConnectionTable.model_validate({**details, "row_count": len(details.get("rows", []))})
                    self.tables[step_id] = table
                    step.details = ConnectionTableSummary.model_validate(table.model_dump(exclude={"rows"}))
                else:
                    step.details = ConnectionStepDetails.model_validate(details)
            self.publish()

    def finish(self, status: ConnectionRunResult = "succeeded", error: ConnectionStepError | None = None) -> None:
        """失败或取消保留完成比例；成功必须完成所有必要步骤。"""
        with self._lock:
            if self.run.status == "failed" and status == "failed" and error is not None:
                self._enrich_failure(error)
                return
            if self.run.status not in {"queued", "running"}:
                return
            if status == "succeeded" and any(s.status != "passed" for s in self.run.steps):
                status = "failed"
                error = ConnectionStepError(reason_code="CONNECTION.INCOMPLETE", message="连接未完成全部检查")
            self.run.status = status
            self.run.error = error
            self.run.finished_at = clock_now_iso()
            for step in self.run.steps:
                if step.status == "running":
                    step.status = "failed" if status == "failed" else "skipped"
                    step.error = error
                    step.finished_at = self.run.finished_at
                elif step.status == "pending":
                    step.status = "skipped"
            self.publish()

    def _enrich_failure(self, error: ConnectionStepError) -> None:
        """异步回调先定位失败阶段，异常抛出后补齐传播栈，保留原始柜台原因。"""
        previous = self.run.error
        if previous is None or not error.traceback or previous.traceback == error.traceback:
            return
        enriched = previous.model_copy(update={"traceback": error.traceback})
        self.run.error = enriched
        for step in self.run.steps:
            if step.status == "failed":
                step.error = enriched
        self.publish()


_progress: ContextVar[ConnectionProgressTracker | None] = ContextVar("connection_progress", default=None)


def current_connection_progress() -> ConnectionProgressTracker | None:
    """供构造期间及同线程连接步骤取得当前过程。"""
    return _progress.get()


@contextmanager
def connection_progress_scope(tracker: ConnectionProgressTracker) -> Iterator[None]:
    """后台线程通过 ConnectionMonitor 捕获记录器，不依赖线程上下文继承。"""
    token = _progress.set(tracker)
    try:
        tracker.publish()
        yield
    finally:
        _progress.reset(token)
