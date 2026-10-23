"""The frozen downgrade restores compact compatibility JSON from typed facts."""

import pytest
import sqlalchemy as sa

from tests.conftest import initialize_native_database, native_store_call
from tests.fixtures.rust_store.oracle_tables import analysis_snapshot, cost_execution, cost_trade
from tests.unit.server._analysis_cost_facts_oracle import iter_cost_fact_batches
from tests.unit.server._business_analysis_support import record
from tests.unit.server._performance_costs_oracle import summarize
from tests.unit.server.test_analysis_cost_facts import _project_rows, _ranges, _rich_projection
from tests.unit.server.test_analysis_structured_migration import _connect


@pytest.mark.parametrize("cost_schema", [1, 2])
def test_frozen_downgrade_restores_full_execution_evidence_and_trade_json(tmp_path, cost_schema):
    """Empty payloads and retained schema-one payloads both regain full summaries."""
    projected, trades = _rich_projection()
    no_trades = record(2)
    no_trades.result.update(status="NOOP", symbol_results={})
    extra, _ = _project_rows([no_trades])
    projected.extend(extra)
    expected = {
        row["record_id"]: row["payload"]
        | {"summary": summarize([trade["payload"] for trade in trades if trade["record_id"] == row["record_id"]])}
        for row in projected
    }
    database = tmp_path / "cost-restore.db"
    initialize_native_database(database, revision="0035")
    with _connect(database) as connection:
        connection.exec_driver_sql(
            "INSERT INTO account(id,name,market,trade_channel,brokerage,created_at,updated_at) "
            "VALUES(2,'synthetic','FUTURES','tq','','2026-01-01','2026-01-01')"
        )
        connection.execute(
            analysis_snapshot.insert().values(
                id="snapshot",
                account_id=2,
                source_version=1,
                logic_version="test",
                engine_version="test",
                computed_at="2026-01-03T00:00:00",
                settings={},
                ranges={},
                cost_schema_version=cost_schema,
                result_schema_version=2,
            )
        )
        for table, batch in iter_cost_fact_batches("snapshot", projected, trades, _ranges(), batch_size=1):
            connection.execute(table.insert(), batch)
        if cost_schema == 2:
            connection.execute(cost_execution.update().values(payload={}))
            connection.execute(cost_trade.update().values(payload={}))
    native_store_call(database, "migration_downgrade", {"public": "0034"})
    with _connect(database) as connection:
        actual = {
            row.record_id: row.payload
            for row in connection.execute(sa.select(cost_execution.c.record_id, cost_execution.c.payload))
        }
        assert actual == expected
        assert list(connection.execute(sa.select(cost_trade.c.payload).order_by(cost_trade.c.id)).scalars()) == [
            row["payload"] for row in trades
        ]
        assert connection.scalar(sa.select(analysis_snapshot.c.cost_schema_version)) == 1
        assert actual[2]["summary"] == {
            "value": None,
            "cost": None,
            "lossBp": None,
            "coverage": None,
            "covered": 0,
            "count": 0,
            "amountComplete": True,
            "fees": {},
            "feeCovered": 0,
        }
        assert actual[1]["record"]["raw_result"]["reason_code"] is None
        assert actual[1]["record"]["raw_result"]["msg"] == ""
        assert "execution_kind" not in actual[1]["record"]["raw_result"]
        assert actual[1]["transactions"][0]["summary"]["fees"] == {"CNY": 0.0}
    native_store_call(database, "migration_downgrade", {"public": "0034"})
    with _connect(database) as connection:
        assert {
            row.record_id: row.payload
            for row in connection.execute(sa.select(cost_execution.c.record_id, cost_execution.c.payload))
        } == actual
