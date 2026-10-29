"""为审计分析持久化 execution 事件与附件的辅助函数."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Coroutine
from typing import TypedDict, TypeVar, cast

import loguru

from axile.common.trade_channel import TradeChannel
from axile.domain.execution import (
    ExecutionArtifactType,
    ExecutionEventStatus,
    ExecutionEventType,
    ExecutionReasonFamily,
)
from axile.server.db.models import (
    ExecutionArtifact,
    ExecutionEvent,
    now_str,
)
from axile.server.execution.live import live_hub
from axile.server.native_store import native_operation

T = TypeVar("T")


def _notify_live_hub(
    *,
    execution_id: str,
    account_id: int,
    event_type: ExecutionEventType,
    symbol: str | None,
    seq: int,
) -> None:
    """把一条已落库的执行事件转交实时广播中枢（失败仅记日志，绝不影响审计持久化）。"""
    try:
        live_hub.publish(
            execution_id=execution_id,
            account_id=account_id,
            event_type=event_type,
            symbol=symbol,
            seq=seq,
        )
    except Exception as exc:  # noqa: BLE001
        loguru.logger.debug(f"live hub 广播失败（忽略）: {exc}")


_AUDIT_COROUTINE_TIMEOUT_SECONDS = 10.0
"""同步审计的取消期限；原生调用退出与资源清理完成后才返回。"""


class SyncAuditContextError(RuntimeError):
    """同步审计被误用于运行中的事件循环。"""


def _run_coroutine_sync(
    coro_factory: Callable[[], Coroutine[object, object, T]],
    *,
    timeout: float = _AUDIT_COROUTINE_TIMEOUT_SECONDS,
) -> T:
    """仅在无运行循环的工作线程中同步执行审计，禁止阻塞事件循环。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise SyncAuditContextError("同步审计入口不能在运行中的事件循环内调用，请 await 异步入口或移至工作线程")

    async def _run_with_deadline() -> T:
        return await asyncio.wait_for(coro_factory(), timeout)

    return asyncio.run(_run_with_deadline())


def build_event_uid(
    execution_id: str,
    event_type: ExecutionEventType | str,
    *,
    symbol: str | None = None,
    intent_id: str | None = None,
    order_id: str | None = None,
    seq: int = 0,
) -> str:
    """为持久化执行事件构造稳定的唯一标识."""
    payload = {
        "execution_id": execution_id,
        "event_type": str(event_type),
        "symbol": symbol,
        "intent_id": intent_id,
        "order_id": order_id,
        "seq": seq,
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_execution_event(
    *,
    execution_id: str,
    account_id: int,
    channel: TradeChannel,
    algorithm: str,
    event_type: ExecutionEventType,
    status: ExecutionEventStatus,
    reason_family: ExecutionReasonFamily,
    reason_code: str,
    symbol: str | None = None,
    intent_id: str | None = None,
    order_id: str | None = None,
    client_order_id: str | None = None,
    ts_local_created: str | None = None,
    ts_exchange: str | None = None,
    seq: int = 0,
    details: dict[str, object] | None = None,
    schema_version: int = 1,
) -> ExecutionEvent:
    """构造单条与批次共用的事件模型，不执行持久化或广播。"""
    event = ExecutionEvent(
        execution_id=execution_id,
        account_id=account_id,
        channel=channel,
        algorithm=algorithm,
        schema_version=schema_version,
        event_uid=build_event_uid(
            execution_id,
            event_type,
            symbol=symbol,
            intent_id=intent_id,
            order_id=order_id,
            seq=seq,
        ),
        event_type=event_type,
        status=status,
        reason_family=reason_family,
        reason_code=reason_code,
        symbol=symbol,
        intent_id=intent_id,
        order_id=order_id,
        client_order_id=client_order_id,
        ts_local_created=ts_local_created or now_str(),
        ts_exchange=ts_exchange,
        ts_persisted=now_str(),
        seq=seq,
        details=details if details is not None else {},
    )
    if details is not None:
        event.details = details
    return event


async def append_execution_event(
    *,
    execution_id: str,
    account_id: int,
    channel: TradeChannel,
    algorithm: str,
    event_type: ExecutionEventType,
    status: ExecutionEventStatus,
    reason_family: ExecutionReasonFamily,
    reason_code: str,
    symbol: str | None = None,
    intent_id: str | None = None,
    order_id: str | None = None,
    client_order_id: str | None = None,
    ts_local_created: str | None = None,
    ts_exchange: str | None = None,
    seq: int = 0,
    details: dict[str, object] | None = None,
    schema_version: int = 1,
    logger: loguru.Logger = loguru.logger,
    raise_on_error: bool = False,
    publish_live: bool = True,
) -> bool:
    """持久化一条 execution 事件记录，并按事件 UID 去重."""
    event = build_execution_event(
        execution_id=execution_id,
        account_id=account_id,
        channel=channel,
        algorithm=algorithm,
        event_type=event_type,
        status=status,
        reason_family=reason_family,
        reason_code=reason_code,
        symbol=symbol,
        intent_id=intent_id,
        order_id=order_id,
        client_order_id=client_order_id,
        ts_local_created=ts_local_created,
        ts_exchange=ts_exchange,
        seq=seq,
        details=details,
        schema_version=schema_version,
    )

    try:
        inserted = await native_operation("append_execution_event", event.model_dump(mode="python"))
    except Exception as exc:
        logger.error(f"写入执行事件失败: {exc}")
        if raise_on_error:
            raise
        return False
    if inserted and publish_live:
        _notify_live_hub(
            execution_id=execution_id,
            account_id=account_id,
            event_type=event_type,
            symbol=symbol,
            seq=seq,
        )
    return bool(inserted)


class _AppendExecutionEventKwargs(TypedDict):
    execution_id: str
    account_id: int
    channel: TradeChannel
    algorithm: str
    event_type: ExecutionEventType
    status: ExecutionEventStatus
    reason_family: ExecutionReasonFamily
    reason_code: str
    symbol: str | None
    intent_id: str | None
    order_id: str | None
    client_order_id: str | None
    ts_local_created: str | None
    ts_exchange: str | None
    seq: int
    details: dict[str, object] | None
    schema_version: int
    logger: loguru.Logger
    raise_on_error: bool
    publish_live: bool


def append_execution_event_sync(**kwargs: object) -> bool:
    """在线程环境中同步写入执行事件."""
    typed_kwargs = cast(_AppendExecutionEventKwargs, kwargs)
    try:
        return bool(_run_coroutine_sync(lambda: append_execution_event(**typed_kwargs)))
    except TimeoutError as exc:
        loguru.logger.error(f"同步写入执行事件超时，已放弃（审计尽力而为）: {exc}")
        return False


async def append_execution_artifact(
    *,
    execution_id: str,
    artifact_type: ExecutionArtifactType,
    content: dict[str, object],
    schema_version: int = 1,
    logger: loguru.Logger = loguru.logger,
    raise_on_error: bool = False,
) -> bool:
    """持久化执行附件快照，供后续审计查看."""
    artifact = ExecutionArtifact(
        execution_id=execution_id,
        artifact_type=artifact_type,
        schema_version=schema_version,
        content=content,
        created_at=now_str(),
    )

    try:
        return bool(await native_operation("append_execution_artifact", artifact.model_dump(mode="python")))
    except Exception as exc:
        logger.error(f"写入执行附件失败: {exc}")
        if raise_on_error:
            raise
        return False


class _AppendExecutionArtifactKwargs(TypedDict):
    execution_id: str
    artifact_type: ExecutionArtifactType
    content: dict[str, object]
    schema_version: int
    logger: loguru.Logger
    raise_on_error: bool


def append_execution_artifact_sync(**kwargs: object) -> bool:
    """在线程环境中同步写入执行附件."""
    typed_kwargs = cast(_AppendExecutionArtifactKwargs, kwargs)
    try:
        return bool(_run_coroutine_sync(lambda: append_execution_artifact(**typed_kwargs)))
    except TimeoutError as exc:
        loguru.logger.error(f"同步写入执行附件超时，已放弃（审计尽力而为）: {exc}")
        return False
