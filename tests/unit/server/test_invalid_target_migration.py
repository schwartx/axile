"""Malformed historical target values survive upgrade and downgrade as evidence."""

import json

import pytest
import sqlalchemy as sa

from tests.conftest import native_store_call, require_native_store
from tests.unit.server.test_analysis_structured_migration import _connect, _downgrade, _upgrade
from tests.unit.server.test_analysis_structured_migration import database as structured_database  # noqa: F401
from tests.unit.server.test_summary_guard_migration import _schema, _seed_legacy_evidence


def test_invalid_target_values_migrate_without_losing_original_evidence(request):
    database = request.getfixturevalue("structured_database")
    _upgrade(database, "0036")
    expected = {
        "raw_weights": {"bad": "not-a-number", "bool": False, "nested": {"value": 2}, "null": None, "ok": 0.5},
        "normalized_weights": {"bad": [1, 2], "zero": 0},
    }
    with _connect(database) as connection:
        _seed_legacy_evidence(connection)
        connection.execute(
            sa.text(
                "INSERT INTO target_weight_snapshot(id,portfolio_id,account_id,execution_id,source,calculated_at,raw_weights,normalized_weights) "
                "VALUES(203,11,2,'synthetic-4','execution','2026-01-01',:raw,:normalized)"
            ),
            {"raw": json.dumps(expected["raw_weights"]), "normalized": json.dumps(expected["normalized_weights"])},
        )
        # 摘要压缩在权重回填之后，模拟已生成明细丢失审计证据。
        connection.exec_driver_sql(
            "CREATE TRIGGER discard_target_evidence AFTER UPDATE OF content ON execution_artifact "
            "WHEN NEW.artifact_type='EXECUTION_SUMMARY' BEGIN "
            "UPDATE target_weight_snapshot_weight SET audit_evidence='{}' "
            "WHERE snapshot_id=203 AND role='raw_weights' AND map_key='bad'; END"
        )
        original_schema = _schema(connection)
    with pytest.raises(
        require_native_store("migration_upgrade").NativeError, match="Conversion mismatch target_weight_snapshot"
    ):
        _upgrade(database, "0037")
    with _connect(database) as connection:
        assert _schema(connection) == original_schema
        assert connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == "0036"
        assert (
            json.loads(
                connection.exec_driver_sql("SELECT raw_weights FROM target_weight_snapshot WHERE id=203").scalar_one()
            )
            == expected["raw_weights"]
        )
        connection.exec_driver_sql("DROP TRIGGER discard_target_evidence")
    _upgrade(database, "0037")
    actual = native_store_call(
        database, "target_operation", {"op": "latest_account", "account_id": 2, "portfolio_id": 11}
    )
    assert actual["raw_weights"]["bad"] is None
    assert actual["raw_weights"]["ok"] == 0.5
    evidence = actual["audit_evidence"]["invalid_target_values"]
    assert evidence["raw_weights"]["bad"]["invalid_fields"]["value"] == "not-a-number"
    assert evidence["normalized_weights"]["bad"]["invalid_fields"]["value"] == [1, 2]
    _downgrade(database, "0036")
    with _connect(database) as connection:
        restored = connection.execute(
            sa.text("SELECT raw_weights,normalized_weights FROM target_weight_snapshot WHERE id=203")
        ).one()
        assert json.loads(restored.raw_weights) == expected["raw_weights"]
        assert json.loads(restored.normalized_weights) == expected["normalized_weights"]
