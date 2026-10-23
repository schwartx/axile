"""业务阶段的原生入口；数据库连接和事务由 Rust 持有。"""

import asyncio
import json
from threading import Event
from typing import Any

from axile.server._native import _core


def native_operation_sync(name: str, request: object, *, database: str | None = None) -> Any:
    """序列化业务请求；事实阶段由原生编码器处理非有限数。"""
    if database is None:
        from axile.server.core.db import database_path

        database = database_path()
    evidence_input = name in getattr(_core, "EVIDENCE_INPUT_STAGES", ())
    return getattr(_core, name)(database, json.dumps(request, ensure_ascii=False, allow_nan=evidence_input))


async def native_operation(name: str, request: object, *, database: str | None = None) -> Any:
    """在线程中调用完整业务阶段，避免 SQLite 等待阻塞事件循环。"""
    worker = asyncio.create_task(asyncio.to_thread(native_operation_sync, name, request, database=database))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        await _drain_native(worker)
        raise


async def _drain_native(worker: asyncio.Task[Any]) -> None:
    """取消不能先释放调用方的锁；线程异常也不能覆盖原取消信号。"""
    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            continue
        except Exception:  # noqa: BLE001 - 线程已结束，原调用的取消优先
            break
    if not worker.cancelled():
        worker.exception()


async def native_evidence_operation(request: object, cancelled: Event) -> Any:
    """原生证据导出检查共享取消标记，结束线程后再向上传播取消。"""
    from axile.server.core.db import database_path

    control = _core.EvidenceCancellation()
    request_json = json.dumps(request, ensure_ascii=False, allow_nan=False)
    worker = asyncio.create_task(asyncio.to_thread(_core.collect_evidence, database_path(), request_json, control))
    try:
        while not worker.done():
            if cancelled.is_set():
                control.cancel()
                return await asyncio.shield(worker)
            await asyncio.wait({worker}, timeout=0.05)
        return worker.result()
    except asyncio.CancelledError:
        control.cancel()
        await _drain_native(worker)
        raise
