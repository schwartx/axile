"""为审计分析持久化 execution 事件与附件的辅助函数."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from collections.abc import Callable, Coroutine
from typing import TypedDict, TypeVar, cast

import loguru
from sqlalchemy.exc import IntegrityError

from axile.common.trade_channel import TradeChannel
from axile.domain.execution import (
    ExecutionArtifactType,
    ExecutionEventStatus,
    ExecutionEventType,
    ExecutionReasonFamily,
)
from axile.server.core.db import SessionLocal
from axile.server.db.models import (
    ExecutionArtifact,
    ExecutionEvent,
    now_str,
)
from axile.server.execution.live import live_hub

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
"""同步执行审计协程（DB 写入/广播）的硬超时（秒）。.

审计是尽力而为的旁路：单行 INSERT 正常是毫秒级，该值只用于兜住 DB/通知链路
挂起的场景，避免调用线程（乃至事件循环线程）被无限拖死。

取值依据（非 SLA，是「挂死安全阀」的阈值）：

- **下界**：须远高于健康写入延迟（<1s）与瞬时慢（连接池饱和、DB 短暂
  failover、commit 等 WAL，量级几秒），否则会误杀「慢但没死」的写入。
- **上界**：一次执行会多次调用审计（started / 每 symbol 决策 / completed /
  若干 artifact，最坏约数十次）。DB 全线退化时累计延迟 ≈ N × 本超时，须稳在
  worker recv 600s 兜底（见 ``worker_backend.manager``）之内，否则执行会被那层
  反杀、本超时形同虚设；同时也压低万一命中事件循环线程分支时的冻结暴露。

10s ≈ 健康延迟的上千倍（不误杀）、高于瞬时慢（几秒）、按最坏约 30 次累计封顶
约 300s 稳在 600s 之内。
"""

_AUDIT_JOIN_GRACE_SECONDS = 5.0
"""临时线程 ``join`` 相对协程超时额外预留的宽限（秒），作为最外层兜底。."""


def _run_coroutine_sync(
    coro_factory: Callable[[], Coroutine[object, object, T]],
    *,
    timeout: float = _AUDIT_COROUTINE_TIMEOUT_SECONDS,
) -> T:
    """在同步上下文安全执行协程，并施加超时上界.

    Parameters
    ----------
    coro_factory : Callable[[], Coroutine[object, object, T]]
        待执行协程的工厂；延迟到真正运行时才构造协程。
    timeout : float, default=30.0
        协程执行的硬超时秒数；超时视为失败并抛出 ``TimeoutError``。

    Returns
    -------
    T
        协程的返回值。

    Raises
    ------
    TimeoutError
        协程在 ``timeout`` 内未完成。

    Notes
    -----
    - 无运行中事件循环时，直接 ``asyncio.run``；
    - 已有运行中事件循环时，切到临时线程执行，避免与当前循环冲突；
    - 两条路径都以 ``timeout`` 为硬上界（``asyncio.wait_for``），DB 或审计通知
      链路挂起时不会永久阻塞调用线程；有循环分支额外用 ``join(timeout+宽限)``
      兜底，即便协程不可取消也不会永久冻结事件循环（临时 daemon 线程被孤儿化，
      但调用方得以脱身）。
    """

    async def _run_with_deadline() -> T:
        return await asyncio.wait_for(coro_factory(), timeout)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_run_with_deadline())

    result: list[T] = []
    error: dict[str, BaseException] = {}

    def _runner() -> None:
        try:
            result.append(asyncio.run(_run_with_deadline()))
        except BaseException as exc:  # noqa: BLE001
            error["value"] = exc

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join(timeout + _AUDIT_JOIN_GRACE_SECONDS)

    if thread.is_alive():
        raise TimeoutError(f"同步执行审计协程超时（{timeout}s），临时线程未在宽限内结束")
    if "value" in error:
        raise error["value"]
    if result:
        return result[0]
    raise RuntimeError("同步执行协程失败：未返回结果且未抛出异常")


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


def _is_duplicate_integrity_error(exc: IntegrityError, *tokens: str) -> bool:
    """识别已知唯一约束触发的重复键完整性错误."""
    message = str(exc).lower()
    if "unique" not in message:
        return False
    return any(token.lower() in message for token in tokens)


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
        details=details or {},
    )

    async with SessionLocal() as session:
        try:
            session.add(event)
            await session.commit()
            if publish_live:
                _notify_live_hub(
                    execution_id=execution_id,
                    account_id=account_id,
                    event_type=event_type,
                    symbol=symbol,
                    seq=seq,
                )
            return True
        except IntegrityError as exc:
            await session.rollback()
            if _is_duplicate_integrity_error(exc, "event_uid", "uq_execution_event_event_uid"):
                logger.debug(
                    "跳过重复执行事件: execution_id={}, event_type={}, symbol={}, seq={}",
                    execution_id,
                    event_type.value,
                    symbol,
                    seq,
                )
                return False
            logger.error(f"写入执行事件失败: {exc}")
            if raise_on_error:
                raise
            return False
        except Exception as exc:
            await session.rollback()
            logger.error(f"写入执行事件失败: {exc}")
            if raise_on_error:
                raise
            return False


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

    async with SessionLocal() as session:
        try:
            session.add(artifact)
            await session.commit()
            return True
        except IntegrityError as exc:
            await session.rollback()
            if _is_duplicate_integrity_error(
                exc,
                "execution_id",
                "artifact_type",
                "uq_execution_artifact_execution_type",
            ):
                logger.debug(
                    "跳过重复执行附件: execution_id={}, artifact_type={}",
                    execution_id,
                    artifact_type.value,
                )
                return False
            logger.error(f"写入执行附件失败: {exc}")
            if raise_on_error:
                raise
            return False
        except Exception as exc:
            await session.rollback()
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
