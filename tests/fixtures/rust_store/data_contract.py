"""非空历史迁移 oracle 的测试侧载入和逐行比较。"""

import json
import sqlite3


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def database_rows(connection: sqlite3.Connection, *, canonical: bool = True) -> dict:
    """忽略 JSON 对象顺序，保留 SQL NULL、JSON null 与数值种类。"""
    result = {}
    for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        if name.startswith("sqlite_"):
            continue
        columns = connection.execute(f"PRAGMA table_info({_quote(name)})").fetchall()
        names = [column[1] for column in columns]
        json_fields = {column[1] for column in columns if column[2].upper() == "JSON"}
        rows = []
        for values in connection.execute(f"SELECT * FROM {_quote(name)}"):
            row = dict(zip(names, values, strict=True))
            if canonical:
                for field in json_fields:
                    value = row[field]
                    if value is not None:
                        value = json.loads(value) if isinstance(value, str) else value
                        row[field] = [
                            "json",
                            json.dumps(
                                value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
                            ),
                        ]
            rows.append(row)
        if rows:
            result[name] = sorted(rows, key=lambda row: json.dumps(row, sort_keys=True, ensure_ascii=False))
    return result


def seed_database(connection: sqlite3.Connection, seed: dict) -> None:
    """仅为已初始化的测试 schema 设置历史数据，最后恢复精确的分析版本。"""
    for name in [*sorted(set(seed) - {"account_analysis"}), "account_analysis"]:
        rows = seed.get(name, [])
        if not rows:
            continue
        columns = list(rows[0])
        prefix = "INSERT OR REPLACE" if name in {"account_analysis", "alembic_version"} else "INSERT"
        connection.executemany(
            f"{prefix} INTO {_quote(name)} ({','.join(map(_quote, columns))}) "
            f"VALUES ({','.join('?' for _ in columns)})",
            [[row[column] for column in columns] for row in rows],
        )
