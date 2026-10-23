"""真实原生业务阶段与测试侧 SQL 事实检查；每次调用前关闭 SQLite 连接。"""

import sqlite3
from contextlib import closing
from uuid import uuid4

import sqlalchemy as sa

from tests.conftest import native_store_call
from tests.fixtures.rust_store.oracle_tables import table
from tests.sqlite_oracle import sqlite_oracle


@sqlite_oracle
def sql(path, statement, parameters=()):
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(statement, parameters)


@sqlite_oracle
def rows(path, name):
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        with engine.connect() as connection:
            return [dict(row) for row in connection.execute(sa.select(table(name))).mappings()]
    finally:
        engine.dispose()


def seed_account(path, account_id=1):
    sql(
        path,
        "INSERT OR IGNORE INTO account (id,name,market,trade_channel,brokerage,created_at,updated_at) "
        "VALUES (?,'oracle','期货','ctp','','2026-10-20','2026-10-20')",
        (account_id,),
    )


def persist_result(path, payload, **changes):
    seed_account(path)
    return native_store_call(
        path,
        "persist_execute_record",
        {
            "account_id": 1,
            "execution_id": None,
            "created_at": "2026-10-20",
            "is_success": 0,
            "input_id": str(uuid4()),
            "result_input_id": str(uuid4()),
            "raw_result": payload,
            **changes,
        },
    )


def read_result(path, record):
    return native_store_call(path, "read_execution_result", {"account_id": 1, "record_id": record["id"]})


def freeze_input(path, payload, **changes):
    seed_account(path)
    return native_store_call(
        path,
        "freeze_execution_input",
        {
            "account_id": 1,
            "execution_id": "execution-1",
            "input_id": "input-1",
            "created_at": "2026-10-20",
            "payload": payload,
            **changes,
        },
    )
