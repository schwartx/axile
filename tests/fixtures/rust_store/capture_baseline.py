"""从冻结源码生成迁移 oracle；运行时须把 PYTHONPATH 指向该源码。"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import tempfile
from pathlib import Path


def capture(source: Path, revision: str, commit: str, output: Path) -> None:
    """只迁移临时文件库，把完整 DDL 与源码哈希写入指定测试夹具。"""
    with tempfile.TemporaryDirectory(prefix="axile-schema-oracle-") as runtime:
        config = Path(runtime) / "config.toml"
        config.write_text(
            f'sqlalchemy_database_uri = "sqlite+aiosqlite:///{runtime}/oracle.db"\n'
            f'app_log_dir = "{runtime}/logs"\n'
            'environment = "local"\n',
            encoding="utf-8",
        )
        os.environ["AXILE_CONFIG_TOML"] = str(config)

        import sqlalchemy as sa
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        from alembic.script import ScriptDirectory

        import axile

        assert Path(axile.__file__).resolve() == source / "axile/__init__.py", axile.__file__
        versions = source / "axile/server/alembic/versions"
        scripts = ScriptDirectory(str(versions.parent))
        revisions = list(reversed(list(scripts.walk_revisions(base="base", head=revision))))
        assert [item.revision for item in revisions] == [f"{index:04}" for index in range(1, int(revision) + 1)]
        engine = sa.create_engine(f"sqlite:///{runtime}/oracle.db")
        try:
            with engine.begin() as connection:
                context = MigrationContext.configure(connection)
                for item in revisions:
                    spec = importlib.util.spec_from_file_location(f"oracle_migration_{item.revision}", item.path)
                    assert spec is not None and spec.loader is not None
                    migration = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(migration)
                    migration.op = Operations(context)
                    migration.upgrade()
                context.stamp(scripts, revision)
                assert connection.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
                assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
                rows = connection.exec_driver_sql(
                    "SELECT type,name,sql FROM sqlite_master "
                    "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY type,name"
                ).all()
                inspector = sa.inspect(connection)
                table_columns = {}
                for table_name in inspector.get_table_names():
                    primary = inspector.get_pk_constraint(table_name)["constrained_columns"]
                    table_columns[table_name] = [
                        {
                            "name": column["name"],
                            "type": str(column["type"]),
                            "nullable": column["nullable"],
                            "default": column["default"],
                            "primary_key": column["name"] in primary,
                        }
                        for column in inspector.get_columns(table_name)
                    ]
                ddl = (
                    f"-- Generated from frozen Axile {commit} (public head {revision}), empty SQLite.\n"
                    + "\n\n".join(f"{sql};" for _, _, sql in rows)
                    + "\n"
                )
        finally:
            engine.dispose()

    source_paths = sorted(
        path
        for folder in ("alembic", "migration_support", "business_facts", "db/models")
        for path in (source / "axile/server" / folder).rglob("*.py")
    )
    manifest = {
        "source_commit": commit,
        "public_head": revision,
        "revisions": [item.revision for item in revisions],
        "sql_sha256": hashlib.sha256(ddl.encode()).hexdigest(),
        "objects": {kind: sum(row[0] == kind for row in rows) for kind in ("table", "index", "trigger", "view")},
        "table_columns": table_columns,
        "source_sha256": {
            str(path.relative_to(source)): hashlib.sha256(path.read_bytes()).hexdigest() for path in source_paths
        },
    }
    output.write_text(ddl, encoding="utf-8")
    output.with_suffix(".json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {"source": str(source), "public_head": revision, "output": str(output), "objects": manifest["objects"]}
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    capture(args.source.resolve(), args.revision, args.commit, args.output.resolve())
