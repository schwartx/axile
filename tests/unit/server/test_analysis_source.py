"""Analysis derives canonical facts without persistent intermediate input tables."""

from tests.unit.server._migration_support import _connect


def test_runtime_metadata_contains_only_business_inputs_and_source_identity(native_database):
    with _connect(native_database) as connection:
        tables = {row[0] for row in connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not any(name.startswith(("analysis_source_", "replay_observation")) for name in tables)
    assert "executerecord" in tables
    assert "analysis_source" in tables
    assert "performance_cost_execution" not in tables
    assert "performance_cost_trade" not in tables
