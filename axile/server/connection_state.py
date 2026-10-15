"""主服务保存最新连接观察；worker 仅通过 IPC 提供观察。"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from functools import lru_cache
from typing import Any, cast
from uuid import uuid4

from loguru import logger
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlmodel import Session, col, select

from axile.common.config import settings
from axile.executor.connection_presentation import presentation_snapshot
from axile.executor.connection_state import (
    ConnectionObservation,
    connection_observer_scope,
    current_connection_observer,
)
from axile.server.db.models.account_connection import (
    AccountConnectionDetail,
    AccountConnectionPublic,
    AccountConnectionState,
)


def connection_config_key(channel: object, config: object) -> str:
    """比较规范化连接配置；签名不公开，也不写日志。"""
    value = json.dumps([str(channel), config], sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(value.encode()).hexdigest()


@lru_cache(maxsize=1)
def connection_engine() -> Engine:
    """为主服务后台线程提供独立同步连接，不复用请求事务。"""
    engine = create_engine(str(settings.sqlalchemy_database_uri).replace("sqlite+aiosqlite:", "sqlite:"))

    @event.listens_for(engine, "connect")
    def configure(connection, _record) -> None:
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    return engine


@dataclass(frozen=True)
class ConnectionBinding:
    """一个配置版本下最新获准上报的运行时实例。"""

    account_id: int
    config_key: str
    instance_id: str
    manually_disconnected: bool = False


def _saved_key(session: Session, account_id: int) -> str | None:
    from axile.server.db.models.account import Account
    from axile.server.db.models.account_settings import AccountSettings

    row = session.exec(
        select(Account.trade_channel, AccountSettings.connection)
        .join(AccountSettings, col(AccountSettings.account_id) == col(Account.id))
        .where(col(Account.id) == account_id)
    ).first()
    return connection_config_key(*row) if row is not None else None


def begin_connection_instance(account: object) -> ConnectionBinding | None:
    """注册最新实例，拒绝使用过期配置的调用方；重连不提前清除旧异常。"""
    account_id = getattr(account, "id", None)
    if account_id is None:
        return None
    key = connection_config_key(getattr(account, "trade_channel"), getattr(account, "account_config"))
    try:
        with Session(connection_engine()) as session:
            if _saved_key(session, account_id) != key:
                return None
            state = session.get(AccountConnectionState, account_id)
            if state is None:
                state = AccountConnectionState(account_id=account_id, config_key=key)
            if state.config_key != key:
                state.config_key = key
                state.status, state.observed_at, state.message, state.error = "unknown", None, None, None
                state.label, state.presentation = None, None
                state.last_success_at, state.last_failure, state.manually_disconnected = None, None, False
            state.instance_id, state.sequence = uuid4().hex, 0
            session.add(state)
            session.commit()
            return ConnectionBinding(account_id, key, state.instance_id, state.manually_disconnected)
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
        with Session(connection_engine()) as session:
            state = session.get(AccountConnectionState, binding.account_id)
            if (
                state is None
                or state.config_key != binding.config_key
                or state.instance_id != binding.instance_id
                or state.sequence >= sequence
                or _saved_key(session, binding.account_id) != binding.config_key
            ):
                return
            values = asdict(observation)
            values.pop("kind", None)
            if observation.kind == "access" and observation.status == "ok":
                values["last_success_at"] = observation.observed_at
            if observation.status in {"error", "cooldown"}:
                values["last_failure"] = {
                    key: values[key] for key in ("observed_at", "message", "label", "error", "presentation")
                }
            if history:
                values.update({key: value for key, value in history.items() if value is not None})
            if observation.kind == "session":
                if observation.status == "disconnected":
                    values["manually_disconnected"] = True
                elif observation.status in {"ok", "connecting", "recovering"}:
                    values["manually_disconnected"] = False
            # 条件 UPDATE 同时防止监控响应与业务响应竞争，把旧序号写回。
            from sqlalchemy import update

            session.execute(
                update(AccountConnectionState)
                .where(
                    col(AccountConnectionState.account_id) == binding.account_id,
                    col(AccountConnectionState.config_key) == binding.config_key,
                    col(AccountConnectionState.instance_id) == binding.instance_id,
                    col(AccountConnectionState.sequence) < sequence,
                )
                .values(sequence=sequence, **values)
            )
            session.commit()
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
        self._session: ConnectionObservation | None = None
        self._last_success_at: str | None = None
        self._last_failure: dict[str, object] | None = None

    def observe(self, observation: ConnectionObservation) -> None:
        """序列化观察来源；同一观察回放不刷新确认时间。"""
        with self._lock:
            if observation == self._latest:
                return
            self._sequence += 1
            self._latest = observation
            if observation.kind == "session":
                self._session = observation
            elif observation.status == "error" and self._session is not None and self._session.status == "ok":
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
            return self._session is not None and self._session.status == "error"

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
    from sqlalchemy import update

    from axile.server.connection_runtime import clear_sessions
    from axile.server.core.db import SessionLocal

    clear_sessions()
    async with SessionLocal() as session:
        await session.execute(update(AccountConnectionState).values(instance_id="", sequence=0))
        await session.commit()


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
        from sqlalchemy import update

        with Session(connection_engine()) as session:
            values: dict[str, object] = {"instance_id": ""}
            if error is not None:
                values["last_failure"] = {
                    "observed_at": clock_now_iso(),
                    "message": message,
                    "error": exception_details(error),
                }
            session.execute(
                update(AccountConnectionState)
                .where(
                    col(AccountConnectionState.account_id) == binding.account_id,
                    col(AccountConnectionState.instance_id) == binding.instance_id,
                    col(AccountConnectionState.config_key) == binding.config_key,
                )
                .values(**values)
            )
            session.commit()
    except Exception:
        logger.exception("账户会话结束状态保存失败 account_id={}", binding.account_id)


async def reset_connection_for_config(session, account: object) -> None:
    """配置实际变化时，在同一提交中清除旧结果并撤销旧实例。"""
    key = connection_config_key(getattr(account, "trade_channel"), getattr(account, "account_config"))
    from sqlalchemy.dialects.sqlite import insert

    values = dict(
        config_key=key,
        instance_id="",
        sequence=0,
        status="unknown",
        observed_at=None,
        message=None,
        error=None,
        label=None,
        presentation=None,
        last_success_at=None,
        last_failure=None,
        manually_disconnected=False,
    )
    statement = insert(AccountConnectionState).values(account_id=getattr(account, "id"), **values)
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=["account_id"], set_=values, where=col(AccountConnectionState.config_key) != key
        )
    )
