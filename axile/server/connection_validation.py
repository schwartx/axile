"""短期连接验证任务；凭据仅经 Pipe 传入独立进程，不保存到账户或快照。"""

from __future__ import annotations

import asyncio
import multiprocessing
import tempfile
from typing import Literal, cast
from uuid import uuid4

from loguru import logger
from pydantic import ValidationError

from axile.channels import get_channel
from axile.executor.algorithms.utils.clock import get_default_clock
from axile.executor.connection_progress import (
    ConnectionProgressTracker,
    ConnectionRun,
    ConnectionStep,
    ConnectionStepError,
    ConnectionStepUpdate,
    ConnectionTable,
    connection_plan,
)
from axile.server.connection_validation_worker import validation_worker
from axile.server.error_details import exception_details


class ValidationRejected(ValueError):
    """带 HTTP 状态的拒绝原因。"""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class ValidationJob(ConnectionRun):
    """任务只保存诊断状态，不保存账户配置。"""

    validation_id: str
    purpose: Literal["validation"] = "validation"
    status: Literal["queued", "running", "succeeded", "failed", "cancelled"] = "queued"


class ValidationService:
    """单服务进程中的有界短期任务表；生产服务已限制为单 worker。"""

    def __init__(self, *, timeout: float = 180, retention: float = 600) -> None:
        self.jobs: dict[str, ValidationJob] = {}
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.tables: dict[str, dict[str, ConnectionTable]] = {}
        self.trackers: dict[str, ConnectionProgressTracker] = {}
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
        run_id = uuid4().hex
        job = ValidationJob(
            validation_id=run_id,
            run_id=run_id,
            channel=channel,
            steps=[ConnectionStep(id=step.id, label=step.label) for step in connection_plan(channel, "validation")],
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
                self.trackers.pop(key, None)
                self.expires.pop(key, None)

    def get(self, validation_id: str) -> ValidationJob:
        """读取尚未过期的任务。"""
        self.prune()
        try:
            return self.jobs[validation_id]
        except KeyError as exc:
            raise ValidationRejected(404, "验证任务不存在或已过期，请重新验证") from exc

    def _tracker(self, job: ValidationJob) -> ConnectionProgressTracker:
        """任务使用公共记录器，完整表格单独保留供按需读取。"""
        if job.validation_id not in self.trackers:
            tracker = ConnectionProgressTracker(job.channel, "validation", run=job)
            tracker.tables = self.tables.setdefault(job.validation_id, {})
            self.trackers[job.validation_id] = tracker
        return self.trackers[job.validation_id]

    def _finish(self, job: ValidationJob, status: Literal["succeeded", "failed", "cancelled"]) -> None:
        self._tracker(job).finish(status, job.error)
        self.expires[job.validation_id] = get_default_clock().time() + self.retention

    def apply_event(self, job: ValidationJob, event: dict[str, object]) -> None:
        """接受渠道声明的阶段，完整快照让轮询无需重放短暂事件。"""
        if job.status not in {"queued", "running"}:
            return
        if "step" in event:
            if any(step.id == event["step"] for step in job.steps) and event.get("status") in {"running", "passed"}:
                job.status = "running"
                details = event.get("details")
                self._tracker(job).step(
                    str(event["step"]),
                    cast(ConnectionStepUpdate, event["status"]),
                    details if isinstance(details, dict) else None,
                )
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
            self._finish(job, "succeeded")

    def get_table(self, validation_id: str, step_id: str) -> ConnectionTable:
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
        error = ConnectionStepError(
            reason_code=code,
            message=message,
            traceback=trace if isinstance(trace, str) else None,
            error_id=error_id if isinstance(error_id, int) else None,
            details=details if isinstance(details, str) else None,
        )
        job.error = error
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
