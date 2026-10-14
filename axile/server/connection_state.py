"""主服务保存最新连接观察；worker 仅通过 IPC 提供观察。"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from functools import lru_cache
from uuid import uuid4

from loguru import logger
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlmodel import Session, col, select

from axile.common.config import settings
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
            state.instance_id, state.sequence = uuid4().hex, 0
            session.add(state)
            session.commit()
            return ConnectionBinding(account_id, key, state.instance_id)
    except Exception:
        logger.exception("账户连接实例注册失败，不改变原始操作")
        return None


def record_connection_observation(
    binding: ConnectionBinding | None, sequence: int, observation: ConnectionObservation
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
                .values(sequence=sequence, **asdict(observation))
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

    def observe(self, observation: ConnectionObservation) -> None:
        """序列化观察来源；同一观察回放不刷新确认时间。"""
        with self._lock:
            if observation == self._latest:
                return
            self._sequence += 1
            self._latest = observation
            if self._persist:
                record_connection_observation(self.binding, self._sequence, observation)

    def snapshot(self) -> dict[str, object] | None:
        """只复制已发生的结果，采样不产生新的连接证据。"""
        with self._lock:
            if self.binding is None or self._latest is None:
                return None
            return {"binding": asdict(self.binding), "sequence": self._sequence, "observation": asdict(self._latest)}


def consume_connection_snapshot(snapshot: dict[str, object] | None) -> None:
    """主服务消费 worker 业务响应与空闲监控中的同一份快照。"""
    if snapshot is not None:
        try:
            record_connection_observation(
                ConnectionBinding(**snapshot["binding"]),  # type: ignore[arg-type]
                int(snapshot["sequence"]),  # type: ignore[arg-type]
                ConnectionObservation(**snapshot["observation"]),  # type: ignore[arg-type]
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
    """只读取已加载记录；配置不匹配时返回未知。"""
    state = getattr(account, "connection_state", None)
    model = AccountConnectionDetail if detail else AccountConnectionPublic
    key = connection_config_key(getattr(account, "trade_channel"), getattr(account, "account_config"))
    return (
        model.model_validate(state, from_attributes=True) if state is not None and state.config_key == key else model()
    )


async def reset_connection_for_config(session, account: object) -> None:
    """配置实际变化时，在同一提交中清除旧结果并撤销旧实例。"""
    key = connection_config_key(getattr(account, "trade_channel"), getattr(account, "account_config"))
    from sqlalchemy.dialects.sqlite import insert

    values = dict(
        config_key=key, instance_id="", sequence=0, status="unknown", observed_at=None, message=None, error=None
    )
    statement = insert(AccountConnectionState).values(account_id=getattr(account, "id"), **values)
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=["account_id"], set_=values, where=col(AccountConnectionState.config_key) != key
        )
    )
