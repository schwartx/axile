"""Malformed historical target values survive upgrade and downgrade as evidence."""

import json

import pytest
import sqlalchemy as sa

from axile.server.target_weight_snapshots import decode_snapshot_weights
from tests.unit.server.test_business_facts_migration_semantics import (
    _apply_0037,
    _migration_database,
    _seed_historical_snapshots,
)


def test_invalid_target_values_migrate_without_losing_original_evidence(tmp_path, monkeypatch):
    from axile.server.alembic import business0037_migration as conversion

    verify = conversion._verify_target_snapshots

    def verify_rejects_lost_evidence(connection):
        with connection.begin_nested() as savepoint:
            connection.execute(
                sa.text(
                    "UPDATE target_weight_snapshot_weight SET audit_evidence='{}' "
                    "WHERE snapshot_id=203 AND role='raw_weights' AND map_key='bad'"
                )
            )
            with pytest.raises(ValueError, match="Conversion mismatch target_weight_snapshot"):
                verify(connection)
            savepoint.rollback()
        verify(connection)

    monkeypatch.setattr(conversion, "_verify_target_snapshots", verify_rejects_lost_evidence)
    _, engine, _ = _migration_database(tmp_path)
    expected = {
        "raw_weights": {"bad": "not-a-number", "bool": False, "nested": {"value": 2}, "null": None, "ok": 0.5},
        "normalized_weights": {"bad": [1, 2], "zero": 0},
    }
    try:
        with engine.begin() as connection:
            _seed_historical_snapshots(connection)
            connection.execute(
                sa.text(
                    "UPDATE target_weight_snapshot SET raw_weights=:raw,normalized_weights=:normalized WHERE id=203"
                ),
                {"raw": json.dumps(expected["raw_weights"]), "normalized": json.dumps(expected["normalized_weights"])},
            )
            migration = _apply_0037(connection)
            metadata = sa.MetaData()
            metadata.reflect(connection, only=["target_weight_snapshot", "target_weight_snapshot_weight"])
            parent = metadata.tables["target_weight_snapshot"]
            child = metadata.tables["target_weight_snapshot_weight"]
            head = connection.execute(sa.select(parent).where(parent.c.id == 203)).mappings().one()
            children = connection.execute(sa.select(child).where(child.c.snapshot_id == 203)).mappings().all()
            actual = decode_snapshot_weights(head, children)
            assert actual["raw_weights"]["bad"] is None
            assert actual["raw_weights"]["ok"] == 0.5
            evidence = actual["audit_evidence"]["invalid_target_values"]
            assert evidence["raw_weights"]["bad"]["invalid_fields"]["value"] == "not-a-number"
            assert evidence["normalized_weights"]["bad"]["invalid_fields"]["value"] == [1, 2]
            migration.downgrade()
            restored = connection.execute(
                sa.text("SELECT raw_weights,normalized_weights FROM target_weight_snapshot WHERE id=203")
            ).one()
            assert json.loads(restored.raw_weights) == expected["raw_weights"]
            assert json.loads(restored.normalized_weights) == expected["normalized_weights"]
    finally:
        engine.dispose()
