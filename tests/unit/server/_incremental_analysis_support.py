"""隔离 Python SQLite 与原生 SQLite 的文件连接。"""

from contextlib import closing

from tests.sqlite_oracle import connect, sqlite_oracle


@sqlite_oracle
def sql(path, statement, args=()):
    with closing(connect(path)) as c, c:
        return [tuple(r) for r in c.execute(statement, args)]
