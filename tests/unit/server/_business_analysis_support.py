"""真实原生迁移和业务阶段驱动的分析测试夹具。"""

import sqlite3
from contextlib import asynccontextmanager, closing
from datetime import datetime, timedelta
from graphlib import TopologicalSorter
from pathlib import Path
from shutil import copyfile
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.pool import NullPool

from tests.conftest import initialize_native_database, native_store_call


def analysis_record(*, input=None, result=None, **metadata):
    """构造可修改的测试输入对象，持久化须显式调用 persist_records。"""
    return SimpleNamespace(**metadata, input={} if input is None else input, result={} if result is None else result)


async def _release_oracle_connections(session) -> Path:
    """Python 与原生扩展的 SQLite 内核不能同时持有同一个文件连接。"""
    engine = session.bind
    assert isinstance(engine, AsyncEngine), "分析 oracle 要求绑定真实 AsyncEngine"
    path = engine.url.database
    assert path and path != ":memory:", "原生分析夹具要求文件库，请在建 Session 前调用 initialize_native_database"
    await session.commit()
    await session.close()
    await engine.dispose()
    return Path(path)


def install_analysis_triggers(connection):
    """验证真实迁移已安装触发器；不再从生产 Python 迁移模块补建。"""
    revision = connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
    assert revision == "0039", "请在打开测试事务前调用 initialize_native_database"
    count = connection.exec_driver_sql("SELECT COUNT(*) FROM sqlite_master WHERE type='trigger'").scalar_one()
    assert count >= 243, "测试库缺少真实迁移触发器"


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _record_tables(connection):
    names = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    tables = {}
    for name in names:
        if not name.startswith(("execution_", "business_")):
            continue
        columns = connection.execute(f"PRAGMA table_info({_quote(name)})").fetchall()
        if any(column[1] == "record_id" for column in columns):
            tables[name] = connection.execute(f"PRAGMA foreign_key_list({_quote(name)})").fetchall()
    dependencies = {
        name: {row[2] for row in keys if row[2] in tables and row[2] != name} for name, keys in tables.items()
    }
    return tuple(TopologicalSorter(dependencies).static_order())


def _move_record_id(connection, source, target, execution_id, tables):
    """显式历史主键仅在测试侧重排，保留全部真实所有权和失效触发器。"""
    if not connection.in_transaction:
        connection.execute("BEGIN IMMEDIATE")
    connection.execute("PRAGMA defer_foreign_keys=ON")
    connection.execute("UPDATE executerecord SET execution_id=NULL WHERE id=?", (source,))
    columns = [row[1] for row in connection.execute("PRAGMA table_info(executerecord)")]
    values = [
        ":target" if name == "id" else ":execution_id" if name == "execution_id" else _quote(name) for name in columns
    ]
    connection.execute(
        f"INSERT INTO executerecord ({','.join(map(_quote, columns))}) SELECT {','.join(values)} "
        "FROM executerecord WHERE id=:source",
        {"source": source, "target": target, "execution_id": execution_id},
    )
    for name in tables:
        connection.execute(f"UPDATE {_quote(name)} SET record_id=? WHERE record_id=?", (target, source))
    connection.execute("DELETE FROM executerecord WHERE id=?", (source,))


def _reset_records(connection, record_ids):
    for identity in record_ids:
        row = connection.execute("SELECT execution_id FROM executerecord WHERE id=?", (identity,)).fetchone()
        if row is None:
            continue
        # 重写仅用于构造历史失效场景；生产首条终态与输入冻结规则不变。
        if row[0] is not None:
            connection.execute(
                "DELETE FROM account_asset_snapshot WHERE execution_id=? AND source='execution'", (row[0],)
            )
        connection.execute("DELETE FROM executerecord WHERE id=?", (identity,))


async def persist_records(session, records):
    """真实原生阶段保存事实；测试侧只负责历史 ID 和显式重置。"""
    records = list(records)
    if not records:
        return
    path = await _release_oracle_connections(session)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("PRAGMA foreign_keys=ON")
        tables = _record_tables(connection)
        _reset_records(connection, [record.id for record in records if record.id is not None])
    for record in records:
        saved = native_store_call(
            path,
            "persist_execute_record",
            {
                "account_id": record.account_id,
                "execution_id": record.execution_id,
                "created_at": record.created_at,
                "is_success": record.is_success,
                "input_id": str(uuid4()),
                "result_input_id": str(uuid4()),
                "raw_input": record.input,
                "raw_result": record.result,
            },
        )
        if record.id is not None and record.id != saved["id"]:
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("PRAGMA foreign_keys=ON")
                _move_record_id(connection, saved["id"], record.id, record.execution_id, tables)
        else:
            record.id = saved["id"]


async def load_record(session, record_id):
    """读取真实原生明细，返回可修改的普通对象。"""
    path = await _release_oracle_connections(session)
    with closing(sqlite3.connect(path)) as connection:
        row = connection.execute("SELECT account_id FROM executerecord WHERE id=?", (record_id,)).fetchone()
    assert row is not None, f"测试执行记录不存在: {record_id}"
    return SimpleNamespace(
        **native_store_call(
            path,
            "read_execution_detail",
            {
                "account_id": row[0],
                "record_id": record_id,
            },
        )
    )


async def persist_target(session, *, normalized_weights=None, raw_weights=None, snapshot=None, **metadata):
    """通过原生 append 保存目标头和权重明细，历史重写仅用于测试。"""
    from axile.server.db.models import TargetWeightSnapshot

    snapshot = snapshot or TargetWeightSnapshot(**metadata)
    path = await _release_oracle_connections(session)
    if snapshot.id is not None:
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("DELETE FROM target_weight_snapshot WHERE id=?", (snapshot.id,))
    saved = native_store_call(
        path,
        "target_operation",
        {
            "op": "append",
            "portfolio_id": snapshot.portfolio_id,
            "account_id": snapshot.account_id,
            "source": snapshot.source,
            "execution_id": snapshot.execution_id,
            "calculated_at": snapshot.calculated_at,
            "raw_weights": raw_weights,
            "normalized_weights": normalized_weights,
        },
    )
    if snapshot.id is not None and snapshot.id != saved["id"]:
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("PRAGMA defer_foreign_keys=ON")
            connection.execute("UPDATE target_weight_snapshot SET execution_id=NULL WHERE id=?", (saved["id"],))
            columns = [row[1] for row in connection.execute("PRAGMA table_info(target_weight_snapshot)")]
            values = [
                ":target" if name == "id" else ":execution_id" if name == "execution_id" else _quote(name)
                for name in columns
            ]
            connection.execute(
                f"INSERT INTO target_weight_snapshot ({','.join(map(_quote, columns))}) SELECT {','.join(values)} "
                "FROM target_weight_snapshot WHERE id=:source",
                {"source": saved["id"], "target": snapshot.id, "execution_id": snapshot.execution_id},
            )
            connection.execute(
                "UPDATE target_weight_snapshot_weight SET snapshot_id=? WHERE snapshot_id=?", (snapshot.id, saved["id"])
            )
            connection.execute("DELETE FROM target_weight_snapshot WHERE id=?", (saved["id"],))
        saved["id"] = snapshot.id
    return SimpleNamespace(**saved)


def record(index=1, **kwargs):
    return analysis_record(
        account_id=2,
        id=index,
        execution_id=f"exec-{index}",
        is_success=1,
        created_at=(datetime(2026, 1, 1, 9) + timedelta(days=index - 1)).isoformat(),
        input={"curr_target": {"A": 1}},
        result={
            "account_assets": {"total_asset": 100 + index},
            "symbol_results": {
                "A": {
                    "first_tick": {"bid_price": 100 + index, "ask_price": 100 + index},
                    "sizing": {"unit_multiplier": 10},
                    "orders": [{"order_id": "buy", "direction": "BUY"}],
                    "trades": [
                        {
                            "order_id": "buy",
                            "trade_price": 102 + index,
                            "trade_volume": 2,
                            "extra": {"commission": 0, "commission_asset": "CNY"},
                        }
                    ],
                }
            },
            **kwargs,
        },
    )


@pytest.fixture(scope="module")
def schema_template(tmp_path_factory):
    """模板包含真实迁移版本、索引和触发器，复制前关闭原生连接。"""
    path = tmp_path_factory.mktemp("analysis-schema") / "template.db"
    initialize_native_database(path)
    return path


@asynccontextmanager
async def database(tmp_path, schema_template, count=3, *, fresh_schema=False):
    """SQLAlchemy 只供测试 oracle；生产业务写入使用真实原生阶段。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from tests.unit.server._execution_test_support import build_account, insert_account

    path = tmp_path / "analysis.db"
    if fresh_schema:
        initialize_native_database(path)
    else:
        copyfile(schema_template, path)
    with closing(sqlite3.connect(path)) as connection, connection:
        insert_account(connection, build_account(id=2, portfolio_id=None, account_control_preset="default"))
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}", poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as session:
            await persist_records(session, [record(index + 1) for index in range(count)])
        yield engine, sessions, None
    finally:
        await engine.dispose()


def _events(records, bindings, skips) -> list[dict]:
    """事件标签 oracle:与 rust/analysis publication.rs 的事件派生保持同义。"""
    from collections.abc import Mapping

    from axile.executor.session_closed import session_closed_scope
    from axile.server.execution.legacy_compat import normalize_legacy_result
    from tests.unit.server._performance_costs_oracle import timestamp

    events = [
        {
            "time": binding.created_at,
            "tag": "换绑",
            "text": "解绑组合" if binding.portfolio_id is None else f"组合 #{binding.portfolio_id}",
            "executionId": None,
        }
        for binding in bindings
    ]
    events.extend(
        {
            "time": skip.triggered_at,
            "tag": "跳过",
            "text": "执行中" if skip.reason_code == "BUSY" else "休市",
            "executionId": None,
        }
        for skip in skips
    )
    for record in records:
        if record.is_success == 1:
            continue
        raw = normalize_legacy_result(record.result)
        if not isinstance(raw, Mapping):
            raise ValueError("Execution event result must be a mapping")
        status = raw.get("status")
        labels: dict[str | None, str] = {
            "SUCCEEDED": "调仓完成",
            "NOOP": "无需调仓",
            "PARTIAL": "执行未全部完成",
            "FAILED": "执行失败",
            "BLOCKED": "未执行",
        }
        label = labels.get(status, "执行状态未知")
        scope = session_closed_scope(raw)
        if scope == "all":
            label = "休市跳过"
        elif scope == "partial":
            label = "部分品种休市 · 执行未全部完成"
        if raw.get("task_status") == "TERMINATED":
            label = "执行已终止"
        reason = raw.get("error") if status in {"FAILED", "BLOCKED", "PARTIAL"} else None
        text = f"{label} · {reason}" if reason else label
        events.append(
            {
                "time": record.created_at,
                "tag": label,
                "text": text[:500],
                "executionId": record.execution_id,
            }
        )
    return sorted(events, key=lambda event: timestamp(event["time"]), reverse=True)
