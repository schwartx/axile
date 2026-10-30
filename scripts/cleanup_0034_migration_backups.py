#!/usr/bin/env python3
"""一次性删除已迁至 0034 的历史迁移备份表；执行前停止服务。

用法：python scripts/cleanup_0034_migration_backups.py /path/to/axile.db
清理后不再支持旧错误码补丁的逐项回退，业务事实和版本号不变。
"""

import argparse
import sqlite3
from contextlib import closing
from pathlib import Path

BACKUP_TABLES = (
    "_futures_close_policy_backup",
    "axile_0013_error_backup",
    "axile_0014_market_value_backup",
    "axile_0015_tq_market_value_backup",
    "axon_0001_error_backup",
)


def cleanup(database: Path) -> None:
    """验证版本后在同一事务内清理，路径不存在时拒绝创建数据库。"""
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=rw", uri=True)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            versions = connection.execute("SELECT version_num FROM alembic_version").fetchall()
            if versions != [("0034",)]:
                raise ValueError(f"仅支持 0034 数据库，当前版本：{versions!r}")
            for table in BACKUP_TABLES:
                connection.execute(f'DROP TABLE IF EXISTS "{table}"')


def main() -> None:
    """读取目标路径并执行一次性清理。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path, help="需要清理的 SQLite 数据库路径")
    args = parser.parse_args()
    cleanup(args.database)
    print(f"已清理迁移备份表：{args.database}")


if __name__ == "__main__":
    main()
