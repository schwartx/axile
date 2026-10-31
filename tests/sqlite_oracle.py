"""让测试 SQL 与原生扩展使用不同进程的 SQLite 文件锁。"""

import asyncio
import atexit
import inspect
import os
import pickle
import select
import sqlite3
import subprocess
import sys
import threading
from contextlib import closing
from datetime import UTC, datetime, timedelta, timezone
from functools import wraps

_CHILD = """
import asyncio, contextlib, copyreg, importlib, inspect, pickle, sqlite3, sys
from tests.sqlite_oracle import _reduce_row
copyreg.pickle(sqlite3.Row, _reduce_row)
while True:
    try:
        module, name, arguments, keywords = pickle.load(sys.stdin.buffer)
    except EOFError:
        break
    try:
        with contextlib.redirect_stdout(sys.stderr):
            task = importlib.import_module(module)
            for part in name.split('.'):
                task = getattr(task, part)
            result = task(*arguments, **keywords)
            if inspect.isawaitable(result):
                result = asyncio.run(result)
        response = (True, result)
    except Exception as error:
        response = (False, error)
    pickle.dump(response, sys.stdout.buffer)
    sys.stdout.buffer.flush()
"""

_worker = None
_worker_pid = None
_worker_lock = threading.Lock()


def to_us(value: str | datetime) -> int:
    """精确编码测试标量时间，无时区值使用本机时区。"""
    current = datetime.fromisoformat(value) if isinstance(value, str) else value
    delta = current.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def utc_time(value: str) -> str:
    """生成公开接口使用的 UTC 微秒字符串。"""
    return datetime.fromisoformat(value).astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _canonical_time(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None)
    return parsed.isoformat(timespec="microseconds")


def register_time_function(connection, _connection_record=None):
    """测试写连接沿用原生索引的固定东八区时间及确定性声明。"""
    connection.create_function("axile_time", 1, _canonical_time, deterministic=True)


def _restore_row(names, values):
    with closing(sqlite3.connect(":memory:")) as connection:
        columns = ",".join('NULL AS "' + name.replace('"', '""') + '"' for name in names)
        cursor = connection.execute("SELECT " + columns)
        return sqlite3.Row(cursor, values)


def _reduce_row(row):
    return _restore_row, (row.keys(), tuple(row))


@atexit.register
def _close_worker():
    global _worker
    if _worker is None or _worker_pid != os.getpid():
        return
    _worker.stdin.close()
    try:
        _worker.wait(timeout=2)
    except subprocess.TimeoutExpired:
        _worker.kill()
        _worker.wait(timeout=2)
    _worker.stdout.close()
    _worker = None


def _call(task, arguments, keywords):
    global _worker, _worker_pid
    with _worker_lock:
        if _worker is None or _worker_pid != os.getpid():
            _worker = subprocess.Popen(
                [sys.executable, "-c", _CHILD],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                env={**os.environ, "AXILE_TEST_SQLITE_ORACLE": "1"},
            )
            _worker_pid = os.getpid()
        pickle.dump((task.__module__, task.__qualname__, arguments, keywords), _worker.stdin)
        _worker.stdin.flush()
        if not select.select([_worker.stdout], [], [], 5)[0]:
            _close_worker()
            raise TimeoutError("test SQLite oracle exceeded five seconds")
        succeeded, result = pickle.load(_worker.stdout)
    if not succeeded:
        raise result
    return result


def sqlite_oracle(task):
    """整批执行原有 SQL helper，保留字段类型、异常与事务边界。"""
    if inspect.iscoroutinefunction(task):

        @wraps(task)
        async def run_async(*arguments, **keywords):
            if os.environ.get("AXILE_TEST_SQLITE_ORACLE") == "1":
                return await task(*arguments, **keywords)
            return await asyncio.to_thread(_call, task, arguments, keywords)

        return run_async

    @wraps(task)
    def run(*arguments, **keywords):
        if os.environ.get("AXILE_TEST_SQLITE_ORACLE") == "1":
            return task(*arguments, **keywords)
        return _call(task, arguments, keywords)

    return run


_connections = {}
_cursors = {}


@sqlite_oracle
def _database_operation(kind, identity=None, name=None, arguments=(), keywords=None):
    if kind == "open":
        connection = sqlite3.connect(*arguments, **(keywords or {}))
        register_time_function(connection)
        identity = id(connection)
        _connections[identity] = connection
        return identity
    if kind == "connection_get":
        return getattr(_connections[identity], name)
    if kind == "connection_set":
        setattr(_connections[identity], name, arguments[0])
        return None
    if kind == "cursor_get":
        return getattr(_cursors[identity][1], name)
    if kind == "cursor":
        return getattr(_cursors[identity][1], name)(*arguments)
    connection = _connections[identity]
    if name == "enter":
        connection.__enter__()
        return None
    if name == "exit":
        return connection.__exit__(None, None, None) if arguments[0] else connection.rollback()
    result = getattr(connection, name)(*arguments)
    if name == "close":
        _connections.pop(identity)
        for cursor_id in [key for key, (owner, _) in _cursors.items() if owner == identity]:
            _cursors.pop(cursor_id)
    if isinstance(result, sqlite3.Cursor):
        cursor_id = id(result)
        _cursors[cursor_id] = (identity, result)
        return cursor_id
    return result


class _Cursor:
    def __init__(self, identity):
        self._identity = identity

    def __getattr__(self, name):
        return _database_operation("cursor_get", self._identity, name)

    def fetchone(self):
        return _database_operation("cursor", self._identity, "fetchone")

    def fetchall(self):
        return _database_operation("cursor", self._identity, "fetchall")

    def __iter__(self):
        return self

    def __next__(self):
        row = self.fetchone()
        if row is None:
            raise StopIteration
        return row


class _Connection:
    def __init__(self, identity):
        object.__setattr__(self, "_identity", identity)

    def __getattr__(self, name):
        return _database_operation("connection_get", self._identity, name)

    def __setattr__(self, name, value):
        _database_operation("connection_set", self._identity, name, (value,))

    def __enter__(self):
        _database_operation("connection", self._identity, "enter")
        return self

    def __exit__(self, exc_type, *_):
        _database_operation("connection", self._identity, "exit", (exc_type is None,))

    def execute(self, statement, parameters=()):
        return _Cursor(_database_operation("connection", self._identity, "execute", (statement, parameters)))

    def executescript(self, statement):
        return _Cursor(_database_operation("connection", self._identity, "executescript", (statement,)))

    def executemany(self, statement, parameters):
        return _Cursor(_database_operation("connection", self._identity, "executemany", (statement, list(parameters))))

    def setconfig(self, *arguments):
        return _database_operation("connection", self._identity, "setconfig", arguments)

    def commit(self):
        _database_operation("connection", self._identity, "commit")

    def rollback(self):
        _database_operation("connection", self._identity, "rollback")

    def close(self):
        _database_operation("connection", self._identity, "close")


def connect(database, *arguments, **keywords):
    """保留显式连接和游标生命周期，让原有 SQL 事务留在子进程。"""
    if database == ":memory:" or os.environ.get("AXILE_TEST_SQLITE_ORACLE") == "1":
        connection = sqlite3.connect(database, *arguments, **keywords)
        register_time_function(connection)
        return connection
    return _Connection(_database_operation("open", arguments=(database, *arguments), keywords=keywords))
