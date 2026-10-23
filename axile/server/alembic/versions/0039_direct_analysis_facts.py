"""Remove disposable analysis inputs while retaining business version guards."""

import re
from datetime import datetime, timedelta, timezone
from importlib import import_module

import sqlalchemy as sa
from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None

_TABLES = (
    "analysis_source_transaction_fee",
    "analysis_source_transaction",
    "analysis_source_attempt",
    "analysis_source_position",
    "analysis_source_execution_symbol",
    "analysis_source_trade",
    "analysis_source_execution",
    "replay_observation_symbol",
    "replay_observation",
)
_OUTPUT_TABLES = ("performance_cost_range", "performance_execution_summary")
_SUMMARY_COLUMNS = {
    "value": "REAL",
    "cost": "REAL",
    "loss_bp": "REAL",
    "coverage": "REAL",
    "covered": "INTEGER",
    "count": "INTEGER",
    "fee_covered": "INTEGER",
    "amount_complete": "BOOLEAN",
    "covered_value": "REAL",
    "estimated": "INTEGER",
    "has_transactions": "BOOLEAN",
}
_DELETE = re.compile(
    r'\bDELETE\s+FROM\s+"?(?:' + "|".join((*_TABLES, *_OUTPUT_TABLES)) + r')"?\s+[^;]*;',
    re.IGNORECASE,
)


def upgrade() -> None:
    """Keep each existing trigger condition and business statement unchanged."""
    connection = op.get_bind()
    triggers = connection.exec_driver_sql(
        "SELECT name,tbl_name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name"
    ).all()
    replacements = []
    for name, owner, sql in triggers:
        if owner in (*_TABLES, *_OUTPUT_TABLES):
            replacements.append((name, None))
            continue
        if not any(table in sql for table in (*_TABLES, *_OUTPUT_TABLES)):
            continue
        replacement = _DELETE.sub("", sql)
        if any(table in replacement for table in (*_TABLES, *_OUTPUT_TABLES)):
            raise RuntimeError(f"Unsupported analysis input trigger: {name}")
        # An empty former invalidation trigger has no remaining business action.
        if re.search(r"\bBEGIN\s*END\s*$", replacement, re.IGNORECASE):
            replacement = None
        replacements.append((name, replacement))
    with connection.begin_nested():
        _merge_outputs(connection)
        columns = {column["name"] for column in sa.inspect(connection).get_columns("performance_cost_execution_symbol")}
        for column in ("memory_message", "memory_reason_code"):
            if column not in columns:
                connection.exec_driver_sql(f'ALTER TABLE performance_cost_execution_symbol ADD COLUMN "{column}" TEXT')
        for name, replacement in replacements:
            connection.exec_driver_sql('DROP TRIGGER "' + name.replace('"', '""') + '"')
            if replacement is not None:
                connection.exec_driver_sql(replacement)
        for table in (*_TABLES, *_OUTPUT_TABLES):
            connection.exec_driver_sql(f'DROP TABLE IF EXISTS "{table}"')


def downgrade() -> None:
    """Restore disposable input schema; the previous worker performs bounded refill."""
    from axile.server.alembic.business0037_invalidation import install_triggers
    from axile.server.alembic.business0037_schema import TABLE_NAMES

    connection = op.get_bind()
    replay = import_module("axile.server.alembic.versions.0034_analysis_rust_facts")
    source = import_module("axile.server.alembic.versions.0035_analysis_structured_results")
    if connection.exec_driver_sql(
        "SELECT EXISTS(SELECT 1 FROM performance_cost_execution_symbol "
        "WHERE memory_message IS NOT NULL OR memory_reason_code IS NOT NULL)"
    ).scalar_one():
        raise RuntimeError("Cannot downgrade: frozen symbol memory evidence cannot be represented by schema 0038")
    with connection.begin_nested():
        _restore_outputs(connection)
        for column in ("memory_message", "memory_reason_code"):
            connection.exec_driver_sql(f'ALTER TABLE performance_cost_execution_symbol DROP COLUMN "{column}"')
        for migration in (replay, source):
            metadata = migration._schema()
            for table in metadata.sorted_tables:
                if table.name in _TABLES:
                    table.create(connection, checkfirst=True)
            migration.install_triggers(connection)
        for table_name in TABLE_NAMES:
            for action in ("insert", "update", "delete"):
                name = f"business_facts_0037_{table_name}_{action}"
                connection.exec_driver_sql(f'DROP TRIGGER IF EXISTS "{name}"')
        metadata = sa.MetaData()
        tables = tuple(sa.Table(name, metadata, autoload_with=connection) for name in TABLE_NAMES)
        install_triggers(connection, tables)


def _millis(value):
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone(timedelta(hours=8)))
    return parsed.timestamp() * 1000.0


def _merge_outputs(connection):
    names = set(sa.inspect(connection).get_table_names())
    existing = {column["name"] for column in sa.inspect(connection).get_columns("performance_cost_execution")}
    for name, kind in _SUMMARY_COLUMNS.items():
        if name not in existing:
            connection.exec_driver_sql(f'ALTER TABLE performance_cost_execution ADD COLUMN "{name}" {kind}')
    if "performance_execution_summary" in names:
        mismatch = connection.exec_driver_sql("""SELECT EXISTS(
            SELECT 1 FROM performance_execution_summary s LEFT JOIN performance_cost_execution e
            ON e.snapshot_id=s.snapshot_id AND e.record_id=s.record_id
            WHERE e.record_id IS NULL OR e.time IS NOT s.time OR e.symbol_count IS NOT s.symbol_count)""").scalar_one()
        if mismatch:
            raise RuntimeError("Cannot merge inconsistent frozen execution summary")
        columns = ",".join('"' + name + '"' for name in _SUMMARY_COLUMNS)
        connection.exec_driver_sql(f"""UPDATE performance_cost_execution AS e SET ({columns})=(
            SELECT {columns} FROM performance_execution_summary s
            WHERE s.snapshot_id=e.snapshot_id AND s.record_id=e.record_id)""")
        connection.exec_driver_sql("DROP INDEX IF EXISTS ix_performance_execution_summary_time")
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_performance_execution_summary_time ON performance_cost_execution(snapshot_id,time,record_id)"
    )
    if "performance_cost_range" in names:
        rows = connection.exec_driver_sql("""SELECT c.snapshot_id,c.range_key,c.baseline,c.end,r.snapshot_id,r.baseline,r.end
            FROM performance_cost_range c LEFT JOIN performance_result_range r
            ON r.snapshot_id=c.snapshot_id AND r.range_key=c.range_key""")
        for snapshot, key, lower, upper, owner, text_lower, text_upper in rows:
            if owner is None or (lower, upper) != (_millis(text_lower), _millis(text_upper)):
                raise RuntimeError(f"Cannot remove unmatched frozen cost range: {snapshot}/{key}")


def _restore_outputs(connection):
    replay = import_module("axile.server.alembic.versions.0034_analysis_rust_facts")
    views = import_module("axile.server.alembic.versions.0036_analysis_read_views")
    connection.exec_driver_sql("DROP INDEX IF EXISTS ix_performance_execution_summary_time")
    for migration, name in ((replay, "performance_cost_range"), (views, "performance_execution_summary")):
        migration._schema().tables[name].create(connection, checkfirst=True)
    columns = ",".join(["snapshot_id", "record_id", "time", *_SUMMARY_COLUMNS, "symbol_count"])
    connection.exec_driver_sql(
        f"INSERT INTO performance_execution_summary ({columns}) SELECT {columns} FROM performance_cost_execution WHERE amount_complete IS NOT NULL"
    )
    for snapshot, key, lower, upper in connection.exec_driver_sql(
        "SELECT snapshot_id,range_key,baseline,end FROM performance_result_range"
    ):
        connection.exec_driver_sql(
            "INSERT INTO performance_cost_range VALUES(?,?,?,?)", (snapshot, key, _millis(lower), _millis(upper))
        )
    for name in _SUMMARY_COLUMNS:
        connection.exec_driver_sql(f'ALTER TABLE performance_cost_execution DROP COLUMN "{name}"')
    # Reinstall only frozen output cleanup triggers whose bodies changed above.
    connection.exec_driver_sql("DROP TRIGGER IF EXISTS analysis_read_views_snapshot_delete")
    views.install_triggers(connection)
    connection.exec_driver_sql("DROP TRIGGER IF EXISTS analysis_facts_snapshot_delete")
