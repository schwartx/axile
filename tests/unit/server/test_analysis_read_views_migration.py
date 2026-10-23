"""Frozen execution read views retain historical cost and currency semantics."""

import pytest
import sqlalchemy as sa

from tests.fixtures.rust_store.oracle_tables import table as oracle_table
from tests.unit.server.test_analysis_structured_migration import (
    _connect,
    _downgrade,
    _insert_snapshot,
    _ranges,
    _upgrade,
)
from tests.unit.server.test_analysis_structured_migration import database as structured_database  # noqa: F401


@pytest.fixture
def database(request):
    """使用真实 0035 结构，保留无法解析的历史 JSON 哨兵。"""
    database = request.getfixturevalue("structured_database")
    _upgrade(database, "0035")
    with _connect(database) as connection:
        connection.exec_driver_sql("INSERT INTO account_analysis(account_id,running_version) VALUES(1,NULL)")
    return database


def _facts(connection, snapshot_id="typed", *, result_version=2, cost_version=1):
    connection.execute(
        sa.text(
            "INSERT INTO performance_snapshot(id,result_schema_version,cost_schema_version,account_id,source_version,logic_version,engine_version,computed_at,ranges,settings) "
            "VALUES(:snapshot,:result,:cost,2,1,'17','engine','2026-01-01','not-json','not-json')"
        ),
        {"snapshot": snapshot_id, "result": result_version, "cost": cost_version},
    )
    connection.execute(
        sa.text(
            "INSERT INTO performance_cost_execution(snapshot_id,record_id,time,symbol_count,success,noop,payload) "
            "VALUES(:snapshot,:record,:time,NULL,1,0,'not-json')"
        ),
        [{"snapshot": snapshot_id, "record": record, "time": record * 1000.25} for record in (1, 2, 3, 4)],
    )
    connection.execute(
        sa.text("UPDATE performance_cost_execution SET symbol_count=0 WHERE snapshot_id=:snapshot AND record_id!=1"),
        {"snapshot": snapshot_id},
    )
    connection.execute(
        sa.text(
            "INSERT INTO performance_cost_transaction(snapshot_id,record_id,idx,time_estimated,amount_complete) VALUES(:snapshot,1,0,0,0)"
        ),
        {"snapshot": snapshot_id},
    )
    rows = (
        (1, "A", 100, 2, 200, 0, "USDT", 1),
        (1, "B", None, 7, None, 0.25, "BTC", None),
        (1, "A", 50, None, None, 9, "", 2),
        (3, "ZERO", 0, 0, 0, 0, "USDT", 0),
        (4, "A", 100, 1, 100, 0.01, "USDT", 1),
        (4, "B", 200, 2, 100, 0.02, "USDT", 2),
    )
    columns = ("record", "symbol", "value", "cost", "loss", "fee", "currency", "quantity")
    connection.execute(
        sa.text(
            "INSERT INTO performance_cost_trade(snapshot_id,id,record_id,symbol,time,value,cost,"
            "loss_bp,fee,fee_currency,quantity,day,estimated,payload) "
            "VALUES(:snapshot,:id,:record,:symbol,:time,:value,:cost,:loss,:fee,:currency,:quantity,'2026-01-01',0,'not-json')"
        ),
        [
            {"snapshot": snapshot_id, "id": index, "time": index * 1000.125, **dict(zip(columns, row, strict=True))}
            for index, row in enumerate(rows, 1)
        ],
    )


def test_backfill_preserves_unknown_amounts_zero_fees_and_currency(database):
    """One frozen row per execution must not fabricate amounts or combine currencies."""
    with _connect(database) as connection:
        _facts(connection)
    _upgrade(database, "0036")
    with _connect(database) as connection:
        rows = {
            row["record_id"]: row
            for row in connection.execute(
                sa.text("SELECT * FROM performance_execution_summary WHERE snapshot_id='typed'")
            ).mappings()
        }
        assert len(rows) == 4
        partial = rows[1]
        assert partial["value"] == 150
        assert partial["cost"] == 2  # Cost=7 has no amount evidence and stays out of the historical sum.
        assert partial["loss_bp"] == 200
        assert partial["coverage"] is None
        assert partial["covered"] == 1
        assert partial["count"] == 3
        assert not partial["amount_complete"]
        assert partial["fee_covered"] == 2
        assert partial["covered_value"] == 100
        assert partial["symbol_count"] is None
        assert partial["has_transactions"]
        assert partial["time"] == 1000.25
        empty = rows[2]
        assert all(empty[field] is None for field in ("value", "cost", "loss_bp", "coverage"))
        assert empty["count"] == empty["covered"] == empty["fee_covered"] == 0
        assert empty["amount_complete"]
        assert empty["covered_value"] == empty["estimated"] == empty["symbol_count"] == 0
        assert not empty["has_transactions"]
        zero = rows[3]
        assert zero["value"] == zero["cost"] == 0
        assert zero["loss_bp"] is zero["coverage"] is None
        assert zero["amount_complete"] and zero["fee_covered"] == 1
        assert zero["symbol_count"] == 1
        assert not zero["has_transactions"]  # Frozen transactions, rather than fill count, supply this flag.
        assert rows[4]["value"] == 300 and rows[4]["cost"] == 3
        assert rows[4]["coverage"] == 1 and rows[4]["loss_bp"] == 100
        assert rows[4]["symbol_count"] == 2
        fees = connection.execute(
            sa.text(
                "SELECT record_id,currency,value FROM performance_execution_fee "
                "WHERE snapshot_id='typed' ORDER BY record_id,currency"
            )
        ).all()
        assert fees == [(1, "BTC", 0.25), (1, "USDT", 0.0), (3, "USDT", 0.0), (4, "USDT", 0.03)]
        assert (
            connection.execute(sa.text("SELECT read_view_schema_version FROM performance_snapshot")).scalar_one() == 1
        )
        assert connection.execute(sa.text("SELECT running_token FROM account_analysis")).scalar_one() is None


def test_upgrade_leaves_unsupported_legacy_snapshots_untouched(database):
    """Legacy JSON remains readable by its explicit compatibility path."""
    with _connect(database) as connection:
        _facts(connection, "typed")
        _facts(connection, "legacy", result_version=0, cost_version=0)
    _upgrade(database, "0036")
    with _connect(database) as connection:
        for name in ("performance_execution_summary", "performance_execution_fee"):
            assert (
                connection.execute(sa.text(f"SELECT COUNT(*) FROM {name} WHERE snapshot_id='legacy'")).scalar_one() == 0
            )
        assert connection.execute(
            sa.text(
                "SELECT ranges,settings,result_schema_version,cost_schema_version FROM performance_snapshot WHERE id='legacy'"
            )
        ).one() == ("not-json", "not-json", 0, 0)
        assert (
            connection.execute(
                sa.text("SELECT read_view_schema_version FROM performance_snapshot WHERE id='legacy'")
            ).scalar_one()
            == 0
        )


def test_snapshot_retention_cleans_views_with_foreign_keys_disabled(database):
    """Published snapshot retention owns the new tables even on FK-off connections."""
    with _connect(database) as connection:
        _facts(connection)
    _upgrade(database, "0036")
    with _connect(database) as connection:
        connection.execute(sa.text("DELETE FROM performance_snapshot WHERE id='typed'"))
        for name in ("performance_execution_summary", "performance_execution_fee"):
            assert connection.execute(sa.text(f"SELECT COUNT(*) FROM {name}")).scalar_one() == 0


def test_downgrade_retains_the_structured_snapshot_and_facts(database):
    """Revision 0035 can still serve existing data after the new read views are removed."""
    with _connect(database) as connection:
        _facts(connection)
        before = connection.execute(sa.text("SELECT * FROM performance_snapshot")).all()
    _upgrade(database, "0036")
    _downgrade(database, "0035")
    with _connect(database) as connection:
        assert connection.execute(sa.text("SELECT * FROM performance_snapshot")).all() == before
        assert connection.execute(sa.text("SELECT COUNT(*) FROM performance_cost_execution")).scalar_one() == 4
        assert connection.execute(sa.text("SELECT COUNT(*) FROM performance_cost_trade")).scalar_one() == 6
        assert "performance_execution_summary" not in sa.inspect(connection).get_table_names()
        assert "performance_execution_fee" not in sa.inspect(connection).get_table_names()
        assert "running_token" not in {
            column["name"] for column in sa.inspect(connection).get_columns("account_analysis")
        }


def test_empty_typed_snapshot_is_marked_complete(database):
    """An empty execution index is complete and must not look like a missing migration."""
    with _connect(database) as connection:
        connection.execute(
            sa.text(
                "INSERT INTO performance_snapshot(id,result_schema_version,cost_schema_version,account_id,source_version,logic_version,engine_version,computed_at,ranges,settings) VALUES('empty',2,1,2,1,'17','engine','2026-01-01','not-json','not-json')"
            )
        )
    _upgrade(database, "0036")
    with _connect(database) as connection:
        assert connection.execute(sa.text("SELECT COUNT(*) FROM performance_execution_summary")).scalar_one() == 0
        assert (
            connection.execute(sa.text("SELECT read_view_schema_version FROM performance_snapshot")).scalar_one() == 1
        )


def test_frozen_migration_matches_runtime_model_keys_columns_and_index(database):
    """Frozen declarations and native upgrades must expose the same ordered read contract."""
    READ_VIEW_TABLES = (oracle_table("performance_execution_fee"),)
    _upgrade(database, "0036")
    with _connect(database) as connection:
        inspector = sa.inspect(connection)
        for table in READ_VIEW_TABLES:
            columns = inspector.get_columns(table.name)
            assert [(column["name"], column["nullable"]) for column in columns] == [
                (column.name, column.nullable) for column in table.c
            ]
            assert inspector.get_pk_constraint(table.name)["constrained_columns"] == [
                column.name for column in table.primary_key
            ]
            assert (
                connection.exec_driver_sql(f"PRAGMA foreign_key_list({table.name})").mappings().one()["on_delete"]
                == "CASCADE"
            )
        assert inspector.get_indexes("performance_execution_summary") == [
            {
                "name": "ix_performance_execution_summary_time",
                "column_names": ["snapshot_id", "time", "record_id"],
                "unique": 0,
                "dialect_options": {},
            }
        ]


def test_real_0035_schema_upgrades_and_downgrades_with_read_views(request):
    """历史触发器和结果能完成真实的 0036 往返迁移。"""
    database = request.getfixturevalue("structured_database")
    with _connect(database) as connection:
        _insert_snapshot(connection, "snapshot", _ranges())
    _upgrade(database, "0035")
    _upgrade(database, "0036")
    with _connect(database) as connection:
        assert connection.execute(
            sa.text("SELECT result_schema_version,read_view_schema_version FROM performance_snapshot")
        ).one() == (2, 1)
    _downgrade(database, "0035")
    _downgrade(database, "0034")
    with _connect(database) as connection:
        assert "read_view_schema_version" not in {
            column["name"] for column in sa.inspect(connection).get_columns("performance_snapshot")
        }
        assert (
            connection.execute(
                sa.text(
                    "SELECT json_extract(ranges,'$.all.performance.points[0].account_equity') FROM performance_snapshot"
                )
            ).scalar_one()
            == 102.0
        )


def test_backfill_preserves_rebates_and_estimated_trade_count(database):
    """Negative fees and genuine estimated observations must survive the frozen projection."""
    with _connect(database) as connection:
        _facts(connection)
        connection.execute(sa.text("UPDATE performance_cost_trade SET estimated=1 WHERE id IN (1,2)"))
        connection.execute(sa.text("UPDATE performance_cost_trade SET fee=-0.125 WHERE id=2"))
    _upgrade(database, "0036")
    with _connect(database) as connection:
        assert (
            connection.execute(
                sa.text("SELECT estimated FROM performance_execution_summary WHERE record_id=1")
            ).scalar_one()
            == 2
        )
        assert connection.execute(
            sa.text("SELECT value,count FROM performance_execution_fee WHERE record_id=1 AND currency='BTC'")
        ).one() == (-0.125, 1)
