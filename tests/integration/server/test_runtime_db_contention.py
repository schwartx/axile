"""独立进程写控制记录以及逐连接 SQLite 持久性配置回归。"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from axile.server.core.db import SQLITE_BUSY_TIMEOUT_MS, register_sqlite_concurrency_pragmas
from axile.server.execution import account_runtime_sync as runtime
from tests.unit.server._runtime_db_support import runtime_database

pytestmark = pytest.mark.slow

_WRITE_CONTROL_RECORD = """
import sqlite3, sys
with sqlite3.connect(sys.argv[1], timeout=0.5) as connection:
    connection.execute('''INSERT INTO account_control_counter_delta
        (account_id, execution_id, control_date, bucket_type, bucket_start,
         scope_type, operation, delta_count, delta_uid)
        VALUES (1, 'prepare-probe', '2026-09-14', 'DAY', '2026-09-14',
                'ACCOUNT', 'query_order', 1, 'prepare-probe')''')
"""


@pytest.mark.parametrize("wal", [False, True])
def test_worker_process_can_commit_during_prepare(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wal: bool) -> None:
    """主进程等待时子进程能以短超时提交，DELETE 与 WAL 均不形成循环等待。"""

    async def scenario() -> None:
        path = tmp_path / "runtime.db"
        async with runtime_database(path, wal=wal) as (factory, sessions):

            async def prepare(*_args: object, **_kwargs: object) -> None:
                assert all(not session.in_transaction() for session in sessions)
                process = await asyncio.create_subprocess_exec(
                    sys.executable, "-c", _WRITE_CONTROL_RECORD, str(path), stderr=asyncio.subprocess.PIPE
                )
                _, error = await asyncio.wait_for(process.communicate(), 5)
                assert process.returncode == 0, error.decode()

            monkeypatch.setattr(runtime, "reconcile_china_channel_account", prepare)
            sync = await runtime.reconcile_account_runtime(1, MagicMock(), session_factory=factory)
            assert sync.status == "synchronized"
            async with factory() as session:
                assert await session.scalar(text("SELECT delta_count FROM account_control_counter_delta")) == 1

    asyncio.run(scenario())


def test_engine_connections_enable_wal_full_and_busy_timeout(tmp_path: Path) -> None:
    """同时持有两个物理连接，并从独立进程验证相同注册逻辑。"""

    async def scenario() -> None:
        uri = f"sqlite+aiosqlite:///{tmp_path / 'pragma.db'}"
        engine = create_async_engine(uri)
        register_sqlite_concurrency_pragmas(engine)
        try:
            async with engine.connect() as first, engine.connect() as second:
                for connection in (first, second):
                    assert await connection.scalar(text("PRAGMA journal_mode")) == "wal"
                    assert await connection.scalar(text("PRAGMA busy_timeout")) == SQLITE_BUSY_TIMEOUT_MS
                    assert await connection.scalar(text("PRAGMA synchronous")) == 2
                    assert await connection.scalar(text("PRAGMA foreign_keys")) == 1
            script = """
import asyncio, sys
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from axile.server.core.db import register_sqlite_concurrency_pragmas
async def check():
    engine = create_async_engine(sys.argv[1])
    register_sqlite_concurrency_pragmas(engine)
    async with engine.connect() as conn:
        assert await conn.scalar(text("PRAGMA journal_mode")) == "wal"
        assert await conn.scalar(text("PRAGMA busy_timeout")) == 30000
        assert await conn.scalar(text("PRAGMA synchronous")) == 2
        assert await conn.scalar(text("PRAGMA foreign_keys")) == 1
    await engine.dispose()
asyncio.run(check())
"""
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-c", script, uri, stderr=asyncio.subprocess.PIPE
            )
            _, error = await asyncio.wait_for(process.communicate(), 10)
            assert process.returncode == 0, error.decode()
        finally:
            await engine.dispose()

    asyncio.run(scenario())
