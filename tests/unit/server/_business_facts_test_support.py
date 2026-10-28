"""真实原生业务阶段与测试侧 SQL 事实检查；每次调用前关闭 SQLite 连接。"""

from contextlib import closing
from uuid import uuid4

import sqlalchemy as sa

from axile.server.business_facts.artifacts import make_artifact_reference
from tests.conftest import native_store_call
from tests.fixtures.rust_store.oracle_tables import table
from tests.sqlite_oracle import connect as oracle_connect
from tests.sqlite_oracle import register_time_function, sqlite_oracle
from tests.unit.server._native_read_views import persist_record_fixture, read_view


@sqlite_oracle
def sql(path, statement, parameters=()):
    with closing(oracle_connect(path)) as connection, connection:
        connection.execute(statement, parameters)


@sqlite_oracle
def rows(path, name):
    engine = sa.create_engine(f"sqlite:///{path}")
    sa.event.listen(engine, "connect", register_time_function)
    try:
        with engine.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    sa.select(sa.Table(name, sa.MetaData(), autoload_with=connection))
                ).mappings()
            ]
    finally:
        engine.dispose()


def seed_account(path, account_id=1):
    sql(
        path,
        "INSERT OR IGNORE INTO account (id,name,market,trade_channel,brokerage,created_at,updated_at) "
        "VALUES (?,'oracle','期货','ctp','','2026-10-20','2026-10-20')",
        (account_id,),
    )


@sqlite_oracle
def seed_historical_input(path, input_id, owner_execution_id, **columns):
    """直接建立历史输入事实，不借用当前运行时的写入协议或编码器。"""
    values = {
        "id": input_id,
        "account_id": 1,
        "execution_id": None,
        "owner_execution_id": owner_execution_id,
        "created_at": "2026-10-20",
        "root_kind": "object",
        "curr_target_state": "absent",
        "last_target_state": "absent",
        "strategy_target_state": "absent",
        "forbidden_symbols_state": "absent",
        "risk_symbols_state": "absent",
        "sizing_context_state": "absent",
        "quantities_state": "absent",
        "target_units_state": "absent",
        "audit_evidence": {},
        **columns,
    }
    engine = sa.create_engine(f"sqlite:///{path}")
    sa.event.listen(engine, "connect", register_time_function)
    try:
        with engine.begin() as connection:
            connection.execute(table("execution_input").insert(), values)
    finally:
        engine.dispose()
    return input_id


def audit_artifacts(path, execution_id):
    """独立读取审计行，再通过生产解析器核对原始附件证据。"""
    data = sorted(
        (row for row in rows(path, "execution_artifact") if row["execution_id"] == execution_id),
        key=lambda row: row["id"],
    )
    for row in data:
        row["content"] = read_view(path, "resolve_execution_artifact", {"artifact": row})
    return {"data": data, "count": len(data)}


def persist_result(path, payload, **changes):
    seed_account(path)
    return persist_record_fixture(
        path,
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
    return read_view(path, "read_execution_result", {"account_id": 1, "record_id": record["id"]})


def read_before_assets(path, input_id):
    """通过已有审计引用读取冻结前观测，独立 SQL 同时确认唯一归属。"""
    (owner,) = [row for row in rows(path, "execution_input") if row["id"] == input_id]
    (observation,) = [
        row for row in rows(path, "business_asset_snapshot") if row["input_id"] == input_id and row["scope"] == "before"
    ]
    return read_view(
        path,
        "resolve_execution_artifact",
        {
            "account_id": owner["account_id"],
            "artifact": {
                "execution_id": owner["owner_execution_id"] or owner["execution_id"],
                "artifact_type": "account_snapshot_before",
                "content": make_artifact_reference(
                    view="assets_before", input_id=input_id, asset_id=observation["asset_id"]
                ),
            },
        },
    )["account_assets"]


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
