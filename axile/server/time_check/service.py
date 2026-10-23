"""单进程时间检查服务：持久化证据、统一互斥和调度。"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any, Literal

from loguru import logger

from axile.executor.algorithms.utils.clock import clock_now
from axile.server.core.scheduler import scheduler
from axile.server.cron import SCHEDULER_TIMEZONE, combine_cron_triggers, parse_cron_expr
from axile.server.native_store import native_operation
from axile.server.time_check.models import TimeCheckSettings
from axile.server.time_check.probes import ntp_probe

_JOB = "system-time-check"


def preview(cron_expr: str, count: int = 5) -> list[str]:
    """使用与实际调度相同的 Cron 规则计算未来执行时间。"""
    trigger = combine_cron_triggers(parse_cron_expr(cron_expr))
    now = clock_now().astimezone(SCHEDULER_TIMEZONE)
    result = []
    for _ in range(count):
        following = trigger.get_next_fire_time(None, now)
        if following is None:
            break
        result.append(following.isoformat())
        now = following + timedelta(seconds=1)
    return result


def update_alerts(previous: dict[str, Any], ntp: dict[str, Any], threshold: int, checked_at: str) -> dict[str, Any]:
    """各项恢复需有对应正常证据；查询失败不会消除历史偏差异常。"""
    alerts = {key: value for key, value in previous.items() if key != "system"}
    if ntp["status"] == "ok":
        alerts.pop("reference", None)
        if abs(ntp["offset_ms"]) > threshold:
            alerts["offset"] = {
                "message": "系统时间偏差超限",
                "checked_at": checked_at,
                "offset_ms": ntp["offset_ms"],
                "threshold_ms": threshold,
            }
        else:
            alerts.pop("offset", None)
    else:
        alerts["reference"] = {"message": ntp.get("error", "参考时间不可用"), "checked_at": checked_at}
    alerts.pop("interrupted", None)
    return alerts


class TimeCheckService:
    """数据库是配置及结果真源，广播订阅不触发检查。"""

    def __init__(self) -> None:
        self.settings = TimeCheckSettings()
        self.state: dict[str, Any] = {}
        self.task: asyncio.Task[None] | None = None
        self.lock = asyncio.Lock()
        self.stream_id = str(uuid.uuid4())
        self.version = 0
        self.subscribers: set[asyncio.Queue[None]] = set()
        self.ready = False
        self.last_warning: tuple[tuple[str, ...], float] | None = None

    async def start(self, first_run: bool = False) -> None:
        """迁移完成后恢复状态；首次标记只有新安装入口才设置。"""
        restored = await native_operation(
            "time_check_start",
            {
                "settings": self.settings.model_dump(exclude={"version"}),
                "checked_at": clock_now(tz=SCHEDULER_TIMEZONE).isoformat(),
            },
        )
        self.settings = TimeCheckSettings.model_validate(restored["settings"])
        self.state = restored["state"]
        self.ready = True
        self.schedule()
        if first_run and not self.state.get("first_claimed"):
            claim = await native_operation("time_check_claim", {})
            self.state = claim["state"]
            if claim["claimed"]:
                await self.run("first")

    def snapshot(self) -> dict[str, Any]:
        """同时返回状态及其版本，方便重连恢复和多窗口同步。"""
        job = scheduler.get_job(_JOB)
        next_run = getattr(job, "next_run_time", None)
        status = "warning" if self.state.get("alerts") else "enabled" if self.settings.enabled else "disabled"
        return {
            "stream_id": self.stream_id,
            "version": self.version,
            "settings": self.settings.model_dump(),
            "state": self.state,
            "status": status,
            "next_run_at": next_run.isoformat() if next_run else None,
            "ready": self.ready,
        }

    def envelope(self) -> dict[str, Any]:
        """每次发送完整状态，慢订阅者可安全跳过中间帧。"""
        return {"stream_id": self.stream_id, "version": self.version, "data": self.snapshot()}

    def publish(self) -> None:
        """单槽通知保留最新状态，避免慢客户端扩大队列。"""
        self.version += 1
        for queue in self.subscribers:
            if queue.empty():
                queue.put_nowait(None)

    async def frames(self) -> AsyncIterator[dict[str, Any]]:
        """注册后立即发快照，不访问外部时间源。"""
        queue: asyncio.Queue[None] = asyncio.Queue(maxsize=1)
        self.subscribers.add(queue)
        try:
            yield self.envelope()
            while True:
                await queue.get()
                yield self.envelope()
        finally:
            self.subscribers.discard(queue)

    async def persist(self) -> None:
        """提交完整状态，完成后才广播或启动外部探测。"""
        await native_operation("time_check_persist", {"state": self.state})

    def schedule(self) -> None:
        """关闭只取消未来触发；不打断正在检查的任务。"""
        if scheduler.get_job(_JOB):
            scheduler.remove_job(_JOB)
        if self.settings.enabled:
            scheduler.add_job(
                self.automatic,
                combine_cron_triggers(parse_cron_expr(self.settings.cron_expr)),
                id=_JOB,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=1,
            )

    async def automatic(self) -> None:
        """调度回调与手工触发共用执行互斥。"""
        await self.run("automatic")

    async def save(self, config: TimeCheckSettings) -> dict[str, Any]:
        """串行版本检查与提交，避免并发浏览器丢失更新。"""
        async with self.lock:
            if config.version != self.settings.version:
                raise ValueError("设置已被其他窗口更新，请重新读取")
            updated = config.model_copy(update={"version": config.version + 1})
            saved = await native_operation(
                "time_check_save", {"version": config.version, "settings": config.model_dump(exclude={"version"})}
            )
            if not saved:
                raise ValueError("设置已被其他窗口更新，请重新读取")
            self.settings = updated
            self.schedule()
            self.publish()
            return self.snapshot()

    async def run(self, reason: Literal["first", "manual", "automatic"]) -> dict[str, Any]:
        """返回当前任务状态；关闭浏览器不取消后端任务。"""
        async with self.lock:
            if not self.task or self.task.done():
                config = self.settings.model_copy(deep=True)
                self.state = {
                    **self.state,
                    "running": True,
                    "started_at": clock_now(tz=SCHEDULER_TIMEZONE).isoformat(),
                    "trigger": reason,
                }
                await self.persist()
                self.task = asyncio.create_task(self._check(config, reason), name="system-time-check")
                self.publish()
            return self.snapshot()

    async def _check(self, config: TimeCheckSettings, reason: str) -> None:
        """保存当次配置；异常局限于诊断服务。"""
        try:
            ntp = await ntp_probe(config.host, config.port)
            finished = clock_now(tz=SCHEDULER_TIMEZONE).isoformat()
            previous = self.state.get("alerts", {})
            alerts = update_alerts(previous, ntp, config.threshold_ms, finished)
            result = {
                "finished_at": finished,
                "trigger": reason,
                "settings": config.model_dump(),
                "host": config.host,
                "port": config.port,
                "ntp": ntp,
            }
            self.state = {**self.state, "running": False, "result": result, "alerts": alerts}
            if ntp["status"] == "ok":
                self.state["last_success"] = {"checked_at": finished, "host": config.host, **ntp}
            await self.persist()
            logger.info("系统时间检查完成 trigger={} host={} ntp={}", reason, config.host, ntp)
            # 每次检查的时间变化不构成新的告警类别，重复异常最多每小时提醒一次。
            signature = tuple(sorted(f"{key}:{value['message']}" for key, value in alerts.items()))
            now = clock_now().timestamp()
            if alerts and (
                self.last_warning is None or self.last_warning[0] != signature or now - self.last_warning[1] >= 3600
            ):
                logger.warning("系统时间检查告警：{}", alerts)
                self.last_warning = (signature, now)
            elif not alerts and previous:
                logger.info("系统时间检查告警已恢复")
                self.last_warning = None

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("系统时间检查失败")
            self.state = {
                **self.state,
                "running": False,
                "alerts": {
                    **self.state.get("alerts", {}),
                    "interrupted": {
                        "message": "检查异常中断，请重新检查",
                        "checked_at": clock_now(tz=SCHEDULER_TIMEZONE).isoformat(),
                    },
                },
            }
            await self.persist()
        finally:
            self.publish()

    async def close(self) -> None:
        """退出时回收探测任务，持久化中断信息留待恢复。"""
        self.ready = False
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


service = TimeCheckService()
