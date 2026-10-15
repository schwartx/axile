"""将独立业务订阅汇入有界 SSE 连接，各域分别恢复快照与错误。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, Callable
from contextlib import aclosing
from dataclasses import dataclass
from typing import Literal, cast

import anyio
from fastapi import Request
from fastapi.responses import StreamingResponse
from loguru import logger

from axile.server.account_control.monitor import monitor
from axile.server.connection_events import connection_frames, connection_hub
from axile.server.execution.live import live_hub

Topic = Literal["execution", "control", "connection"]
_QUEUE_SIZE = 32
_CONTROL_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class StreamEvent:
    """携带业务命名空间的 SSE 帧。"""

    name: str
    data: object


async def execution_events() -> AsyncGenerator[StreamEvent, None]:
    """先注册订阅再读取原子快照，跳过快照水位之前的延迟广播。"""
    async with live_hub.subscribe() as queue:
        snapshot = live_hub.snapshot_envelope()
        version = cast("int", snapshot["version"])
        yield StreamEvent("execution.snapshot", snapshot)
        while True:
            frame = await queue.get()
            incoming = cast("int", frame["version"])
            if incoming <= version:
                continue
            version = incoming
            yield StreamEvent(f"execution.{frame['type']}", frame)


async def control_events() -> AsyncGenerator[StreamEvent, None]:
    """复用共享采样；采样超时只影响流控域，恢复时重新发送全量。"""
    async with monitor.subscribe() as queue:
        monitor.start()
        version = -1
        recovering = True
        if monitor.version > 0:
            snapshot = monitor.snapshot()
            version = snapshot["version"]
            recovering = False
            yield StreamEvent("control.snapshot", snapshot)
        while True:
            try:
                frame = await asyncio.wait_for(queue.get(), timeout=_CONTROL_TIMEOUT_SECONDS)
            except TimeoutError:
                recovering = True
                yield StreamEvent("control.error", {"message": "账户流控监控连续 5 秒未完成采样"})
                continue
            if recovering:
                frame = monitor.snapshot()
                recovering = False
            if frame["version"] <= version:
                continue
            version = frame["version"]
            yield StreamEvent(f"control.{frame['kind']}", frame)


async def connection_events() -> AsyncGenerator[StreamEvent, None]:
    """订阅时读取全量，其后只响应生命周期、历史证据与监控过期通知。"""
    async with connection_hub.subscribe() as queue:
        version = connection_hub.version
        yield StreamEvent(
            "connection.snapshot",
            {
                "stream_id": connection_hub.stream_id,
                "version": version,
                "kind": "snapshot",
                "data": await connection_frames(),
            },
        )
        while True:
            incoming, ids = await queue.get()
            if incoming <= version:
                continue
            frames = await connection_frames(ids)
            version = incoming
            present = {frame["account_id"] for frame in frames}
            kind = "snapshot" if ids is None else "update"
            yield StreamEvent(
                f"connection.{kind}",
                {
                    "stream_id": connection_hub.stream_id,
                    "version": version,
                    "kind": kind,
                    "data": frames,
                    "removed": sorted(ids - present) if ids is not None else [],
                },
            )


async def _forward(
    topic: Topic,
    source: Callable[[], AsyncGenerator[StreamEvent, None]],
    queue: asyncio.Queue[StreamEvent],
) -> None:
    """连接级转发允许背压；源队列负责全量恢复，故障按域重试。"""
    while True:
        try:
            async with aclosing(source()) as events:
                async for event in events:
                    await queue.put(event)
        except Exception as exc:
            logger.exception("{} 实时订阅失败", topic)
            await queue.put(StreamEvent(f"{topic}.error", {"message": str(exc)}))
        await asyncio.sleep(1)


async def merge_events(
    topics: tuple[Topic, ...] = ("execution", "control", "connection"), *, heartbeat_seconds: float = 15.0
) -> AsyncGenerator[StreamEvent | None, None]:
    """并行转发各域；空闲返回心跳，退出时回收所有连接专属任务及订阅。"""
    sources = {"execution": execution_events, "control": control_events, "connection": connection_events}
    queue: asyncio.Queue[StreamEvent] = asyncio.Queue(maxsize=_QUEUE_SIZE)
    tasks = [asyncio.create_task(_forward(topic, sources[topic], queue)) for topic in topics]
    try:
        while True:
            try:
                yield await asyncio.wait_for(queue.get(), timeout=heartbeat_seconds)
            except TimeoutError:
                yield None
    finally:
        with anyio.CancelScope(shield=True):
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


def _encode(event: StreamEvent | None) -> str:
    """编码带业务命名空间的 SSE 帧或心跳。"""
    if event is None:
        return ": ping\n\n"
    return f"event: {event.name}\ndata: {json.dumps(event.data, ensure_ascii=False)}\n\n"


def stream_response(request: Request) -> StreamingResponse:
    """创建统一响应，客户端离开即关闭生成器并释放订阅。"""

    async def frames() -> AsyncGenerator[str, None]:
        async with aclosing(merge_events()) as events:
            async for event in events:
                if await request.is_disconnected():
                    break
                yield _encode(event)

    return StreamingResponse(
        frames(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
