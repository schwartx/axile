"""Migrate legacy business documents once to the canonical typed storage."""

import sqlalchemy as sa
from alembic import op

from axile.server.alembic import business0037_migration as conversion
from axile.server.alembic.business0037_invalidation import drop_triggers, install_triggers
from axile.server.alembic.business0037_schema import ADDED_COLUMNS, CREATE_STATEMENTS, NEW_TABLE_NAMES, TABLE_NAMES

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None

_REMOVED_COLUMNS = {
    "executerecord": ("raw_input", "raw_result"),
    "account_asset_snapshot": ("assets",),
    "target_weight_snapshot": ("raw_weights", "normalized_weights"),
}
_OBSOLETE_TRIGGERS = {"analysis_facts_record_update", "analysis_structured_record_update"}


def _pause_triggers(connection) -> list[tuple[str, str]]:
    """Do not invalidate unchanged frozen projections while changing their storage."""
    triggers = connection.exec_driver_sql(
        "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
        "AND tbl_name IN ('executerecord','account_asset_snapshot','target_weight_snapshot')"
    ).all()
    for name, _sql in triggers:
        connection.exec_driver_sql('DROP TRIGGER "' + name.replace('"', '""') + '"')
    return triggers


def _restore_triggers(connection, triggers: list[tuple[str, str]], *, typed: bool) -> None:
    for name, sql in triggers:
        if name.startswith("business_facts_0037_") or typed and name in _OBSOLETE_TRIGGERS:
            continue
        connection.exec_driver_sql(sql)


def _registry(connection) -> tuple[sa.Table, ...]:
    metadata = sa.MetaData()
    return tuple(sa.Table(name, metadata, autoload_with=connection) for name in TABLE_NAMES)


def install_revision_triggers(connection) -> None:
    """Expose the frozen registry for schema-created tests and explicit initialization."""
    install_triggers(connection, _registry(connection), optimize_summary=False)


def upgrade() -> None:
    """Convert under one savepoint; errors leave all legacy fields and data usable."""
    connection = op.get_bind()
    with connection.begin_nested():
        paused = _pause_triggers(connection)
        for statement in CREATE_STATEMENTS:
            connection.exec_driver_sql(statement)
        for table, columns in ADDED_COLUMNS.items():
            for _name, ddl in columns:
                connection.exec_driver_sql(f'ALTER TABLE "{table}" ADD COLUMN {ddl}')
        conversion.backfill_records(connection)
        conversion.backfill_artifacts(connection)
        conversion.backfill_decision_events(connection)
        conversion.backfill_asset_snapshots(connection)
        conversion.backfill_target_snapshots(connection)
        conversion.compact_summary_artifacts(connection)
        conversion.verify_conversion(connection)
        for table, columns in _REMOVED_COLUMNS.items():
            for name in columns:
                connection.exec_driver_sql(f'ALTER TABLE "{table}" DROP COLUMN "{name}"')
        _restore_triggers(connection, paused, typed=True)
        install_revision_triggers(connection)


def downgrade() -> None:
    """Restore representable evidence; reject pending graphs before any mutation."""
    connection = op.get_bind()
    conversion.preflight_downgrade(connection)
    with connection.begin_nested():
        drop_triggers(connection)
        paused = _pause_triggers(connection)
        connection.exec_driver_sql("ALTER TABLE executerecord ADD COLUMN raw_input JSON NOT NULL DEFAULT '{}'")
        connection.exec_driver_sql("ALTER TABLE executerecord ADD COLUMN raw_result JSON NOT NULL DEFAULT '{}'")
        connection.exec_driver_sql("ALTER TABLE account_asset_snapshot ADD COLUMN assets JSON NOT NULL DEFAULT '{}'")
        connection.exec_driver_sql("ALTER TABLE target_weight_snapshot ADD COLUMN raw_weights JSON")
        connection.exec_driver_sql("ALTER TABLE target_weight_snapshot ADD COLUMN normalized_weights JSON")
        conversion.restore_records(connection)
        conversion.restore_artifacts(connection)
        conversion.restore_decision_events(connection)
        conversion.restore_asset_snapshots(connection)
        conversion.restore_target_snapshots(connection)
        for table, columns in ADDED_COLUMNS.items():
            for name, _ddl in reversed(columns):
                connection.exec_driver_sql(f'ALTER TABLE "{table}" DROP COLUMN "{name}"')
        for name in reversed(NEW_TABLE_NAMES):
            connection.exec_driver_sql(f'DROP TABLE "{name}"')
        _restore_triggers(connection, paused, typed=False)
        conversion.restore_legacy_triggers(connection)
