"""Add scalar analysis facts for bounded Rust reads, retaining legacy payloads."""

from collections.abc import Iterator

import sqlalchemy as sa
from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None

_BATCH_SIZE = 100
_RAW_FIELDS = ("status", "task_status", "error", "outcome", "outcome_reason", "reason_code", "msg", "execution_kind")
_SYMBOL_FIELDS = ("status", "error", "outcome", "outcome_reason", "reason_code")
_COST_CHILDREN = (
    "performance_cost_transaction_fee",
    "performance_cost_transaction",
    "performance_cost_attempt",
    "performance_cost_position",
    "performance_cost_execution_symbol",
    "performance_cost_range",
)
_TRIGGERS = (
    "analysis_facts_snapshot_delete",
    "analysis_facts_record_delete",
    "analysis_facts_record_update",
    "analysis_facts_observation_delete",
    "analysis_facts_account_delete",
    *(f"analysis_facts_target_{action}" for action in ("insert", "update", "delete")),
)


def _execution_columns() -> list[sa.Column]:
    """Freeze revision 0034 additions independently of future ORM metadata."""
    return [
        sa.Column("key", sa.Text),
        sa.Column("execution_id", sa.Text),
        sa.Column("created_at", sa.Text),
        sa.Column("is_success", sa.Integer),
        sa.Column("duration_sec", sa.Float),
        sa.Column("trade_count", sa.Integer),
        sa.Column("symbol_count", sa.Integer),
        sa.Column("position_count", sa.Integer),
        sa.Column("positions_present", sa.Boolean, nullable=False, server_default="0"),
        sa.Column("reason", sa.Text),
        sa.Column("raw_present", sa.Integer, nullable=False, server_default="0"),
        sa.Column("has_symbol_results", sa.Boolean, nullable=False, server_default="0"),
        *(sa.Column("raw_" + name, sa.Text) for name in _RAW_FIELDS),
    ]


def _trade_columns() -> list[sa.Column]:
    return [
        sa.Column("side", sa.Text, nullable=False, server_default="none"),
        sa.Column("quantity", sa.Float),
        sa.Column("price", sa.Float),
        sa.Column("reference", sa.Float),
        sa.Column("reference_source", sa.Text),
    ]


def _schema() -> sa.MetaData:
    """Keep historical DDL stable when runtime fact tables evolve."""
    metadata = sa.MetaData()
    sa.Table("account", metadata, sa.Column("id", sa.Integer, primary_key=True))
    sa.Table("executerecord", metadata, sa.Column("id", sa.Integer, primary_key=True))
    sa.Table(
        "performance_snapshot",
        metadata,
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("cost_schema_version", sa.Integer, nullable=False, server_default="0"),
    )
    sa.Table(
        "performance_cost_execution",
        metadata,
        sa.Column("snapshot_id", sa.Text, primary_key=True),
        sa.Column("record_id", sa.Integer, primary_key=True),
        sa.Column("time", sa.Float),
        sa.Column("success", sa.Boolean),
        sa.Column("noop", sa.Boolean),
        sa.Column("payload", sa.JSON),
        *_execution_columns(),
    )
    sa.Table(
        "performance_cost_trade",
        metadata,
        sa.Column("snapshot_id", sa.Text, primary_key=True),
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("record_id", sa.Integer),
        sa.Column("symbol", sa.Text),
        sa.Column("time", sa.Float),
        sa.Column("day", sa.Text),
        *(sa.Column(name, sa.Float) for name in ("value", "cost", "loss_bp", "fee")),
        sa.Column("fee_currency", sa.Text),
        sa.Column("estimated", sa.Boolean),
        sa.Column("payload", sa.JSON),
        *_trade_columns(),
    )

    def child(name: str, keys: list[sa.Column], values: list[sa.Column]) -> None:
        sa.Table(
            name,
            metadata,
            sa.Column(
                "snapshot_id", sa.Text, sa.ForeignKey("performance_snapshot.id", ondelete="CASCADE"), primary_key=True
            ),
            *keys,
            *values,
        )

    child(
        "performance_cost_range",
        [sa.Column("range_key", sa.Text, primary_key=True)],
        [sa.Column("baseline", sa.Float), sa.Column("end", sa.Float)],
    )
    child(
        "performance_cost_execution_symbol",
        [sa.Column("record_id", sa.Integer, primary_key=True), sa.Column("symbol", sa.Text, primary_key=True)],
        [sa.Column("present", sa.Integer, nullable=False), *(sa.Column(name, sa.Text) for name in _SYMBOL_FIELDS)],
    )
    child(
        "performance_cost_position",
        [sa.Column("record_id", sa.Integer, primary_key=True), sa.Column("idx", sa.Integer, primary_key=True)],
        [sa.Column("symbol", sa.Text), sa.Column("quantity", sa.Float), sa.Column("direction", sa.Text)],
    )
    child(
        "performance_cost_attempt",
        [sa.Column("record_id", sa.Integer, primary_key=True), sa.Column("idx", sa.Integer, primary_key=True)],
        [
            sa.Column("symbol", sa.Text),
            sa.Column("planned", sa.Float),
            sa.Column("filled", sa.Float),
            sa.Column("reason", sa.Text),
        ],
    )
    child(
        "performance_cost_transaction",
        [sa.Column("record_id", sa.Integer, primary_key=True), sa.Column("idx", sa.Integer, primary_key=True)],
        [sa.Column("symbol", sa.Text), sa.Column("side", sa.Text)]
        + [sa.Column(name, sa.Float) for name in ("quantity", "price", "reference")]
        + [sa.Column("reference_source", sa.Text)]
        + [sa.Column(name, sa.Float) for name in ("time", "end_time")]
        + [sa.Column("time_estimated", sa.Boolean, nullable=False)]
        + [sa.Column(name, sa.Float) for name in ("before", "target", "value", "cost", "loss_bp", "coverage")]
        + [sa.Column("covered", sa.Integer), sa.Column("count", sa.Integer)]
        + [sa.Column("amount_complete", sa.Boolean, nullable=False), sa.Column("fee_covered", sa.Integer)],
    )
    child(
        "performance_cost_transaction_fee",
        [
            sa.Column("record_id", sa.Integer, primary_key=True),
            sa.Column("transaction_idx", sa.Integer, primary_key=True),
            sa.Column("currency", sa.Text, primary_key=True),
        ],
        [sa.Column("value", sa.Float, nullable=False)],
    )
    sa.Table(
        "replay_observation",
        metadata,
        sa.Column("record_id", sa.Integer, sa.ForeignKey("executerecord.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("account_id", sa.Integer, sa.ForeignKey("account.id", ondelete="CASCADE"), nullable=False),
        sa.Column("execution_id", sa.Text),
        sa.Column("time", sa.Text, nullable=False),
        sa.Column("asset", sa.Float),
        *(
            sa.Column(name, sa.Boolean, nullable=False)
            for name in (
                "sizing_fallback",
                "target_present",
                "target_weight_present",
                "target_units_present",
                "target_quantities_present",
            )
        ),
        sa.Index("ix_replay_observation_account_time", "account_id", "time", "record_id"),
    )
    sa.Table(
        "replay_observation_symbol",
        metadata,
        sa.Column(
            "record_id", sa.Integer, sa.ForeignKey("replay_observation.record_id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("symbol", sa.Text, primary_key=True),
        *(
            sa.Column(name, sa.Float)
            for name in ("target", "price", "target_weight", "target_units", "target_quantity")
        ),
    )
    return metadata


def install_triggers(connection) -> None:
    """Clean derived rows and invalidate replay facts even with foreign_keys off."""
    deletes = " ".join(
        f"DELETE FROM {name} WHERE snapshot_id = OLD.id;"
        for name in (*_COST_CHILDREN, "performance_cost_trade", "performance_cost_execution")
    )
    connection.exec_driver_sql(
        f"CREATE TRIGGER IF NOT EXISTS analysis_facts_snapshot_delete AFTER DELETE ON performance_snapshot BEGIN {deletes} END"
    )
    clear_record = """DELETE FROM replay_observation_symbol WHERE record_id = OLD.id;
        DELETE FROM replay_observation WHERE record_id = OLD.id;"""
    connection.exec_driver_sql(
        f"CREATE TRIGGER IF NOT EXISTS analysis_facts_record_delete AFTER DELETE ON executerecord BEGIN {clear_record} END"
    )
    connection.exec_driver_sql(f"""CREATE TRIGGER IF NOT EXISTS analysis_facts_record_update
        AFTER UPDATE OF raw_input, raw_result, created_at, account_id, execution_id ON executerecord
        BEGIN {clear_record} END""")
    connection.exec_driver_sql("""CREATE TRIGGER IF NOT EXISTS analysis_facts_observation_delete
        AFTER DELETE ON replay_observation BEGIN
        DELETE FROM replay_observation_symbol WHERE record_id = OLD.record_id;
        END""")
    connection.exec_driver_sql("""CREATE TRIGGER IF NOT EXISTS analysis_facts_account_delete AFTER DELETE ON account BEGIN
        DELETE FROM replay_observation_symbol WHERE record_id IN
            (SELECT record_id FROM replay_observation WHERE account_id = OLD.id);
        DELETE FROM replay_observation WHERE account_id = OLD.id;
        END""")
    for action in ("INSERT", "UPDATE", "DELETE"):
        refs = ("OLD", "NEW") if action == "UPDATE" else ("OLD",) if action == "DELETE" else ("NEW",)
        statements = " ".join(
            f"""DELETE FROM replay_observation_symbol WHERE record_id IN
                (SELECT record_id FROM replay_observation
                WHERE execution_id = {ref}.execution_id AND account_id = {ref}.account_id);
                DELETE FROM replay_observation
                WHERE execution_id = {ref}.execution_id AND account_id = {ref}.account_id;"""
            for ref in refs
        )
        connection.exec_driver_sql(
            f"CREATE TRIGGER IF NOT EXISTS analysis_facts_target_{action.lower()} AFTER {action} ON target_weight_snapshot "
            f"BEGIN {statements} END"
        )


def _batches(connection, table: sa.Table, snapshot_id: str, key: str) -> Iterator[list[dict]]:
    """Read at most 100 legacy projections without retaining full history."""
    previous = None
    # Select only pre-migration columns; newly added headers are not source facts.
    additions = {column.name for column in (_execution_columns() if key == "record_id" else _trade_columns())}
    columns = [column for column in table.c if column.name not in additions]
    while True:
        query = sa.select(*columns).where(table.c.snapshot_id == snapshot_id).order_by(table.c[key]).limit(_BATCH_SIZE)
        if previous is not None:
            query = query.where(table.c[key] > previous)
        rows = [dict(row) for row in connection.execute(query).mappings()]
        if not rows:
            return
        previous = rows[-1][key]
        yield rows


def _validate_scalars(table: sa.Table, values: dict) -> None:
    """Reject coercions that would change historical API values or abort migration."""
    for column in table.c:
        value = values.get(column.name)
        if value is None:
            if not column.nullable:
                raise ValueError(f"Missing legacy scalar in {table.name}.{column.name}")
            continue
        expected = None
        if isinstance(column.type, sa.Text):
            expected = str
        elif isinstance(column.type, sa.Float):
            expected = (int, float)
            if isinstance(value, bool):
                raise ValueError(f"Boolean legacy number in {table.name}.{column.name}")
        elif isinstance(column.type, sa.Boolean):
            expected = (int, bool)
            if value not in (False, True):
                raise ValueError(f"Invalid legacy boolean in {table.name}.{column.name}")
        elif isinstance(column.type, sa.Integer):
            expected = (int, bool)
        if expected is not None and not isinstance(value, expected):
            raise ValueError(f"Invalid legacy scalar in {table.name}.{column.name}")


def _backfill_snapshot(connection, metadata: sa.MetaData, snapshot_id: str) -> None:
    """Backfill immutable projections only; original executions remain untouched."""
    from axile.server.analysis_cost_facts import execution_fact, iter_execution_child_batches, range_facts, trade_fact

    execution = metadata.tables["performance_cost_execution"]
    trade = metadata.tables["performance_cost_trade"]
    for rows in _batches(connection, execution, snapshot_id, "record_id"):
        for row in rows:
            if not isinstance(row["payload"], dict):
                raise ValueError("Legacy execution projection must be an object")
            fact = execution_fact(row, snapshot_id)
            _validate_scalars(execution, fact)
            connection.execute(
                execution.update()
                .where(execution.c.snapshot_id == snapshot_id, execution.c.record_id == row["record_id"])
                .values({column.name: fact[column.name] for column in _execution_columns()})
            )
        for table, batch in iter_execution_child_batches(snapshot_id, rows, _BATCH_SIZE):
            frozen_table = metadata.tables[table.name]
            for value in batch:
                _validate_scalars(frozen_table, value)
            connection.execute(frozen_table.insert(), batch)
    for rows in _batches(connection, trade, snapshot_id, "id"):
        for row in rows:
            if not isinstance(row["payload"], dict):
                raise ValueError("Legacy trade projection must be an object")
            fact = trade_fact(row, snapshot_id)
            _validate_scalars(trade, fact)
            connection.execute(
                trade.update()
                .where(trade.c.snapshot_id == snapshot_id, trade.c.id == row["id"])
                .values({column.name: fact[column.name] for column in _trade_columns()})
            )
    # SQLite extracts only the two scalar bounds, never sending the large snapshot
    # JSON to Python. Iteration also avoids retaining even the range list.
    ranges = connection.execute(
        sa.text("""SELECT item.key AS range_key,
        json_extract(item.value, '$.performance.baseline') AS baseline,
        json_extract(item.value, '$.performance.end') AS range_end
        FROM performance_snapshot AS snapshot, json_each(snapshot.ranges) AS item
        WHERE snapshot.id = :snapshot_id"""),
        {"snapshot_id": snapshot_id},
    )
    for row in ranges.mappings():
        value = {row["range_key"]: {"performance": {"baseline": row["baseline"], "end": row["range_end"]}}}
        connection.execute(metadata.tables["performance_cost_range"].insert(), list(range_facts(snapshot_id, value)))
    snapshot = metadata.tables["performance_snapshot"]
    connection.execute(snapshot.update().where(snapshot.c.id == snapshot_id).values(cost_schema_version=1))


def _backfill(connection, metadata: sa.MetaData) -> None:
    """Keep unsupported historical values readable through the legacy path."""
    previous = None
    while True:
        query = sa.select(metadata.tables["performance_snapshot"].c.id).order_by("id").limit(_BATCH_SIZE)
        if previous is not None:
            query = query.where(metadata.tables["performance_snapshot"].c.id > previous)
        ids = list(connection.execute(query).scalars())
        if not ids:
            return
        previous = ids[-1]
        for snapshot_id in ids:
            try:
                with connection.begin_nested():
                    _backfill_snapshot(connection, metadata, snapshot_id)
            except (ValueError, TypeError, KeyError):
                # A partially typed snapshot must never be advertised to Rust.
                # The savepoint restores all headers and children for this ID.
                continue


def upgrade() -> None:
    """Add typed facts; preserve legacy projections for a reversible migration."""
    metadata = _schema()
    op.add_column(
        "performance_snapshot", sa.Column("cost_schema_version", sa.Integer, nullable=False, server_default="0")
    )
    for name, columns in (
        ("performance_cost_execution", _execution_columns()),
        ("performance_cost_trade", _trade_columns()),
    ):
        for column in columns:
            op.add_column(name, column)
    connection = op.get_bind()
    for name in (*reversed(_COST_CHILDREN), "replay_observation", "replay_observation_symbol"):
        metadata.tables[name].create(connection)
    _backfill(connection, metadata)
    install_triggers(connection)


def downgrade() -> None:
    """Remove derived facts without losing the original projection JSON."""
    for name in _TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
    for name in ("replay_observation_symbol", "replay_observation", *_COST_CHILDREN):
        op.drop_table(name)
    # SQLite's native DROP COLUMN preserves pre-existing triggers and payloads.
    for name, columns in (
        ("performance_cost_trade", _trade_columns()),
        ("performance_cost_execution", _execution_columns()),
    ):
        for column in reversed(columns):
            op.drop_column(name, column.name)
    op.drop_column("performance_snapshot", "cost_schema_version")
