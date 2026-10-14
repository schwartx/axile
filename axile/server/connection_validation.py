"""短期连接验证任务；凭据仅经 Pipe 传入独立进程，不保存到账户或快照。"""

from __future__ import annotations

import asyncio
import multiprocessing
import tempfile
from typing import Literal
from uuid import uuid4

from loguru import logger
from pydantic import BaseModel, Field, ValidationError

from axile.channels import get_channel
from axile.executor.algorithms.utils.clock import clock_now_iso, get_default_clock
from axile.server.connection_validation_worker import validation_worker
from axile.server.error_details import exception_details


class ValidationRejected(ValueError):
    """带 HTTP 状态的拒绝原因。"""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class ValidationErrorPublic(BaseModel):
    """错误摘要与完整柜台原文分开传递。"""

    reason_code: str
    message: str
    error_id: int | None = None
    details: str | None = None
    traceback: str | None = None


class ValidationDetailRow(BaseModel):
    """保持渠道声明顺序的键值行，允许同名字段与空值。"""

    key: str
    value: str | int | float | bool | None


class ValidationStepDetails(BaseModel):
    """渠道提供展示内容，服务与前端不解释业务字段。"""

    title: str
    rows: list[ValidationDetailRow]


class ValidationTableColumn(BaseModel):
    """通用记录列的名称与排序类型。"""

    key: str
    label: str
    type: Literal["text", "number", "date", "time"] = "text"
    filterable: bool = False


class ValidationTableSummary(BaseModel):
    """轮询只返回表格描述，完整数据按需读取。"""

    kind: Literal["table"] = "table"
    title: str
    summary: str
    description: str | None = None
    columns: list[ValidationTableColumn]
    row_count: int


class ValidationTable(ValidationTableSummary):
    """验证时的只读记录快照，不通过此接口触发柜台查询。"""

    rows: list[list[str | int | float | bool | None]]


class ValidationStepPublic(BaseModel):
    """阶段的完整快照。"""

    id: str
    label: str
    status: Literal["pending", "running", "passed", "failed", "skipped"] = "pending"
    error: ValidationErrorPublic | None = None
    details: ValidationStepDetails | ValidationTableSummary | None = None


class ValidationJob(BaseModel):
    """任务只保存诊断状态，不保存账户配置。"""

    validation_id: str
    channel: str
    status: Literal["queued", "running", "succeeded", "failed", "cancelled"] = "queued"
    steps: list[ValidationStepPublic]
    created_at: str = Field(default_factory=clock_now_iso)
    finished_at: str | None = None
    error: ValidationErrorPublic | None = None


class ValidationService:
    """单服务进程中的有界短期任务表；生产服务已限制为单 worker。"""

    def __init__(self, *, timeout: float = 180, retention: float = 600) -> None:
        self.jobs: dict[str, ValidationJob] = {}
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.tables: dict[str, dict[str, ValidationTable]] = {}
        self.expires: dict[str, float] = {}
        self.timeout = timeout
        self.retention = retention

    def create(self, channel: str, config: dict[str, object]) -> ValidationJob:
        """仅显式请求才启动；所有配置校验发生在创建原生进程前。"""
        try:
            plugin = get_channel(channel)
        except KeyError as exc:
            raise ValidationRejected(422, "渠道不支持连接验证") from exc
        declaration = plugin.descriptor.connection_validation
        if declaration is None or plugin.validate_connection is None:
            raise ValidationRejected(422, "渠道不支持连接验证")
        try:
            validated = plugin.account_config_model.model_validate(config)
        except (ValidationError, ValueError) as exc:
            # 路由沿异常链保留原始校验项与异常原文。
            raise ValidationRejected(422, str(exc)) from exc
        normalized = validated.model_dump(mode="json")
        self.prune()
        if any(job.status in {"queued", "running"} for job in self.jobs.values()) or any(
            not task.done() for task in self.tasks.values()
        ):
            raise ValidationRejected(409, "已有连接验证正在运行，请等待完成或取消后重试")
        if len(self.jobs) >= 128:
            raise ValidationRejected(429, "验证任务过多，请稍后重试")
        job = ValidationJob(
            validation_id=uuid4().hex,
            channel=channel,
            steps=[ValidationStepPublic(id=step.id, label=step.label) for step in declaration.steps],
        )
        self.jobs[job.validation_id] = job
        self._start(job, normalized)
        return job

    def _start(self, job: ValidationJob, config: dict[str, object]) -> None:
        task = asyncio.create_task(self._run(job, config))
        self.tasks[job.validation_id] = task
        # 取消可能发生在协程首次执行前，不能只依赖 _run 的 finally 清除引用。
        task.add_done_callback(lambda _completed: self.tasks.pop(job.validation_id, None))

    def prune(self) -> None:
        """清理过期的验证结果快照。"""
        now = get_default_clock().time()
        for key, deadline in list(self.expires.items()):
            if now >= deadline:
                self.jobs.pop(key, None)
                self.tables.pop(key, None)
                self.expires.pop(key, None)

    def get(self, validation_id: str) -> ValidationJob:
        """读取尚未过期的任务。"""
        self.prune()
        try:
            return self.jobs[validation_id]
        except KeyError as exc:
            raise ValidationRejected(404, "验证任务不存在或已过期，请重新验证") from exc

    def _finish(self, job: ValidationJob, status: Literal["succeeded", "failed", "cancelled"]) -> None:
        job.status = status
        job.finished_at = clock_now_iso()
        self.expires[job.validation_id] = get_default_clock().time() + self.retention

    def apply_event(self, job: ValidationJob, event: dict[str, object]) -> None:
        """接受渠道声明的阶段，完整快照让轮询无需重放短暂事件。"""
        if job.status not in {"queued", "running"}:
            return
        if "step" in event:
            for step in job.steps:
                if step.id == event["step"] and event.get("status") in {"running", "passed"}:
                    details = event.get("details")
                    if isinstance(details, dict):
                        self._apply_details(job, step, details)
                    step.status = event["status"]  # type: ignore[assignment]
                    job.status = "running"
                    return
        if event.get("result") == "failed":
            logger.warning(
                "连接验证失败 validation_id={} channel={} reason_code={} diagnostic={}",
                job.validation_id,
                job.channel,
                event.get("reason_code"),
                event.get("diagnostic"),
            )
            if not any(step.status != "pending" for step in job.steps):
                event = {**event, "message": "连接验证初始化失败"}
            self.fail(
                job,
                str(event["reason_code"]),
                str(event["message"]),
                event.get("error_id"),
                event.get("details"),
                event.get("traceback"),
            )
        elif event.get("result") == "succeeded":
            if all(step.status == "passed" for step in job.steps):
                self._finish(job, "succeeded")
            else:
                self.fail(job, "CONNECTION.INCOMPLETE", "连接验证未完成全部检查")

    def _apply_details(self, job: ValidationJob, step: ValidationStepPublic, details: dict[str, object]) -> None:
        """将记录集合单独保存，避免轮询反复传输大目录。"""
        if details.get("kind") == "table":
            rows = details.get("rows", [])
            if not isinstance(rows, list):
                raise ValueError("表格记录必须为列表")
            table = ValidationTable.model_validate({**details, "row_count": len(rows)})
            self.tables.setdefault(job.validation_id, {})[step.id] = table
            step.details = ValidationTableSummary.model_validate(table.model_dump(exclude={"rows"}))
        else:
            step.details = ValidationStepDetails.model_validate(details)

    def get_table(self, validation_id: str, step_id: str) -> ValidationTable:
        """读取已有快照；完成、失败或取消后仍在任务保留期内可查。"""
        self.get(validation_id)
        table = self.tables.get(validation_id, {}).get(step_id)
        if table is None:
            raise ValidationRejected(404, "该检查项尚无记录表格")
        return table

    def fail(
        self,
        job: ValidationJob,
        code: str,
        message: str,
        error_id: object = None,
        details: object = None,
        trace: object = None,
    ) -> None:
        """记录原始错误并将后续阶段标记为未执行。"""
        active = next((step for step in job.steps if step.status == "running"), None)
        if code == "CONNECTION.BROKER_REJECTED":
            label = active.label if active is not None else ""
            if "认证" in label:
                message = "柜台拒绝认证"
            elif "登录" in label:
                message = "柜台拒绝登录"
            elif "结算" in label:
                message = "柜台拒绝结算确认"
            elif "查询" in label or "对账" in label:
                message = "柜台拒绝查询"
            else:
                message = "柜台拒绝请求"
        error = ValidationErrorPublic(
            reason_code=code,
            message=message,
            traceback=trace if isinstance(trace, str) else None,
            error_id=error_id if isinstance(error_id, int) else None,
            details=details if isinstance(details, str) else None,
        )
        job.error = error
        if active is not None:
            active.status, active.error = "failed", error
        for step in job.steps:
            if step.status == "pending":
                step.status = "skipped"
        self._finish(job, "failed")

    async def cancel(self, validation_id: str) -> ValidationJob:
        """取消后等待子进程释放；清理完成前不允许再启动另一个验证。"""
        job = self.get(validation_id)
        active = job.status in {"queued", "running"}
        task = self.tasks.get(validation_id)
        if task is not None:
            if active and not task.cancelling():
                task.cancel()
            await asyncio.shield(asyncio.gather(task, return_exceptions=True))
        if active:
            for step in job.steps:
                if step.status in {"pending", "running"}:
                    step.status = "skipped"
            self._finish(job, "cancelled")
        return job

    async def _run(self, job: ValidationJob, config: dict[str, object]) -> None:
        """隔离 SDK 崩溃，超时或取消时回收进程及全部临时原生文件。"""
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        process = context.Process(target=validation_worker, args=(child,), daemon=True)
        try:
            with tempfile.TemporaryDirectory(prefix="axile-validation-") as directory:
                try:
                    process.start()
                    child.close()
                    parent.send({"channel": job.channel, "account_config": config, "temp_dir": directory})
                    config.clear()
                    await self._monitor(job, process, parent)
                finally:
                    await asyncio.to_thread(_release_process, process)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 进程边界保留失败证据
            if job.status in {"queued", "running"}:
                self.fail(
                    job,
                    "CONNECTION.WORKER_FAILED",
                    "验证进程初始化失败",
                    details=str(exc),
                    trace=exception_details(exc)["traceback"],
                )
        finally:
            parent.close()
            child.close()
            self.tasks.pop(job.validation_id, None)

    async def _monitor(self, job: ValidationJob, process, connection) -> None:
        deadline = get_default_clock().time() + self.timeout
        while job.status in {"queued", "running"}:
            while connection.poll():
                self.apply_event(job, connection.recv())
            if job.status not in {"queued", "running"}:
                return
            if not process.is_alive():
                self.fail(job, "CONNECTION.WORKER_FAILED", "验证进程异常退出，请检查 SDK 和运行环境")
                return
            if get_default_clock().time() >= deadline:
                self.fail(job, "CONNECTION.TIMEOUT", "连接验证超时，请检查网络和测试柜台状态")
                return
            await asyncio.sleep(0.1)

    async def close(self) -> None:
        """服务退出时回收所有未完成任务。"""
        for job in list(self.jobs.values()):
            if job.status in {"queued", "running"} or job.validation_id in self.tasks:
                await self.cancel(job.validation_id)


def _release_process(process) -> None:
    """先允许正常退出，再终止；SDK 卡住时强制回收进程，避免永久占用。"""
    if process.pid is None:
        return
    process.join(0.5)
    if process.is_alive():
        process.terminate()
        process.join(3)
    if process.is_alive():
        process.kill()
        process.join(3)
    process.close()


service = ValidationService()
