"""独立 worker 与 SDK 回调共写真实文件库时不持锁等待或丢失提交。"""

import asyncio
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from threading import Event
from unittest.mock import MagicMock

import pytest

from axile.executor.order_submissions import OrderSubmissionFact
from axile.server import order_submissions as submissions
from axile.server.execution import account_runtime_sync as runtime
from tests.conftest import require_native_store
from tests.unit.server._runtime_db_support import runtime_database

pytestmark = pytest.mark.slow

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_WRITE_CONTROL_RECORD = """
import sqlite3, sys
from contextlib import closing
with closing(sqlite3.connect(sys.argv[1], timeout=0.5)) as connection:
    with connection:
        connection.execute('''INSERT INTO account_control_counter_delta
            (account_id, execution_id, control_date, bucket_type, bucket_start,
             scope_type, operation, delta_count, delta_uid)
            VALUES (1, 'prepare-probe', '2026-10-23', 'DAY', '2026-10-23',
                    'ACCOUNT', 'query_order', 1, 'prepare-probe')''')
    assert connection.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
    assert connection.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] == 0
"""
_WRITE_SUBMISSIONS = """
import sys
from axile.executor.order_submissions import OrderSubmissionFact
from axile.server.order_submissions import _save_fact
print('ready', flush=True)
for index in range(8):
    fact = OrderSubmissionFact(
        submission_uid=f'worker-{index}', account_id=1, source='script', symbol='A',
        order_id=f'worker-{index}', request_id=index, occurred_at_ms=1_792_719_000_000,
        send_status='accepted',
    )
    _save_fact(sys.argv[1], fact)
for revision in (0, 6, 2, 8):
    _save_fact(sys.argv[1], fact.model_copy(update={
        'submission_uid': 'shared', 'order_id': 'shared', 'request_id': 999,
        'revision': revision,
    }))
"""
_HOLD_WRITE_LOCK = """
import sqlite3, sys
from contextlib import closing
with closing(sqlite3.connect(sys.argv[1])) as connection:
    connection.execute('BEGIN IMMEDIATE')
    print('locked', flush=True)
    sys.stdin.buffer.read(1)
    connection.rollback()
"""


@pytest.mark.parametrize("wal", [False, True])
def test_worker_process_can_commit_during_prepare(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wal: bool) -> None:
    """两种初始日志模式下，准备渠道时无写锁或阻止 checkpoint 的读事务。"""

    async def scenario() -> None:
        async with runtime_database(tmp_path / "runtime.db", wal=wal) as database:

            async def prepare(*_args: object, **_kwargs: object) -> None:
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-c",
                    _WRITE_CONTROL_RECORD,
                    str(database),
                    cwd=_PROJECT_ROOT,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    _, error = await asyncio.wait_for(process.communicate(), 5)
                    assert process.returncode == 0, error.decode()
                finally:
                    if process.returncode is None:
                        process.kill()
                        await process.communicate()

            monkeypatch.setattr(runtime, "reconcile_china_channel_account", prepare)
            sync = await runtime.reconcile_account_runtime(1, MagicMock())
            assert sync.status == "synchronized"
            with closing(sqlite3.connect(database)) as connection:
                assert connection.execute("SELECT delta_count FROM account_control_counter_delta").fetchone() == (1,)

    asyncio.run(scenario())


def test_worker_and_sdk_callbacks_keep_all_commits_and_latest_revision(tmp_path: Path) -> None:
    """原生忙等待释放 GIL；独立进程和回调线程排队后各自提交完整事实。"""
    require_native_store("save_order_submission")

    async def scenario() -> None:
        async with runtime_database(tmp_path / "runtime.db") as database:
            started = Event()

            def sdk_callbacks() -> None:
                started.set()
                for index in range(8):
                    fact = OrderSubmissionFact(
                        submission_uid=f"sdk-{index}",
                        account_id=1,
                        source="script",
                        symbol="A",
                        order_id=f"sdk-{index}",
                        request_id=index,
                        occurred_at_ms=1_792_719_000_000,
                        send_status="accepted",
                    )
                    submissions._save_fact(str(database), fact)
                for revision in (1, 7, 3, 10):
                    submissions._save_fact(
                        str(database),
                        fact.model_copy(
                            update={
                                "submission_uid": "shared",
                                "order_id": "shared",
                                "request_id": 999,
                                "revision": revision,
                                "broker_status": "rejected" if revision == 10 else "unknown",
                            }
                        ),
                    )

            blocker = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                _HOLD_WRITE_LOCK,
                str(database),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            process, callbacks = None, None
            try:
                assert blocker.stdout is not None
                assert await asyncio.wait_for(blocker.stdout.readline(), 5) == b"locked\n"
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-c",
                    _WRITE_SUBMISSIONS,
                    str(database),
                    cwd=_PROJECT_ROOT,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                assert process.stdout is not None
                assert await asyncio.wait_for(process.stdout.readline(), 5) == b"ready\n"
                callbacks = asyncio.create_task(asyncio.to_thread(sdk_callbacks))
                assert await asyncio.to_thread(started.wait, 2)
            finally:
                assert blocker.stdin is not None
                blocker.stdin.close()
                try:
                    _, error = await asyncio.wait_for(blocker.communicate(), 5)
                    assert blocker.returncode == 0, error.decode()
                finally:
                    if blocker.returncode is None:
                        blocker.kill()
                        await blocker.communicate()
                    try:
                        if callbacks is not None:
                            await asyncio.wait_for(callbacks, 10)
                        if process is not None:
                            _, error = await asyncio.wait_for(process.communicate(), 10)
                            assert process.returncode == 0, error.decode()
                    finally:
                        if process is not None and process.returncode is None:
                            process.kill()
                            await process.communicate()

            with closing(sqlite3.connect(database)) as connection:
                assert connection.execute("SELECT COUNT(*) FROM order_submission").fetchone() == (17,)
                revision, encoded = connection.execute(
                    "SELECT revision, data FROM order_submission WHERE submission_uid='shared'"
                ).fetchone()
                assert revision == 10
                assert json.loads(encoded)["broker_status"] == "rejected"
                assert connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0

    asyncio.run(scenario())
