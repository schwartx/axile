"""主服务保存最新连接观察；worker 仅通过 IPC 提供观察。"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from typing import Any, cast
from uuid import uuid4

from loguru import logger

from axile.executor.connection_presentation import presentation_snapshot
from axile.executor.connection_state import (
    ConnectionObservation,
    connection_observer_scope,
    current_connection_observer,
)
from axile.server.db.models.account_connection import (
    AccountConnectionDetail,
    AccountConnectionPublic,
)
from axile.server.native_store import native_operation, native_operation_sync


def connection_config_key(channel: object, config: object) -> str:
    """比较规范化连接配置；签名不公开，也不写日志。"""
    value = json.dumps([str(channel), config], sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class ConnectionBinding:
    """一个配置版本下最新获准上报的运行时实例。"""

    account_id: int
    config_key: str
    instance_id: str
    manually_disconnected: bool = False


def begin_connection_instance(account: object) -> ConnectionBinding | None:
    """注册最新实例，拒绝使用过期配置的调用方；重连不提前清除旧异常。"""
    account_id = getattr(account, "id", None)
    if account_id is None:
        return None
    key = connection_config_key(getattr(account, "trade_channel"), getattr(account, "account_config"))
    try:
        result = native_operation_sync(
            "connection_begin", {"account_id": account_id, "config_key": key, "instance_id": uuid4().hex}
        )
        return ConnectionBinding(**result) if result is not None else None
    except Exception:
        logger.exception("账户连接实例注册失败，不改变原始操作")
        return None


def record_connection_observation(
    binding: ConnectionBinding | None,
    sequence: int,
    observation: ConnectionObservation,
    *,
    history: dict[str, object] | None = None,
) -> None:
    """只覆盖当前配置及实例的更新观察；正常时同时删除旧错误。"""
    if binding is None:
        return
    try:
        result = native_operation_sync(
            "connection_observe",
            {"binding": asdict(binding), "sequence": sequence, "observation": asdict(observation), "history": history},
        )
        if result["applied"]:
            from axile.server.connection_events import connection_hub

            connection_hub.changed(binding.account_id)
    except Exception:
        logger.exception("账户连接状态保存失败，不覆盖原始操作错误")


class ConnectionCollector:
    """运行时线程与监控线程共享的最新观察，不在 worker 中访问数据库。"""

    def __init__(self, binding: ConnectionBinding | None = None, *, persist: bool = False) -> None:
        self.binding = binding
        self._persist = persist
        self._lock = threading.RLock()
        self._sequence = 0
        self._latest: ConnectionObservation | None = None
        self._progress: dict[str, object] | None = None
        self._session: ConnectionObservation | None = None
        self._last_success_at: str | None = None
        self._last_failure: dict[str, object] | None = None

    def observe(self, observation: ConnectionObservation) -> None:
        """序列化观察来源；同一观察回放不刷新确认时间。"""
        with self._lock:
            if observation.kind == "progress":
                self._sequence += 1
                self._progress = observation.progress
                return
            if observation == self._latest:
                return
            self._sequence += 1
            self._latest = observation
            if observation.kind == "session":
                self._session = observation
            elif (
                observation.status in {"error", "recovering"}
                and self._session is not None
                and self._session.status in {"ok", "recovering"}
            ):
                self._session = observation
            if observation.kind == "access" and observation.status == "ok":
                self._last_success_at = observation.observed_at
            if observation.status in {"error", "cooldown"}:
                self._last_failure = {
                    key: value for key, value in asdict(observation).items() if key not in {"status", "kind"}
                }
            if self._persist:
                record_connection_observation(self.binding, self._sequence, observation, history=self._history())

    def observe_session(self, observation: ConnectionObservation) -> None:
        """宿主在准备与核对完成后上报会话状态，独立于 SDK 的单次访问结果。"""
        from dataclasses import replace

        self.observe(replace(observation, kind="session"))

    @property
    def needs_reconciliation(self) -> bool:
        """原生连接故障恢复后仍需一次真实核对，成功回调不能直接恢复会话就绪。"""
        with self._lock:
            return self._session is not None and self._session.status in {"error", "recovering"}

    def _history(self) -> dict[str, object]:
        return {"last_success_at": self._last_success_at, "last_failure": self._last_failure}

    def snapshot(self) -> dict[str, object] | None:
        """只复制已发生的结果，采样不产生新的连接证据。"""
        with self._lock:
            if self.binding is None or self._latest is None:
                return None
            return {
                "binding": asdict(self.binding),
                "sequence": self._sequence,
                "observation": asdict(self._latest),
                "session": asdict(self._session) if self._session else None,
                "history": self._history(),
                "progress": self._progress,
            }


def consume_connection_snapshot(snapshot: dict[str, object] | None) -> None:
    """主服务消费 worker 业务响应与空闲监控中的同一份快照。"""
    if snapshot is not None:
        try:
            from axile.server.connection_runtime import observe_session

            binding = ConnectionBinding(**cast("dict[str, Any]", snapshot["binding"]))
            raw_session = snapshot.get("session")
            observe_session(
                binding,
                int(str(snapshot["sequence"])),
                ConnectionObservation(**cast("dict[str, Any]", raw_session)) if raw_session else None,
                progress=cast("dict | None", snapshot.get("progress")),
            )
            record_connection_observation(
                binding,  # type: ignore[arg-type]
                int(snapshot["sequence"]),  # type: ignore[arg-type]
                ConnectionObservation(**snapshot["observation"]),  # type: ignore[arg-type]
                history=cast("dict[str, object] | None", snapshot.get("history")),
            )
        except (TypeError, KeyError, ValueError):
            logger.exception("账户连接观察载荷无效")


@contextmanager
def report_connection_for_account(account: object) -> Iterator[None]:
    """主服务直接构造执行器时绑定观察者；worker 使用请求已注入的观察者。"""
    if current_connection_observer() is not None:
        yield
        return
    collector = ConnectionCollector(begin_connection_instance(account), persist=True)
    with connection_observer_scope(collector.observe):
        yield


def public_connection(account: object, *, detail: bool = False) -> AccountConnectionPublic:
    """当前状态只取本进程会话；历史结果不构成会话存活或就绪证明。"""
    from axile.server.connection_runtime import current_session
    from axile.server.db.models.account_connection import AccountConnectionFailure

    state = getattr(account, "connection_state", None)
    key = connection_config_key(getattr(account, "trade_channel"), getattr(account, "account_config"))
    current = current_session(getattr(account, "id"), key)
    failure = None
    if state is not None and state.config_key == key:
        current.last_success_at = state.last_success_at
        current.manually_disconnected = state.manually_disconnected
        if state.last_failure:
            raw = {**state.last_failure, "presentation": presentation_snapshot(state.last_failure.get("presentation"))}
            failure = AccountConnectionFailure.model_validate(raw)
            current.last_failure_at = failure.observed_at
        if state.manually_disconnected and current.status in {"uninitialized", "unknown", "disconnected"}:
            current.status, current.message = "disconnected", "账户会话已主动断开，需要显式重新连接"
    if not detail:
        return current
    active_failure = failure if current.status == "unavailable" else None
    return AccountConnectionDetail(
        **current.model_dump(),
        last_failure=failure,
        error=active_failure.error if active_failure else None,
        presentation=active_failure.presentation if active_failure else None,
    )


async def recover_connection_sessions() -> None:
    """服务重启撤销旧实例的写回权限，保留历史和主动断开意愿。"""
    from axile.server.connection_runtime import clear_sessions

    clear_sessions()
    await native_operation("connection_recover", {})


def end_connection_instance(
    binding: ConnectionBinding | None, message: str, error: BaseException | None = None
) -> None:
    """终止当前实例并撤销迟到响应；IPC 故障作为连接历史保留。"""
    if binding is None:
        return
    from axile.executor.algorithms.utils.clock import clock_now_iso
    from axile.server.connection_runtime import end_session
    from axile.server.error_details import exception_details

    end_session(binding, message, failed=error is not None)
    try:
        failure = (
            {"observed_at": clock_now_iso(), "message": message, "error": exception_details(error)}
            if error is not None
            else None
        )
        result = native_operation_sync("connection_end", {"binding": asdict(binding), "last_failure": failure})
        if result["applied"]:
            from axile.server.connection_events import connection_hub

            connection_hub.changed(binding.account_id)
    except Exception:
        logger.exception("账户会话结束状态保存失败 account_id={}", binding.account_id)
