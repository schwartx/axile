"""历史休市迁移保留业务证据并排除真实失败。"""

import json
from copy import deepcopy

import sqlalchemy as sa

from tests.conftest import initialize_native_database
from tests.unit.server._migration_support import _connect, _downgrade, _upgrade


def test_migrate_only_unambiguous_partial_sessions(tmp_path):
    raw = {
        "status": "PARTIAL",
        "reason_code": "COMMON.SESSION.CLOSED",
        "error": "非交易时段",
        "success": False,
        "outcome": "not_reached",
        "outcome_reason": None,
        "symbol_results": {
            "m2701": {
                "status": "BLOCKED",
                "reason_code": "COMMON.SESSION.CLOSED",
                "target_volume": -38,
                "final_volume": None,
                "orders": [],
                "trades": [],
            }
        },
    }
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
    database = tmp_path / "partial-session.db"
    initialize_native_database(database, revision="0029")
    with _connect(database) as connection:
        for index, (value, status, error, cancel, account) in enumerate(cases):
            connection.execute(
                sa.text(
                    "INSERT INTO execution_intent(execution_id,account_id,status,error,cancel_reason,kind,trigger_source,payload,created_at) "
                    "VALUES(:id,1,:status,:error,:cancel,'execute','manual','{}','2026-01-01')"
                ),
                {"id": str(index), "status": status, "error": error, "cancel": cancel},
            )
            connection.execute(
                sa.text(
                    "INSERT INTO executerecord(id,execution_id,account_id,is_success,raw_result,raw_input,created_at) "
                    "VALUES(:id,:exec,:account,0,:raw,'{}','2026-01-01')"
                ),
                {"id": index, "exec": str(index), "account": account, "raw": json.dumps(value)},
            )
    _upgrade(database, "0030")
    _upgrade(database, "0030")
    with _connect(database) as connection:
        statuses = (
            connection.execute(sa.text("SELECT status FROM execution_intent ORDER BY execution_id")).scalars().all()
        )
        assert statuses == ["PARTIAL"] + [case[1] for case in cases[1:]]
        results = connection.execute(sa.text("SELECT raw_result FROM executerecord ORDER BY id")).scalars().all()
        migrated = json.loads(results[0])
        assert migrated == dict(raw, task_status="PARTIAL", outcome_reason="非交易时段")
        assert [json.loads(value) for value in results[1:]] == [case[0] for case in cases[1:]]
        assert connection.scalar(sa.text("SELECT sum(is_success) FROM executerecord")) == 0
    _downgrade(database, "0029")
    with _connect(database) as connection:
        assert connection.scalar(sa.text("SELECT status FROM execution_intent WHERE execution_id='0'")) == "FAILED"
        restored = json.loads(connection.scalar(sa.text("SELECT raw_result FROM executerecord WHERE id=0")))
        assert restored == dict(migrated, task_status="FAILED")
