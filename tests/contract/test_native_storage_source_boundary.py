"""第一方运行时代码不得重新引入 Python 数据库访问。"""

import ast
import re
from pathlib import Path

import pytest

_DRIVERS = {
    "_sqlite3",
    "aiosqlite",
    "alembic",
    "apsw",
    "pysqlite3",
    "sqlalchemy",
    "sqlite3",
    "sqlite_utils",
    "sqlmodel",
}
_SESSIONS = {
    "AsyncSession",
    "Session",
    "SessionDep",
    "SessionLocal",
    "SQLModel",
    "async_sessionmaker",
    "create_async_engine",
    "create_engine",
    "get_db",
    "get_session",
    "get_sync_session",
    "sessionmaker",
}
_DB_RECEIVERS = {"connection", "conn", "cursor", "db", "dbapi_connection", "engine", "session", "sync_connection"}
_SQL_PREFIX = re.compile(
    r"\s*(?:ALTER|ATTACH|BEGIN|CREATE|DELETE|DETACH|DROP|INSERT|PRAGMA|REPLACE|SELECT|UPDATE|VACUUM|WITH)\b", re.I
)


def _qualified_name(node: ast.AST, imports: dict[str, str]) -> str:
    if isinstance(node, ast.Name):
        return imports.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        return f"{_qualified_name(node.value, imports)}.{node.attr}"
    return ""


def _database_call(node: ast.Call, imports: dict[str, str]) -> str | None:
    call = _qualified_name(node.func, imports)
    if call in {"__import__", "importlib.import_module"} and node.args:
        module = node.args[0]
        if isinstance(module, ast.Constant) and isinstance(module.value, str):
            if _DRIVERS.intersection(module.value.split(".")):
                return f"dynamic Python database import {module.value}"
    if not isinstance(node.func, ast.Attribute):
        return None
    method = node.func.attr
    receiver = _qualified_name(node.func.value, imports).rsplit(".", 1)[-1].strip("_")
    first = node.args[0] if node.args else None
    sql = first.value if isinstance(first, ast.Constant) and isinstance(first.value, str) else ""
    if isinstance(first, ast.JoinedStr) and first.values and isinstance(first.values[0], ast.Constant):
        sql = str(first.values[0].value)
    if method in {"cursor", "exec_driver_sql", "executemany", "executescript", "begin_nested"} or (
        method in {"execute", "exec", "commit", "rollback"} and (receiver in _DB_RECEIVERS or _SQL_PREFIX.match(sql))
    ):
        return f"Python database call {call}"
    return None


def _source_violations(source: str) -> list[str]:
    tree = ast.parse(source)
    imports: dict[str, str] = {}
    violations: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(
                (item.asname or item.name.split(".")[0], item.name if item.asname else item.name.split(".")[0])
                for item in node.names
            )
        elif isinstance(node, ast.ImportFrom):
            imports.update((item.asname or item.name, f"{node.module}.{item.name}") for item in node.names)

    for node in ast.walk(tree):
        name = _qualified_name(node, imports)
        references = [name]
        if isinstance(node, ast.Import):
            modules = [item.name for item in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = [node.module or ""]
            references.extend(f"{node.module}.{item.name}" for item in node.names)
        else:
            modules = []
        for module in modules:
            if _DRIVERS.intersection(module.split(".")):
                violations.add(f"{node.lineno}: Python database import {module}")
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            references.append(node.name)
        for name in references:
            if name.rsplit(".", 1)[-1] in _SESSIONS and ("." not in name or name.startswith("axile.")):
                violations.add(f"{node.lineno}: legacy database session {name}")
        if isinstance(node, ast.Call) and (reason := _database_call(node, imports)):
            violations.add(f"{node.lineno}: {reason}")
    return sorted(violations)


def test_first_party_runtime_uses_native_storage() -> None:
    root = Path(__file__).parents[2]
    violations = []
    # 只扫描发行包；用户策略、第三方 SDK、测试 oracle 和开发夹具不在此目录。
    for path in sorted((root / "axile").rglob("*.py")):
        violations.extend(
            f"{path.relative_to(root)}:{item}" for item in _source_violations(path.read_text(encoding="utf-8"))
        )
    assert not violations, "Python database access remains:\n" + "\n".join(violations)


@pytest.mark.parametrize(
    "source",
    [
        "import sqlalchemy as sa",
        "from sqlalchemy.ext.asyncio import AsyncSession as S",
        "from sqlmodel import Field",
        "import sqlite3 as sqlite",
        "import aiosqlite",
        "from alembic import command",
        "from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore",
        "__import__('sqlite3')",
        "import importlib as loader\nloader.import_module('sqlalchemy')",
        "import importlib.util\nimportlib.import_module('sqlite3')",
        "from importlib import import_module as load\nload('aiosqlite')",
        "from axile.server.core.db import SessionLocal as unused",
        "from axile.server.api.deps import SessionDep as old_session",
        "async def get_db():\n    yield None",
        "from axile.server.core.db import SessionLocal as factory\nfactory()",
        "def get_session():\n    yield None",
        "connection.execute(query)",
        "session.commit()",
        "self._session.rollback()",
        "cursor.executescript(sql)",
        "wrapper.execute('SELECT 1')",
        "wrapper.execute(f'PRAGMA busy_timeout={timeout}')",
    ],
)
def test_scanner_rejects_python_database_access(source: str) -> None:
    assert _source_violations(source)


@pytest.mark.parametrize(
    "source",
    [
        "# import sqlalchemy\nvalue = 'sqlite3.connect()'",
        '"""session.execute(query)"""',
        "from requests import Session\nSession()",
        "from axile.executor.execution_session import ExecutionSession\nExecutionSession(executor)",
        "executor.execute(request)",
        "from axile.server.core.db import database_path\ndatabase_path()",
        "settings.sqlalchemy_database_uri",
    ],
)
def test_scanner_allows_domain_sessions_and_database_configuration(source: str) -> None:
    assert not _source_violations(source)
