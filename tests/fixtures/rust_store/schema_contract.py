"""比较 SQLite 的结构语义，忽略 DDL 排版和标识符引号。"""

from __future__ import annotations

import re
import sqlite3

_TOKENS = re.compile(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`[^`]*`|\[[^]]*\]|[\w.]+|\S")


def load_schema(connection: sqlite3.Connection, ddl: str) -> None:
    """按依赖顺序载入 sqlite_master 导出的 DDL，触发器内部语句保持完整。"""
    statements, pending = [], ""
    for line in ddl.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            statements.append(pending)
            pending = ""
    assert not pending.strip(), "不完整的 SQLite DDL 夹具"
    for kind in ("TABLE", "INDEX", "VIEW", "TRIGGER"):
        for statement in statements:
            if re.search(rf"\bCREATE\s+(?:UNIQUE\s+)?{kind}\b", statement, re.IGNORECASE):
                connection.execute(statement)


def sql_tokens(sql: str | None) -> tuple[str, ...]:
    """保留字符串值，统一 SQLite 不区分大小写的名称与关键字。"""
    tokens = []
    for token in _TOKENS.findall(sql or ""):
        if token.startswith("'"):
            tokens.append(token)
        elif token.startswith(('"', "`", "[")):
            tokens.append(token[1:-1].replace('""', '"').lower())
        else:
            tokens.append(token.lower())
    return tuple(tokens)


def database_schema(connection: sqlite3.Connection) -> dict:
    """捕获列、默认值、唯一性、外键、索引及完整触发器条件。"""
    tables, triggers, views = {}, {}, {}
    objects = connection.execute(
        "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
    ).fetchall()
    for kind, name, sql in objects:
        quoted = '"' + name.replace('"', '""') + '"'
        if kind == "trigger":
            triggers[name] = sql_tokens(sql)
        elif kind == "view":
            views[name] = sql_tokens(sql)
        elif kind == "table":
            indexes = []
            for _, index_name, unique, origin, partial in connection.execute(f"PRAGMA index_list({quoted})"):
                index_quoted = '"' + index_name.replace('"', '""') + '"'
                columns = tuple(
                    (row[1], row[2], row[3], row[4], row[5])
                    for row in connection.execute(f"PRAGMA index_xinfo({index_quoted})")
                )
                definition_row = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE name=?", (index_name,)
                ).fetchone()
                definition = definition_row[0] if definition_row is not None else None
                indexes.append(
                    (index_name if origin == "c" else origin, unique, partial, columns, sql_tokens(definition))
                )
            foreign_keys = {}
            for identity, seq, parent, child, target, update, delete, match in connection.execute(
                f"PRAGMA foreign_key_list({quoted})"
            ):
                foreign_keys.setdefault(identity, []).append((seq, parent, child, target, update, delete, match))
            table_tokens = sql_tokens(sql)
            checks = []
            for index, token in enumerate(table_tokens):
                if token != "check" or table_tokens[index + 1 : index + 2] != ("(",):
                    continue
                depth, expression = 0, []
                for part in table_tokens[index + 1 :]:
                    expression.append(part)
                    depth += (part == "(") - (part == ")")
                    if depth == 0:
                        break
                checks.append(tuple(expression))
            tables[name] = {
                "columns": tuple(
                    (row[1], row[2].upper(), row[3], sql_tokens(row[4]), row[5], row[6])
                    for row in connection.execute(f"PRAGMA table_xinfo({quoted})")
                ),
                "foreign_keys": tuple(sorted(tuple(sorted(keys)) for keys in foreign_keys.values())),
                "indexes": tuple(sorted(indexes)),
                "checks": tuple(sorted(checks)),
                "without_rowid": ("without", "rowid") == table_tokens[-2:],
                "strict": table_tokens[-1:] == ("strict",),
            }
    return {"tables": tables, "triggers": triggers, "views": views}


def schema_differences(actual: dict, oracle: dict) -> list[str]:
    """逐对象报告严格结构差异，避免 pytest 展开整个数据库 schema。"""
    differences = []
    for section in ("tables", "triggers", "views"):
        left, right = actual[section], oracle[section]
        for name in sorted(right.keys() - left.keys()):
            differences.append(f"{section}.{name}: missing")
        for name in sorted(left.keys() - right.keys()):
            differences.append(f"{section}.{name}: unexpected")
        for name in sorted(left.keys() & right.keys()):
            if left[name] == right[name]:
                continue
            if section == "tables":
                for field in left[name]:
                    if left[name][field] != right[name][field]:
                        value = repr(left[name][field])
                        expected = repr(right[name][field])
                        differences.append(f"{section}.{name}.{field}: actual={value[:500]} oracle={expected[:500]}")
            else:
                offset = next(
                    (index for index, pair in enumerate(zip(left[name], right[name])) if pair[0] != pair[1]),
                    min(len(left[name]), len(right[name])),
                )
                differences.append(
                    f"{section}.{name}: token {offset} actual={left[name][offset : offset + 10]!r} "
                    f"oracle={right[name][offset : offset + 10]!r}"
                )
    return differences
