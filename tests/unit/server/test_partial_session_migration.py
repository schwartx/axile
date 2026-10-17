"""历史休市迁移保留业务证据并排除真实失败。"""

import json
from copy import deepcopy
from importlib import import_module

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from tests.unit.server.test_session_closed_execution import closed_result

revision = import_module("axile.server.alembic.versions.0030_partial_session_tasks")


def test_migrate_only_unambiguous_partial_sessions():
    raw = closed_result()
    raw.update(status="PARTIAL", outcome="not_reached", outcome_reason=None)
    raw["symbol_results"]["ag2612"] = {"status": "NOOP", "target_volume": -6, "final_volume": -6}
    failed = deepcopy(raw)
    failed["symbol_results"]["ag2612"]["status"] = "FAILED"
    ordered = deepcopy(raw)
    ordered["symbol_results"]["m2701"]["orders"] = [{"order_id": "pending"}]
    cases = [
        (raw, "FAILED", None, None, 1),
        (failed, "FAILED", None, None, 1),
        (ordered, "FAILED", None, None, 1),
        (dict(raw, task_status="TERMINATED"), "FAILED", None, None, 1),
        (raw, "FAILED", "worker crashed", None, 1),
        (raw, "FAILED", None, "stop", 1),
        (raw, "RUNNING", None, None, 1),
        (raw, "FAILED", None, None, 2),
        ("malformed", "FAILED", None, None, 1),
    ]
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE execution_intent (execution_id TEXT, account_id INTEGER, status TEXT, "
            "error TEXT, cancel_requested_at TEXT, cancel_reason TEXT, terminate_mode TEXT)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE executerecord (id INTEGER PRIMARY KEY, execution_id TEXT, account_id INTEGER, "
            "is_success INTEGER, raw_result JSON)"
        )
        for index, (value, status, error, cancel, account) in enumerate(cases):
            connection.execute(
                sa.text("INSERT INTO execution_intent VALUES (:id, 1, :status, :error, NULL, :cancel, NULL)"),
                {"id": str(index), "status": status, "error": error, "cancel": cancel},
            )
            connection.execute(
                sa.text("INSERT INTO executerecord VALUES (:id, :exec, :account, 0, :raw)"),
                {"id": index, "exec": str(index), "account": account, "raw": json.dumps(value)},
            )
        with Operations.context(MigrationContext.configure(connection)):
            revision.upgrade()
            revision.upgrade()
        statuses = (
            connection.execute(sa.text("SELECT status FROM execution_intent ORDER BY execution_id")).scalars().all()
        )
        assert statuses == ["PARTIAL"] + [case[1] for case in cases[1:]]
        results = connection.execute(sa.text("SELECT raw_result FROM executerecord ORDER BY id")).scalars().all()
        migrated = json.loads(results[0])
        assert migrated == dict(raw, task_status="PARTIAL", outcome_reason="非交易时段")
        assert [json.loads(value) for value in results[1:]] == [case[0] for case in cases[1:]]
        assert connection.scalar(sa.text("SELECT sum(is_success) FROM executerecord")) == 0
        with Operations.context(MigrationContext.configure(connection)):
            revision.downgrade()
        assert connection.scalar(sa.text("SELECT status FROM execution_intent WHERE execution_id='0'")) == "FAILED"
        restored = json.loads(connection.scalar(sa.text("SELECT raw_result FROM executerecord WHERE id=0")))
        assert restored == dict(migrated, task_status="FAILED")
    engine.dispose()
