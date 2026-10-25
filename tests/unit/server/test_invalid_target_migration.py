"""Malformed historical target values survive upgrade and downgrade as evidence."""

import json

import sqlalchemy as sa

from tests.conftest import native_store_call
from tests.unit.server.test_analysis_structured_migration import _connect, _downgrade, _upgrade
from tests.unit.server.test_analysis_structured_migration import database as structured_database  # noqa: F401
from tests.unit.server.test_summary_guard_migration import _seed_legacy_evidence


def test_invalid_target_values_migrate_without_losing_original_evidence(request):
    database = request.getfixturevalue("structured_database")
    _upgrade(database, "0033")
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
    _upgrade(database, "0041")
    actual = native_store_call(
        database, "target_operation", {"op": "latest_account", "account_id": 2, "portfolio_id": 11}
    )
    assert actual["raw_weights"]["bad"] is None
    assert actual["raw_weights"]["ok"] == 0.5
    evidence = actual["audit_evidence"]["invalid_target_values"]
    assert evidence["raw_weights"]["bad"]["invalid_fields"]["value"] == "not-a-number"
    assert evidence["normalized_weights"]["bad"]["invalid_fields"]["value"] == [1, 2]
    _downgrade(database, "0033")
    with _connect(database) as connection:
        restored = connection.execute(
            sa.text("SELECT raw_weights,normalized_weights FROM target_weight_snapshot WHERE id=203")
        ).one()
        assert json.loads(restored.raw_weights) == expected["raw_weights"]
        assert json.loads(restored.normalized_weights) == expected["normalized_weights"]
