"""共享引擎跨循环、跨线程审计及 HTTP 查询的真实 SQLite 验收。"""

import asyncio
import sqlite3
import threading
from contextlib import closing
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlmodel import SQLModel

from axile.common.trade_channel import TradeChannel
from axile.domain.execution import (
    ExecutionArtifactType,
    ExecutionEventStatus,
    ExecutionEventType,
    ExecutionReasonFamily,
)
from axile.server import execution_audit
from axile.server.api import deps
from axile.server.api.routes import account_execution
from axile.server.core import db
from axile.server.execution import execution_account_control
from tests.unit.server._execution_test_support import build_account


def _event_kwargs(seq):
    return dict(
        execution_id="loop-audit",
        account_id=1,
        channel=TradeChannel.CTP,
        algorithm="SINGLE-MAKER",
        event_type=ExecutionEventType.SYMBOL_SKIPPED,
        status=ExecutionEventStatus.WARNING,
        reason_family=ExecutionReasonFamily.MARKET_RULE,
        reason_code="EXTERNAL.BELOW_MIN_NOTIONAL",
        seq=seq,
    )


@pytest.fixture
def audit_database(tmp_path, monkeypatch):
    assert isinstance(db.engine.pool, NullPool)
    path = tmp_path / "audit.sqlite"
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}", poolclass=type(db.engine.pool))
    db.register_sqlite_concurrency_pragmas(engine)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    counters = {"active": 0, "peak": 0, "opened": 0}
    lock = threading.Lock()
    threads = set()

    @event.listens_for(engine.sync_engine, "connect")
    def connected(connection, _record):
        driver = connection.driver_connection
        with lock:
            threads.add(driver if isinstance(driver, threading.Thread) else driver._thread)
            counters["active"] += 1
            counters["opened"] += 1
            counters["peak"] = max(counters["peak"], counters["active"])

    @event.listens_for(engine.sync_engine, "close")
    def disconnected(_connection, _record):
        with lock:
            counters["active"] -= 1

    async def initialize():
        async with engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)
        async with sessions() as session, session.begin():
            session.add(build_account(portfolio_id=None))

    asyncio.run(initialize())
    for module in (execution_audit, execution_account_control, deps):
        monkeypatch.setattr(module, "SessionLocal", sessions)
    yield path, engine, sessions, counters
    asyncio.run(engine.dispose())
    for thread in threads:
        thread.join(timeout=1)
        assert not thread.is_alive()
    assert counters["active"] == 0


def test_shared_engine_handles_repeated_loops_threads_control_and_http(audit_database):
    path, _engine, sessions, counters = audit_database

    def write(seq):
        assert execution_audit.append_execution_event_sync(**_event_kwargs(seq))
        assert execution_audit.append_execution_artifact_sync(
            execution_id=f"artifact-{seq}",
            artifact_type=ExecutionArtifactType.ENVIRONMENT_SNAPSHOT,
            content={"seq": seq},
        )

        async def control():
            guard = await execution_account_control.build_account_control_guard(
                build_account(portfolio_id=None),
                f"control-{seq}",
                register_monitor=False,
            )
            await execution_account_control.flush_account_control_records(
                SimpleNamespace(
                    get_account_control_guard=lambda: guard, export_account_control_records=lambda: ([], [])
                )
            )

        asyncio.run(control())

    # 同一引擎先在独立循环中重复使用，再与请求循环和多个 worker 并发。
    write(0)
    write(1)
    app = FastAPI()
    app.include_router(account_execution.router, prefix="/account")

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:

            async def queries():
                for _ in range(8):
                    response = await client.get("/account/executions/loop-audit/events")
                    assert response.status_code == 200

            await asyncio.gather(*(asyncio.to_thread(write, seq) for seq in range(2, 8)), queries())
            response = await client.get("/account/executions/loop-audit/events")
            assert response.json()["count"] == 8

    asyncio.run(scenario())
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM execution_artifact").fetchone()[0] == 8
        assert (
            connection.execute("SELECT COUNT(*) FROM account_control_session WHERE status='closed'").fetchone()[0] == 8
        )
    assert counters["active"] == 0
    assert counters["peak"] > 1


def test_pragmas_dedup_rollback_and_notification_follow_real_commits(audit_database, monkeypatch):
    path, _engine, sessions, counters = audit_database
    notifications = []

    def notify(**_kwargs):
        with closing(sqlite3.connect(path)) as connection:
            assert connection.execute("SELECT COUNT(*) FROM execution_event").fetchone()[0] == 1
        notifications.append(True)

    monkeypatch.setattr(execution_audit, "_notify_live_hub", notify)

    async def scenario():
        # 每次取用都创建新物理连接，各自应用持久性设置。
        for _ in range(3):
            async with sessions() as session:
                for name, expected in (
                    ("journal_mode", "wal"),
                    ("synchronous", 2),
                    ("foreign_keys", 1),
                    ("busy_timeout", 30000),
                ):
                    assert await session.scalar(text(f"PRAGMA {name}")) == expected
        kwargs = _event_kwargs(1)
        assert await execution_audit.append_execution_event(**kwargs)
        assert not await execution_audit.append_execution_event(**kwargs)
        with pytest.raises(IntegrityError, match="FOREIGN KEY"):
            await execution_audit.append_execution_event(
                **(kwargs | {"seq": 2, "account_id": 9999}), raise_on_error=True
            )
        artifact = dict(
            execution_id="artifact", artifact_type=ExecutionArtifactType.EXECUTION_SUMMARY, content={"ok": True}
        )
        assert await execution_audit.append_execution_artifact(**artifact)
        assert not await execution_audit.append_execution_artifact(**artifact)
        assert not await execution_audit.append_execution_artifact(
            **(artifact | {"execution_id": "invalid", "content": {"bad": object()}})
        )

    asyncio.run(scenario())
    assert notifications == [True]
    assert counters["active"] == 0
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM execution_event").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM execution_artifact").fetchone()[0] == 1


@pytest.mark.parametrize("cancel", [False, True])
def test_locked_write_timeout_or_cancel_cleans_connections(audit_database, monkeypatch, cancel):
    path, engine, _sessions, counters = audit_database
    entered = threading.Event()
    notifications = []
    monkeypatch.setattr(execution_audit, "_notify_live_hub", lambda **_: notifications.append(True))

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def writing(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("INSERT INTO execution_event"):
            entered.set()

    async def scenario():
        with closing(sqlite3.connect(path)) as locker:
            locker.execute("BEGIN IMMEDIATE")
            if cancel:
                task = asyncio.create_task(execution_audit.append_execution_event(**_event_kwargs(1)))
            else:
                task = asyncio.create_task(
                    asyncio.to_thread(
                        execution_audit._run_coroutine_sync,
                        lambda: execution_audit.append_execution_event(**_event_kwargs(1)),
                        timeout=0.01,
                    )
                )
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                if cancel:
                    task.cancel()
                # 写锁未释放期间，循环仍可响应，清理还在等待真实 SQLite 调用退出。
                await asyncio.wait({task}, timeout=0.03)
            finally:
                locker.rollback()
            with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
                await asyncio.wait_for(task, 2)
        assert counters["active"] == 0
        assert notifications == []
        assert await execution_audit.append_execution_event(**_event_kwargs(2))

    asyncio.run(scenario())
    assert counters["active"] == 0
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT seq FROM execution_event").fetchall() == [(2,)]
