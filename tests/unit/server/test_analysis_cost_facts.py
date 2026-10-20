"""Structured analysis facts preserve API semantics without decoding historical JSON."""

from collections import defaultdict

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from axile.server.analysis_cost_facts import COST_FACT_SCHEMA_VERSION, iter_cost_fact_batches
from axile.server.performance_costs import project_execution, timestamp
from tests.unit.server.test_initial_migration import _MIGRATIONS_DIR, _load_migration
from tests.unit.server.test_performance_analysis import record


def _project_rows(records):
    projected, costs = [], []
    for source in records:
        payload, trades = project_execution(source)
        projected.append(
            {
                "record_id": source.id,
                "time": timestamp(source.created_at),
                "success": source.is_success == 1,
                "noop": payload["noop"],
                "payload": payload,
            }
        )
        for trade in trades:
            costs.append(
                {
                    "id": len(costs),
                    "record_id": source.id,
                    "symbol": trade["symbol"],
                    "time": trade["time"],
                    "day": trade["day"],
                    "value": trade["value"],
                    "cost": trade["cost"],
                    "loss_bp": trade["lossBp"],
                    "fee": trade["fee"],
                    "fee_currency": trade["feeCurrency"],
                    "estimated": trade["timeEstimated"],
                    "payload": trade,
                }
            )
    return projected, costs


def _ranges():
    return {
        "all": {"performance": {"baseline": "2026-01-01T09:00:00", "end": "2026-01-02T09:00:00"}},
        "30": {"performance": {"baseline": None, "end": None}},
        "90": {"performance": {"baseline": "2026-01-01T09:00:00", "end": "2026-01-02T09:00:00"}},
    }


def _rich_projection():
    source = record()
    raw = source.raw_result
    raw.update(status="PARTIAL", error="未全部成交", reason_code=None, msg="")
    raw["account_assets"]["positions"] = [
        {"symbol": "A", "volume": -2, "direction": "SHORT"},
        {"symbol": "FLAT", "volume": 0, "direction": "LONG"},
    ]
    raw["symbol_results"]["A"].update(status="PARTIAL", reason_code=None)
    raw["symbol_results"]["A"]["sizing"].update(current_quantity=0, target_quantity=3)
    raw["symbol_results"]["A"]["orders"][0]["filled_volume"] = 2
    raw["symbol_results"]["B"] = {
        "status": "SUCCEEDED",
        "first_tick": {"last_price": 50},
        "orders": [{"order_id": "sell", "direction": "SELL"}],
        "trades": [{"order_id": "sell", "trade_price": 51, "trade_volume": 1}],
    }
    return _project_rows([source])


def _group_facts(projected, costs, batch_size=500):
    result = defaultdict(list)
    for table, rows in iter_cost_fact_batches("snapshot", projected, costs, _ranges(), batch_size=batch_size):
        result[table.name].extend(rows)
    return result


def test_cost_facts_reconstruct_status_presence_positions_and_attempts():
    """Null, absent and empty evidence remain distinct in scalar and child facts."""
    projected, costs = _rich_projection()
    facts = _group_facts(projected, costs)
    original = projected[0]["payload"]
    execution = facts["performance_cost_execution"][0]
    raw = original["record"]["raw_result"]
    fields = ("status", "task_status", "error", "outcome", "outcome_reason", "reason_code", "msg", "execution_kind")
    rebuilt_raw = {
        name: execution["raw_" + name] for index, name in enumerate(fields) if execution["raw_present"] & (1 << index)
    }
    assert rebuilt_raw == {name: raw[name] for name in fields if name in raw}
    assert rebuilt_raw["reason_code"] is None
    assert rebuilt_raw["msg"] == ""
    assert "execution_kind" not in rebuilt_raw
    assert execution["positions_present"] is True
    assert execution["position_count"] == 1
    assert [
        {key: value for key, value in row.items() if key not in {"snapshot_id", "record_id", "idx"}}
        for row in facts["performance_cost_position"]
    ] == original["positions"]
    assert [
        {key: value for key, value in row.items() if key not in {"snapshot_id", "record_id", "idx"}}
        for row in facts["performance_cost_attempt"]
    ] == original["attempts"]
    assert execution["payload"] == original
    symbols = {row["symbol"]: row for row in facts["performance_cost_execution_symbol"]}
    assert symbols["A"]["reason_code"] is None
    assert symbols["A"]["present"] & (1 << 4)
    assert not symbols["B"]["present"] & (1 << 4)


def test_cost_facts_preserve_unknown_money_and_zero_fees():
    """Unpriced trades stay unknown while an explicit zero fee stays covered."""
    projected, costs = _rich_projection()
    facts = _group_facts(projected, costs)
    trades = {row["symbol"]: row for row in facts["performance_cost_trade"]}
    assert trades["A"]["value"] == 2060
    assert trades["A"]["cost"] == 40
    assert trades["A"]["fee"] == 0
    assert trades["B"]["value"] is None
    assert trades["B"]["cost"] is None
    assert trades["B"]["fee"] is None
    assert trades["B"]["side"] == "sell"
    assert trades["B"]["reference_source"] == "last"
    transactions = {row["symbol"]: row for row in facts["performance_cost_transaction"]}
    assert transactions["A"]["amount_complete"] is True
    assert transactions["A"]["fee_covered"] == 1
    assert transactions["B"]["amount_complete"] is False
    assert transactions["B"]["fee_covered"] == 0
    assert facts["performance_cost_transaction_fee"] == [
        {"snapshot_id": "snapshot", "record_id": 1, "transaction_idx": 0, "currency": "CNY", "value": 0}
    ]


def test_cost_facts_distinguish_unknown_positions_from_explicit_flat():
    """No child positions can mean unavailable or flat; the scalar presence bit disambiguates."""
    unavailable = record(1)
    flat = record(2, account_assets={"total_asset": 102, "positions": []})
    projected, costs = _project_rows([unavailable, flat])
    facts = _group_facts(projected, costs)
    first, second = facts["performance_cost_execution"]
    assert first["positions_present"] is False
    assert first["position_count"] is None
    assert second["positions_present"] is True
    assert second["position_count"] == 0
    assert facts["performance_cost_position"] == []


def test_cost_fact_batches_stream_and_limit_each_insert():
    """Range facts can be emitted before consuming history and every insert stays bounded."""
    projected, costs = _project_rows([record(index) for index in range(1, 8)])
    consumed = []

    def history():
        for row in projected:
            consumed.append(row["record_id"])
            yield row

    batches = iter_cost_fact_batches("snapshot", history(), iter(costs), _ranges(), batch_size=2)
    table, first = next(batches)
    assert table.name == "performance_cost_range"
    assert len(first) == 2
    assert consumed == []
    remaining = list(batches)
    assert all(0 < len(rows) <= 2 for _, rows in remaining)
    assert consumed == list(range(1, 8))
    assert sum(len(rows) for table, rows in remaining if table.name == "performance_cost_execution") == 7
    assert sum(len(rows) for table, rows in remaining if table.name == "performance_cost_trade") == len(costs)
    ranges = first + [row for table, rows in remaining if table.name == "performance_cost_range" for row in rows]
    assert next(row for row in ranges if row["range_key"] == "30")["baseline"] is None
    assert next(row for row in ranges if row["range_key"] == "all")["end"] == timestamp("2026-01-02T09:00:00")
    with pytest.raises(ValueError, match="batch_size"):
        list(iter_cost_fact_batches("snapshot", [], [], {}, batch_size=0))


def _legacy_schema(connection):
    metadata = sa.MetaData()
    sa.Table(
        "account",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("backtest_weight_type", sa.Text),
        sa.Column("backtest_fee_rate", sa.Float),
    )
    for name in ("executerecord", "portfolioaccount", "target_weight_snapshot", "schedule_skip"):
        sa.Table(name, metadata, sa.Column("id", sa.Integer, primary_key=True), sa.Column("account_id", sa.Integer))
    metadata.create_all(connection)
    connection.execute(sa.text("INSERT INTO account (id) VALUES (2)"))
    legacy = _load_migration(_MIGRATIONS_DIR / "0012_performance_snapshots.py")
    legacy.op = Operations(MigrationContext.configure(connection))
    legacy.upgrade()


@pytest.mark.parametrize("extra_count", [0, 105])
def test_cost_facts_migration_backfills_and_downgrades_without_losing_legacy_payloads(tmp_path, extra_count):
    """Upgrade freezes typed facts while downgrade retains original immutable snapshot evidence."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'legacy-analysis.db'}")
    projected, costs = _rich_projection()
    extra_projected, extra_costs = _project_rows([record(index + 2) for index in range(extra_count)])
    projected.extend(extra_projected)
    costs.extend({**row, "id": index + 2} for index, row in enumerate(extra_costs))
    migration = _load_migration(_MIGRATIONS_DIR / "0034_analysis_rust_facts.py")
    try:
        with engine.begin() as connection:
            _legacy_schema(connection)
            legacy = sa.MetaData()
            legacy.reflect(connection)
            connection.execute(
                legacy.tables["performance_snapshot"].insert(),
                {
                    "id": "snapshot",
                    "account_id": 2,
                    "source_version": 3,
                    "logic_version": "16",
                    "engine_version": "equity-ledger-1",
                    "computed_at": "2026-01-03T00:00:00",
                    "settings": {"backtest_weight_type": "cs", "backtest_fee_rate": 0.0002},
                    "ranges": _ranges(),
                },
            )
            connection.execute(
                legacy.tables["performance_cost_execution"].insert(),
                [{"snapshot_id": "snapshot", **row} for row in projected],
            )
            connection.execute(
                legacy.tables["performance_cost_trade"].insert(),
                [{"snapshot_id": "snapshot", **row} for row in costs],
            )
            migration.op = Operations(MigrationContext.configure(connection))
            migration.upgrade()
            assert connection.execute(sa.text("SELECT cost_schema_version FROM performance_snapshot")).scalar_one() == (
                COST_FACT_SCHEMA_VERSION
            )
            row = (
                connection.execute(
                    sa.text(
                        "SELECT positions_present, position_count, raw_reason_code, raw_present "
                        "FROM performance_cost_execution WHERE record_id=1"
                    )
                )
                .mappings()
                .one()
            )
            assert row["positions_present"] == 1
            assert row["position_count"] == 1
            assert row["raw_reason_code"] is None
            assert row["raw_present"] & (1 << 5)
            assert connection.execute(sa.text("SELECT COUNT(*) FROM performance_cost_position")).scalar_one() == 1
            assert connection.execute(sa.text("SELECT COUNT(*) FROM performance_cost_transaction")).scalar_one() == (
                2 + extra_count
            )
            assert (
                connection.execute(
                    sa.text("SELECT value FROM performance_cost_transaction_fee WHERE record_id=1")
                ).scalar_one()
                == 0
            )
            assert (
                connection.execute(sa.text("SELECT side FROM performance_cost_trade WHERE symbol='B'")).scalar_one()
                == "sell"
            )
            migration.downgrade()
            inspector = sa.inspect(connection)
            assert "performance_cost_position" not in inspector.get_table_names()
            assert "cost_schema_version" not in {row["name"] for row in inspector.get_columns("performance_snapshot")}
            assert (
                connection.execute(
                    sa.select(legacy.tables["performance_cost_execution"].c.payload).where(
                        legacy.tables["performance_cost_execution"].c.record_id == 1
                    )
                ).scalar_one()
                == (projected[0]["payload"])
            )
            assert (
                connection.execute(sa.select(legacy.tables["performance_snapshot"].c.ranges)).scalar_one() == _ranges()
            )
            assert connection.execute(sa.text("SELECT COUNT(*) FROM performance_cost_trade")).scalar_one() == (
                2 + extra_count
            )
    finally:
        engine.dispose()


def test_cost_fact_snapshot_cleanup_works_without_sqlite_foreign_keys(tmp_path):
    """Snapshot retention cannot leak child facts when SQLite foreign_keys remains disabled."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'cleanup-analysis.db'}")
    migration = _load_migration(_MIGRATIONS_DIR / "0034_analysis_rust_facts.py")
    try:
        with engine.begin() as connection:
            _legacy_schema(connection)
            migration.op = Operations(MigrationContext.configure(connection))
            migration.upgrade()
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 0
            connection.execute(
                sa.text(
                    "INSERT INTO performance_snapshot "
                    "(id, account_id, source_version, logic_version, engine_version, computed_at, settings, ranges) "
                    "VALUES ('snapshot', 2, 1, '16', 'equity-ledger-1', '2026-01-03', '{}', '{}')"
                )
            )
            projected, costs = _rich_projection()
            tables = set()
            for table, rows in iter_cost_fact_batches("snapshot", projected, costs, _ranges()):
                connection.execute(table.insert(), rows)
                tables.add(table.name)
            connection.execute(sa.text("DELETE FROM performance_snapshot WHERE id='snapshot'"))
            for name in tables:
                assert connection.exec_driver_sql(f'SELECT COUNT(*) FROM "{name}"').scalar_one() == 0, name
    finally:
        engine.dispose()


def test_cost_fact_backfill_rejects_one_snapshot_atomically_without_blocking_others(tmp_path):
    """Unsupported legacy fields keep the fallback readable and cannot publish half a fact set."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'partial-backfill.db'}")
    migration = _load_migration(_MIGRATIONS_DIR / "0034_analysis_rust_facts.py")
    projected, costs = _rich_projection()
    bad_projected = [{**projected[0], "payload": {**projected[0]["payload"], "key": 123}}]
    try:
        with engine.begin() as connection:
            _legacy_schema(connection)
            legacy = sa.MetaData()
            legacy.reflect(connection)
            for snapshot_id, execution_rows in (("bad", bad_projected), ("good", projected)):
                connection.execute(
                    legacy.tables["performance_snapshot"].insert(),
                    {
                        "id": snapshot_id,
                        "account_id": 2,
                        "source_version": 3,
                        "logic_version": "16",
                        "engine_version": "equity-ledger-1",
                        "computed_at": "2026-01-03T00:00:00",
                        "settings": {},
                        "ranges": _ranges(),
                    },
                )
                connection.execute(
                    legacy.tables["performance_cost_execution"].insert(),
                    [{"snapshot_id": snapshot_id, **row} for row in execution_rows],
                )
                connection.execute(
                    legacy.tables["performance_cost_trade"].insert(),
                    [{"snapshot_id": snapshot_id, **row} for row in costs],
                )
            migration.op = Operations(MigrationContext.configure(connection))
            migration.upgrade()
            assert dict(
                connection.execute(sa.text("SELECT id, cost_schema_version FROM performance_snapshot")).all()
            ) == {
                "bad": 0,
                "good": COST_FACT_SCHEMA_VERSION,
            }
            assert (
                connection.execute(
                    sa.text("SELECT COUNT(*) FROM performance_cost_range WHERE snapshot_id='bad'")
                ).scalar_one()
                == 0
            )
            assert (
                connection.execute(
                    sa.text("SELECT COUNT(*) FROM performance_cost_transaction WHERE snapshot_id='bad'")
                ).scalar_one()
                == 0
            )
            assert (
                connection.execute(
                    sa.text("SELECT COUNT(*) FROM performance_cost_transaction WHERE snapshot_id='good'")
                ).scalar_one()
                == 2
            )
            assert (
                connection.execute(
                    sa.select(legacy.tables["performance_cost_execution"].c.payload).where(
                        legacy.tables["performance_cost_execution"].c.snapshot_id == "bad"
                    )
                ).scalar_one()
                == bad_projected[0]["payload"]
            )
    finally:
        engine.dispose()
