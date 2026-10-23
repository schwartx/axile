"""从冻结的迁移测试场景生成非空 0033 输入和 0039 结果。"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import tempfile
from pathlib import Path


def _rows(connection, *, canonical=False):
    result = {}
    for name in connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").scalars():
        if name.startswith("sqlite_"):
            continue
        quoted = '"' + name.replace('"', '""') + '"'
        json_fields = {
            row[1] for row in connection.exec_driver_sql(f"PRAGMA table_info({quoted})") if row[2].upper() == "JSON"
        }
        rows = []
        for source in connection.exec_driver_sql(f"SELECT * FROM {quoted}").mappings():
            row = dict(source)
            if canonical:
                for field in json_fields:
                    if row[field] is not None:
                        value = json.loads(row[field]) if isinstance(row[field], str) else row[field]
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


def capture(source: Path, output: Path) -> None:
    """在临时文件库运行旧迁移；输出只包含合成数据和来源信息。"""
    with tempfile.TemporaryDirectory(prefix="axile-data-oracle-") as runtime:
        config = Path(runtime) / "config.toml"
        config.write_text(
            f'sqlalchemy_database_uri = "sqlite+aiosqlite:///{runtime}/oracle.db"\n'
            f'app_log_dir = "{runtime}/logs"\nenvironment = "local"\n',
            encoding="utf-8",
        )
        os.environ["AXILE_CONFIG_TOML"] = str(config)

        import sqlalchemy as sa
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        from alembic.script import ScriptDirectory

        import axile
        from tests.unit.server import test_business_facts_migration_semantics as historical
        from tests.unit.server.test_analysis_cost_facts import _rich_projection
        from tests.unit.server.test_analysis_structured_migration import _ranges

        assert Path(axile.__file__).resolve() == source / "axile/__init__.py", axile.__file__
        versions = source / "axile/server/alembic/versions"
        scripts = ScriptDirectory(str(versions.parent))
        revisions = list(reversed(list(scripts.walk_revisions(base="base", head="0039"))))
        engine = sa.create_engine(f"sqlite:///{runtime}/oracle.db")
        try:
            with engine.begin() as connection:
                context = MigrationContext.configure(connection)
                for item in revisions:
                    spec = importlib.util.spec_from_file_location(f"oracle_data_{item.revision}", item.path)
                    assert spec is not None and spec.loader is not None
                    migration = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(migration)
                    migration.op = Operations(context)
                    if item.revision == "0034":
                        historical._seed_historical_records(connection)
                        historical._seed_historical_snapshots(connection)
                        historical._seed_summary_journals(connection)
                        historical._seed_notification_outbox_evidence(connection)
                        projected, trades = _rich_projection()
                        connection.execute(
                            sa.text(
                                "INSERT INTO performance_snapshot(id,account_id,source_version,logic_version,engine_version,"
                                "computed_at,settings,ranges) VALUES ('good',2,3,'17','equity-ledger-1','2026-01-03T00:00:00',"
                                ":settings,:ranges)"
                            ),
                            {
                                "settings": json.dumps({"backtest_weight_type": "cs", "backtest_fee_rate": 0.0002}),
                                "ranges": json.dumps(_ranges(), ensure_ascii=False),
                            },
                        )
                        for name, rows in (
                            ("performance_cost_execution", projected),
                            ("performance_cost_trade", trades),
                        ):
                            table = sa.Table(name, sa.MetaData(), autoload_with=connection)
                            connection.execute(table.insert(), [{"snapshot_id": "good", **row} for row in rows])
                        raw_input = historical._historical_documents()[3][1]
                        connection.execute(
                            sa.text(
                                "INSERT INTO execution_artifact(id,execution_id,artifact_type,schema_version,content,created_at) "
                                "VALUES (801,'synthetic-4','STANDARD_INPUT',1,:content,:created_at)"
                            ),
                            {"content": json.dumps({"input": raw_input}), "created_at": historical._CREATED_AT},
                        )
                        connection.execute(
                            sa.text(
                                "INSERT INTO execution_event(id,execution_id,account_id,channel,algorithm,schema_version,"
                                "event_uid,event_type,status,reason_family,reason_code,ts_local_created,ts_persisted,seq,details) "
                                "VALUES (901,'synthetic-4',2,'ctp','synthetic',1,'oracle-decision','DECISION','INFO','NONE','',"
                                ":created_at,:created_at,1,:details)"
                            ),
                            {
                                "created_at": historical._CREATED_AT,
                                "details": json.dumps({"decision": {"curr_target": {"A/B~C": 0}, "label": "frozen"}}),
                            },
                        )
                        context.stamp(scripts, "0033")
                        seed = _rows(connection)
                    migration.upgrade()
                context.stamp(scripts, "0039")
                assert connection.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
                assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
                expected = _rows(connection, canonical=True)
        finally:
            engine.dispose()

    used = [
        Path(historical.__file__),
        source / "tests/unit/server/test_analysis_cost_facts.py",
        source / "tests/unit/server/test_analysis_structured_migration.py",
    ]
    output.write_text(
        json.dumps(
            {
                "source_commit": "509b3627ca5fc6ee5f5a72e91b5ebebed3cf9879",
                "from_revision": "0033",
                "to_revision": "0039",
                "source_sha256": {
                    str(path.relative_to(source)): hashlib.sha256(path.read_bytes()).hexdigest() for path in used
                },
                "seed": seed,
                "expected": expected,
            },
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(output),
                "records": len(seed["executerecord"]),
                "seed_rows": sum(map(len, seed.values())),
                "expected_rows": sum(map(len, expected.values())),
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    capture(args.source.resolve(), args.output.resolve())
