"""Freeze small execution read views without changing the 0035 result layout."""

import sqlalchemy as sa
from alembic import op

from axile.server.alembic.analysis0036_read_views import FEE_INSERT, MARK_COMPLETE, SUMMARY_INSERT

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None

_TRIGGER = "analysis_read_views_snapshot_delete"
_TABLE_NAMES = ("performance_execution_summary", "performance_execution_fee")


def _schema() -> sa.MetaData:
    """Freeze the revision's DDL independently of future runtime model changes."""
    metadata = sa.MetaData()
    sa.Table("performance_snapshot", metadata, sa.Column("id", sa.Text, primary_key=True))
    sa.Table(
        "performance_execution_summary",
        metadata,
        sa.Column(
            "snapshot_id", sa.Text, sa.ForeignKey("performance_snapshot.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("record_id", sa.Integer, primary_key=True),
        sa.Column("time", sa.Float, nullable=False),
        *[sa.Column(name, sa.Float) for name in ("value", "cost", "loss_bp", "coverage")],
        *[sa.Column(name, sa.Integer, nullable=False) for name in ("covered", "count", "fee_covered")],
        sa.Column("amount_complete", sa.Boolean, nullable=False),
        sa.Column("covered_value", sa.Float, nullable=False),
        sa.Column("estimated", sa.Integer, nullable=False),
        sa.Column("symbol_count", sa.Integer),
        sa.Column("has_transactions", sa.Boolean, nullable=False),
        sa.Index("ix_performance_execution_summary_time", "snapshot_id", "time", "record_id"),
    )
    sa.Table(
        "performance_execution_fee",
        metadata,
        sa.Column(
            "snapshot_id", sa.Text, sa.ForeignKey("performance_snapshot.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("record_id", sa.Integer, primary_key=True),
        sa.Column("currency", sa.Text, primary_key=True),
        sa.Column("value", sa.Float, nullable=False),
        sa.Column("count", sa.Integer, nullable=False),
    )
    return metadata


def install_triggers(connection) -> None:
    """Keep snapshot retention correct on SQLite connections with foreign keys off."""
    connection.exec_driver_sql(
        f"CREATE TRIGGER IF NOT EXISTS {_TRIGGER} AFTER DELETE ON performance_snapshot BEGIN "
        "DELETE FROM performance_execution_fee WHERE snapshot_id=OLD.id; "
        "DELETE FROM performance_execution_summary WHERE snapshot_id=OLD.id; END"
    )


def upgrade() -> None:
    """Backfill supported frozen facts once; unsupported legacy snapshots stay explicit."""
    op.add_column("account_analysis", sa.Column("running_token", sa.Text))
    op.add_column(
        "performance_snapshot", sa.Column("read_view_schema_version", sa.Integer, nullable=False, server_default="0")
    )
    connection = op.get_bind()
    metadata = _schema()
    for name in _TABLE_NAMES:
        metadata.tables[name].create(connection)
    for sql in (SUMMARY_INSERT, FEE_INSERT, MARK_COMPLETE):
        connection.execute(sa.text(sql), {"snapshot_id": None})
    install_triggers(connection)


def downgrade() -> None:
    """Remove only read-view additions; all revision 0035 evidence remains available."""
    connection = op.get_bind()
    connection.exec_driver_sql(f"DROP TRIGGER IF EXISTS {_TRIGGER}")
    for name in reversed(_TABLE_NAMES):
        op.drop_table(name)
    op.drop_column("performance_snapshot", "read_view_schema_version")
    op.drop_column("account_analysis", "running_token")
