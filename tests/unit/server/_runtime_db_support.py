"""运行态测试使用原生建库，独立 SQLite 连接只负责准备和核验数据。"""

import json
import sqlite3
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager, closing
from pathlib import Path
from typing import Any
from unittest.mock import patch

from axile.server.db.models import AccountRuntimeSync
from axile.server.native_store import native_operation
from tests.unit.server._execution_test_support import build_account


def runtime_rows(path: Path, statement: str, parameters: Sequence[Any] = ()) -> list[sqlite3.Row]:
    """每次重新连接并提交，短超时使生产事务泄漏直接暴露为测试失败。"""
    with closing(sqlite3.connect(path, timeout=0.5)) as connection, connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(statement, parameters).fetchall()


def runtime_sync(path: Path, account_id: int = 1) -> AccountRuntimeSync:
    """从独立连接观察生产操作已提交的同步状态。"""
    rows = runtime_rows(path, "SELECT * FROM accountruntimesync WHERE account_id=?", (account_id,))
    assert len(rows) == 1
    return AccountRuntimeSync.model_validate(dict(rows[0]))


@asynccontextmanager
async def runtime_database(path: Path, *, wal: bool = False) -> AsyncIterator[Path]:
    """提供两个账户与隔离文件库；生产入口自行持有原生连接和事务。"""
    with patch("axile.server.core.db.database_path", return_value=str(path)):
        await native_operation("migration_upgrade", {})
        with closing(sqlite3.connect(path)) as connection, connection:
            # 只固定初始模式；生产 writer 会按服务约定切换到 WAL。
            connection.execute(f"PRAGMA journal_mode={'WAL' if wal else 'DELETE'}")
            for account_id in (1, 2):
                account = build_account(id=account_id, portfolio_id=None)
                connection.execute(
                    "INSERT INTO account(id,name,market,trade_channel,remark,brokerage,updated_at,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (
                        account_id,
                        account.name,
                        account.market,
                        str(account.trade_channel),
                        account.remark,
                        account.brokerage,
                        account.updated_at,
                        account.created_at,
                    ),
                )
                settings = account.settings
                groups = [settings.connection] + [
                    getattr(settings, name).model_dump(mode="json")
                    for name in ("execution", "schedule", "notification", "backtest")
                ]
                connection.execute(
                    "INSERT INTO account_settings(account_id,connection,execution,schedule,notification,backtest) "
                    "VALUES (?,?,?,?,?,?)",
                    (account_id, *(json.dumps(group, ensure_ascii=False) for group in groups)),
                )
        yield path
