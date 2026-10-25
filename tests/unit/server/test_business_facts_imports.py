"""独立进程导入事实入口，原生建库不依赖 Python ORM 或驱动。"""

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "module", ["axile.server.business_facts.repository", "axile.server.native_store", "axile.server.db.models"]
)
def test_fact_entrypoints_register_schema_in_fresh_interpreter(module, tmp_path):
    root = Path(__file__).resolve().parents[3]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import importlib
import importlib.abc
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

class RejectORM(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname.split('.')[0] in {'sqlalchemy', 'sqlmodel', 'alembic', 'aiosqlite'}:
            raise AssertionError(f'原生事实入口加载了 Python ORM 或驱动: {fullname}')

sys.meta_path.insert(0, RejectORM())
import axile
assert Path(axile.__file__).resolve() == Path.cwd() / 'axile/__init__.py'
importlib.import_module(sys.argv[1])
from pydantic import BaseModel
from axile.server.db.models import Account, ExecuteRecord
from axile.server.native_store import native_operation_sync

for model in (Account, ExecuteRecord):
    assert issubclass(model, BaseModel)
    assert not hasattr(model, '__table__')
assert not {'sqlalchemy', 'sqlmodel', 'alembic', 'aiosqlite'} & sys.modules.keys()
native_operation_sync('migration_upgrade', {}, database=sys.argv[2])
schema = json.loads(Path('tests/fixtures/rust_store/public-0034.json').read_text())
with closing(sqlite3.connect(sys.argv[2])) as connection, connection:
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert set(schema['table_columns']) <= tables
    assert connection.execute('SELECT version_num FROM alembic_version').fetchall() == [('0034',)]
    assert connection.execute('PRAGMA foreign_key_check').fetchall() == []
""",
            module,
            str(tmp_path / "facts.db"),
        ],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root)},
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
