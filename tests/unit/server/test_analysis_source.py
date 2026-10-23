"""Analysis derives canonical facts without persistent intermediate input tables."""

from sqlmodel import SQLModel

from axile.server.db.models import analysis  # noqa: F401


def test_runtime_metadata_contains_only_business_inputs_and_frozen_outputs():
    assert not any(name.startswith(("analysis_source_", "replay_observation")) for name in SQLModel.metadata.tables)
    assert "executerecord" in SQLModel.metadata.tables
    assert "performance_cost_execution" in SQLModel.metadata.tables
    assert "performance_cost_trade" in SQLModel.metadata.tables
