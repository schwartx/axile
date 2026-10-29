"""Revision 0034 trigger invariants and lossless historical Rust migration."""

import json
from contextlib import contextmanager
from copy import deepcopy

import pytest
import sqlalchemy as sa

from scripts.business_facts_contract import business_evidence
from tests.conftest import initialize_native_database, native_store_call, require_native_store
from tests.fixtures.rust_store.oracle_tables import table
from tests.sqlite_oracle import register_time_function
from tests.unit.server._native_read_views import read_view


@contextmanager
def _connect(database):
    """Close test-side SQLite handles before every Rust business operation."""
    engine = sa.create_engine(f"sqlite:///{database}", poolclass=sa.pool.NullPool)
    sa.event.listen(engine, "connect", register_time_function)
    try:
        with engine.begin() as connection:
            yield connection
    finally:
        engine.dispose()


def _upgrade(database):
    return initialize_native_database(database, revision="0034")


def _downgrade(database):
    return native_store_call(database, "migration_downgrade", {"public": "0033"})


def _load_input(database, input_id):
    return read_view(database, "read_execution_input", {"input_id": input_id})


@pytest.fixture
def fact_connection(tmp_path):
    database = tmp_path / "fact-triggers.db"
    _upgrade(database)
    with _connect(database) as connection:
        for account_id in (1, 2):
            connection.exec_driver_sql(
                "INSERT INTO account(id,name,market,trade_channel,brokerage,created_at,updated_at) "
                "VALUES(?,'synthetic','FUTURES','ctp','','2026-01-02','2026-01-02')",
                (account_id,),
            )
    for account_id, input_id in ((1, "one"), (2, "two")):
        native_store_call(
            database,
            "freeze_execution_input",
            {
                "account_id": account_id,
                "execution_id": input_id,
                "input_id": input_id,
                "created_at": "2026-01-02",
                "payload": {},
            },
        )
    with _connect(database) as connection:
        for account_id, input_id in ((1, "one"), (2, "two")):
            connection.exec_driver_sql(
                "INSERT INTO executerecord(id,account_id,execution_id,input_id,is_success,created_at,facts_complete) "
                "VALUES(?,?,?,?,0,'2026-01-02',1)",
                (account_id, account_id, input_id, input_id),
            )
        connection.exec_driver_sql(
            "INSERT INTO account_analysis(account_id) VALUES(1),(2) "
            "ON CONFLICT(account_id) DO UPDATE SET source_version=1"
        )
        yield connection


def _insert_trade(connection, *, record_id=1, price=1):
    connection.execute(
        table("business_trade").insert(),
        {
            "record_id": record_id,
            "path": "/trades/0",
            "collection_path": "/trades",
            "ordinal": 0,
            "state": "object",
            "trade_price": price,
            "trade_id_state": "absent",
            "order_id_state": "absent",
            "conversion_recorded": False,
            "fee_state": "absent",
            "audit_evidence": {},
        },
    )


def test_child_write_versions_only_its_account(fact_connection):
    connection = fact_connection
    _insert_trade(connection, price=0)
    assert business_evidence(
        connection.exec_driver_sql("SELECT account_id,source_version FROM account_analysis ORDER BY account_id").all()
    ) == business_evidence(
        [
            (1, 2),
            (2, 1),
        ]
    )


def test_input_write_does_not_version_unrelated_accounts(fact_connection):
    connection = fact_connection
    connection.exec_driver_sql(
        "INSERT INTO execution_input_target(input_id,role,map_key,value,audit_evidence) "
        "VALUES('one','curr_target','A',0,'{}')"
    )
    assert business_evidence(
        connection.exec_driver_sql("SELECT source_version FROM account_analysis WHERE account_id=2").scalar_one()
    ) == business_evidence(1)


def test_reparent_update_invalidates_old_and_new_owners(fact_connection):
    connection = fact_connection
    _insert_trade(connection)
    connection.exec_driver_sql("UPDATE business_trade SET record_id=2 WHERE record_id=1 AND path='/trades/0'")
    assert business_evidence(
        connection.exec_driver_sql("SELECT account_id,source_version FROM account_analysis ORDER BY account_id").all()
    ) == business_evidence(
        [
            (1, 3),
            (2, 2),
        ]
    )


def test_cross_account_and_missing_owner_writes_fail_with_foreign_keys_off(fact_connection):
    connection = fact_connection
    assert business_evidence(connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one()) == business_evidence(0)
    with pytest.raises(sa.exc.IntegrityError, match="input owner mismatch"):
        connection.exec_driver_sql("UPDATE executerecord SET result_input_id='two' WHERE id=1")
    with pytest.raises(sa.exc.IntegrityError, match="fact owner missing"):
        _insert_trade(connection, record_id=99)
    with pytest.raises(sa.exc.IntegrityError, match="still referenced"):
        connection.exec_driver_sql("DELETE FROM execution_input WHERE id='one'")


def test_record_delete_cleans_children_even_with_foreign_keys_off(fact_connection):
    connection = fact_connection
    _insert_trade(connection)
    connection.exec_driver_sql("DELETE FROM executerecord WHERE id=1")
    assert business_evidence(
        connection.exec_driver_sql("SELECT COUNT(*) FROM business_trade").scalar_one()
    ) == business_evidence(0)


def test_analysis_publication_does_not_invalidate_business_source(fact_connection):
    connection = fact_connection
    before = connection.exec_driver_sql("SELECT source_version FROM account_analysis WHERE account_id=1").scalar_one()
    connection.exec_driver_sql(
        "INSERT INTO analysis_day(account_id,range_key,day,observed_at,sizing_fallback) VALUES(1,'all','2026-01-02','2026-01-02',0)"
    )
    assert business_evidence(
        connection.exec_driver_sql("SELECT source_version FROM account_analysis WHERE account_id=1").scalar_one()
    ) == business_evidence(before)
    assert business_evidence(
        connection.exec_driver_sql("SELECT COUNT(*) FROM analysis_day").scalar_one()
    ) == business_evidence(1)


@pytest.fixture
def legacy_connection(tmp_path):
    database = tmp_path / "business-migration.db"
    initialize_native_database(database, revision="0033")
    with _connect(database) as connection:
        metadata = sa.MetaData()
        metadata.reflect(connection, only=["account", "executerecord", "execution_artifact"])
        now = "2026-01-02T09:30:00+08:00"
        connection.execute(
            metadata.tables["account"].insert(),
            {
                "id": 1,
                "name": "migration synthetic",
                "market": "FUTURES",
                "trade_channel": "ctp",
                "account_control_preset": "default",
                "account_config": {},
                "is_started": False,
                "cron_expr": "0 0 0 * * *",
                "brokerage": "",
                "weight_precision": 0,
                "created_at": now,
                "updated_at": now,
            },
        )
        primary = {"curr_target": {"A": 1}}
        connection.execute(
            metadata.tables["executerecord"].insert(),
            {
                "id": 1,
                "account_id": 1,
                "execution_id": "migration-exec",
                "created_at": now,
                "is_success": 1,
                "raw_input": primary,
                "raw_result": {"inputs": primary, "account_assets": {"total_asset": 10, "positions": []}},
            },
        )
    return database, metadata, now


def test_artifact_variants_and_before_assets_migrate_and_roundtrip_exactly(legacy_connection):
    database, metadata, now = legacy_connection
    audit_input = {"curr_target": {"A": 2}, "sizing_context": {"weight_precision": 0.05}}
    evidence = [
        ("STANDARD_INPUT", {"input": audit_input}),
        ("TARGET_SNAPSHOT", {"sizing_context": {"weight_precision": 0.05}, "curr_target": {"A": 2}}),
        ("ACCOUNT_SNAPSHOT_BEFORE", {"account_assets": {"total_asset": 9, "positions": []}}),
        ("ACCOUNT_SNAPSHOT", {"orders_count": 0, "account_assets": {"total_asset": 10, "positions": []}}),
    ]
    with _connect(database) as connection:
        for index, (kind, content) in enumerate(evidence, 1):
            connection.execute(
                metadata.tables["execution_artifact"].insert(),
                {
                    "id": index,
                    "execution_id": "migration-exec",
                    "artifact_type": kind,
                    "schema_version": 1,
                    "created_at": now,
                    "content": content,
                },
            )
    _upgrade(database)
    with _connect(database) as connection:
        rows = connection.exec_driver_sql("SELECT content FROM execution_artifact ORDER BY id").scalars().all()
        refs = [json.loads(content)["business_facts_ref"] for content in rows]
        assert refs[0]["input_id"] != connection.exec_driver_sql("SELECT input_id FROM executerecord").scalar_one()
        assert business_evidence(refs[3]["asset_id"]) == business_evidence("record:1:/account_assets")
        assert business_evidence(
            connection.exec_driver_sql(
                "SELECT owner_execution_id FROM execution_input WHERE id=?", (refs[0]["input_id"],)
            ).scalar_one()
        ) == business_evidence("migration-exec")
    assert business_evidence(_load_input(database, refs[0]["input_id"])) == business_evidence(audit_input)
    assert business_evidence(
        _load_input(database, refs[1]["input_id"])["sizing_context"]["weight_precision"]
    ) == business_evidence(0.05)
    _downgrade(database)
    with _connect(database) as connection:
        restored = connection.exec_driver_sql("SELECT content,schema_version FROM execution_artifact ORDER BY id").all()
        assert business_evidence(
            [(json.loads(content), version) for content, version in restored]
        ) == business_evidence([(content, 1) for _kind, content in evidence])
        assert business_evidence([set(json.loads(content)) for content, _version in restored]) == business_evidence(
            [set(content) for _kind, content in evidence]
        )


def test_downgrade_rejects_unrepresented_pending_input_without_mutation(legacy_connection):
    database, _metadata, now = legacy_connection
    _upgrade(database)
    native_store_call(
        database,
        "freeze_execution_input",
        {
            "account_id": 1,
            "execution_id": "pending-only",
            "input_id": "pending-only",
            "created_at": now,
            "payload": {"curr_target": {}},
        },
    )
    with _connect(database) as connection:
        before = connection.exec_driver_sql("SELECT name,sql FROM sqlite_master ORDER BY name").all()
        inputs_before = connection.exec_driver_sql("SELECT * FROM execution_input ORDER BY id").all()
    with pytest.raises(
        require_native_store("migration_downgrade").NativeError, match="unrepresented pending execution input"
    ):
        _downgrade(database)
    with _connect(database) as connection:
        assert business_evidence(
            connection.exec_driver_sql("SELECT name,sql FROM sqlite_master ORDER BY name").all()
        ) == business_evidence(before)
        assert business_evidence(
            connection.exec_driver_sql("SELECT * FROM execution_input ORDER BY id").all()
        ) == business_evidence(inputs_before)
        assert "raw_input" not in {column["name"] for column in sa.inspect(connection).get_columns("executerecord")}
        assert connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == "0034"


def test_terminal_delete_retains_asset_and_input_referenced_by_audit(legacy_connection):
    database, metadata, now = legacy_connection
    with _connect(database) as connection:
        connection.execute(
            metadata.tables["execution_artifact"].insert(),
            {
                "execution_id": "migration-exec",
                "artifact_type": "ACCOUNT_SNAPSHOT",
                "schema_version": 1,
                "created_at": now,
                "content": {"account_assets": {"total_asset": 10, "positions": []}},
            },
        )
    _upgrade(database)
    with _connect(database) as connection:
        input_id = connection.exec_driver_sql("SELECT input_id FROM executerecord").scalar_one()
        connection.exec_driver_sql("DELETE FROM executerecord WHERE id=1")
        asset = connection.exec_driver_sql(
            "SELECT record_id,input_id FROM business_asset_snapshot WHERE asset_id='record:1:/account_assets'"
        ).one()
        assert business_evidence(asset) == business_evidence((None, input_id))
        assert business_evidence(
            connection.exec_driver_sql("SELECT COUNT(*) FROM execution_input WHERE id=?", (input_id,)).scalar_one()
        ) == business_evidence(1)
        assert business_evidence(
            connection.exec_driver_sql("SELECT COUNT(*) FROM business_asset_binding").scalar_one()
        ) == business_evidence(0)


def _decision_event(connection, now, details):
    events = sa.Table("execution_event", sa.MetaData(), autoload_with=connection)
    connection.execute(
        events.insert(),
        {
            "execution_id": "migration-exec",
            "account_id": 1,
            "channel": "ctp",
            "algorithm": "synthetic",
            "schema_version": 1,
            "event_uid": "migration-event",
            "event_type": "TARGET_COMPUTED",
            "status": "INFO",
            "reason_family": "INPUT",
            "reason_code": "COMMON.TARGET_COMPUTED",
            "ts_local_created": now,
            "ts_persisted": now,
            "seq": 1,
            "details": details,
        },
    )


def test_event_target_reference_survives_terminal_delete_and_downgrades(legacy_connection):
    database, _metadata, now = legacy_connection
    original = {"debug": {"source": "synthetic"}, "decision": {"symbol_count": 1, "curr_target": {"A": 1}}}
    with _connect(database) as connection:
        _decision_event(connection, now, original)
    _upgrade(database)
    with _connect(database) as connection:
        stored = json.loads(connection.exec_driver_sql("SELECT details FROM execution_event").scalar_one())
        assert business_evidence(stored["decision"]["business_facts_ref"]["select_fields"]) == business_evidence(
            ["curr_target"]
        )
        input_id = stored["decision"]["business_facts_ref"]["input_id"]
    assert business_evidence(_load_input(database, input_id)) == business_evidence({"curr_target": {"A": 1}})
    with _connect(database) as connection:
        connection.exec_driver_sql("DELETE FROM executerecord WHERE id=1")
        assert business_evidence(
            connection.exec_driver_sql("SELECT COUNT(*) FROM execution_input WHERE id=?", (input_id,)).scalar_one()
        ) == business_evidence(1)
    _downgrade(database)
    with _connect(database) as connection:
        restored = json.loads(connection.exec_driver_sql("SELECT details FROM execution_event").scalar_one())
        assert business_evidence(restored) == business_evidence(original)
        assert business_evidence(list(restored["decision"])) == business_evidence(list(original["decision"]))


def test_migrated_decision_cold_resolver_uses_explicit_original_selector(legacy_connection, tmp_path):
    database, _metadata, now = legacy_connection
    original = {"decision": {"symbol_count": 1, "curr_target": {"A": 1}}}
    with _connect(database) as connection:
        _decision_event(connection, now, original)
    _upgrade(database)
    resolved = json.loads(
        native_store_call(database, "list_execution_events_json", {"execution_id": "migration-exec"})
    )["data"][0]["details"]
    decision = {key: value for key, value in resolved["decision"].items() if key != "business_facts_ref"}
    assert business_evidence(decision) == business_evidence(original["decision"])
    assert business_evidence(list(decision)) == business_evidence(list(original["decision"]))


def test_preorder_audit_input_and_asset_migrate_without_terminal_record(legacy_connection):
    database, metadata, now = legacy_connection
    with _connect(database) as connection:
        connection.exec_driver_sql("DELETE FROM executerecord")
        _decision_event(connection, now, {"decision": {"curr_target": {"A": 0}}})
        for kind, content in (
            ("STANDARD_INPUT", {"input": {"curr_target": {"A": 0}}}),
            ("ACCOUNT_SNAPSHOT_BEFORE", {"account_assets": {"total_asset": 9, "positions": []}}),
        ):
            connection.execute(
                metadata.tables["execution_artifact"].insert(),
                {
                    "execution_id": "migration-exec",
                    "artifact_type": kind,
                    "schema_version": 1,
                    "created_at": now,
                    "content": content,
                },
            )
    _upgrade(database)
    with _connect(database) as connection:
        assert business_evidence(
            connection.exec_driver_sql("SELECT COUNT(*) FROM executerecord").scalar_one()
        ) == business_evidence(0)
        assert business_evidence(
            connection.exec_driver_sql(
                "SELECT COUNT(*) FROM execution_input WHERE execution_id='migration-exec'"
            ).scalar_one()
        ) == business_evidence(1)
        assert business_evidence(
            connection.exec_driver_sql("SELECT scope FROM business_asset_snapshot").scalar_one()
        ) == business_evidence("before")
    _downgrade(database)
    with _connect(database) as connection:
        assert business_evidence(
            connection.exec_driver_sql("SELECT COUNT(*) FROM execution_artifact").scalar_one()
        ) == business_evidence(2)


def _summary_fixture(connection, metadata, now, *, captured=None):
    symbol = {
        "status": "SUCCEEDED",
        "orders": [{"order_id": "one", "symbol": "A", "direction": "BUY", "price": 1, "volume": 1, "filled_volume": 1}],
        "trades": [],
        "sizing": {"target_quantity": 1},
    }
    result = {
        "status": "SUCCEEDED",
        "success": True,
        "inputs": {"curr_target": {"A": 1}},
        "symbol_results": {"A": symbol},
    }
    if captured is not None:
        result["extra"] = {"server_notification_context": captured}
    connection.execute(
        metadata.tables["executerecord"]
        .update()
        .where(metadata.tables["executerecord"].c.id == 1)
        .values(raw_result=result)
    )
    summary = {"status": "SUCCEEDED", "symbol_results": {"A": symbol}}
    connection.execute(
        metadata.tables["execution_artifact"].insert(),
        {
            "execution_id": "migration-exec",
            "artifact_type": "EXECUTION_SUMMARY",
            "schema_version": 2,
            "created_at": now,
            "content": summary,
        },
    )
    return result, summary


def test_derivable_summary_is_removed_without_losing_execution_facts(legacy_connection):
    database, metadata, now = legacy_connection
    with _connect(database) as connection:
        result, _summary = _summary_fixture(connection, metadata, now)
    _upgrade(database)
    with _connect(database) as connection:
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM execution_artifact").scalar_one() == 0
    actual = read_view(database, "read_execution_result", {"account_id": 1, "record_id": 1})
    assert business_evidence(actual) == business_evidence(result)
    _downgrade(database)
    with _connect(database) as connection:
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM execution_artifact").scalar_one() == 0
        restored = json.loads(
            connection.exec_driver_sql("SELECT raw_result FROM executerecord WHERE id=1").scalar_one()
        )
        assert business_evidence(restored) == business_evidence(result)


def _summary_time_fixture(connection, metadata, now, *, root_orders=False):
    result, summary = _summary_fixture(connection, metadata, now)
    order = result["symbol_results"]["A"]["orders"][0]
    order.update(update_time="2026-10-15T18:38:34.582412", extra={})
    if root_orders:
        result["orders"] = result["symbol_results"]["A"].pop("orders")
        summary["orders"] = result["orders"]
    else:
        result["symbol_results"]["A/B~C"] = result["symbol_results"].pop("A")
        summary["symbol_results"] = result["symbol_results"]
    summary = deepcopy(summary)
    summary_order = summary["orders"][0] if root_orders else summary["symbol_results"]["A/B~C"]["orders"][0]
    summary_order["update_time"] = "2026-10-15T18:38:33.939061"
    result["audit_evidence"] = {"original": {"zero": 0, "unknown": None}}
    connection.execute(
        metadata.tables["executerecord"]
        .update()
        .where(metadata.tables["executerecord"].c.id == 1)
        .values(raw_result=result)
    )
    connection.execute(metadata.tables["execution_artifact"].update().values(content=summary))
    return result, summary, summary_order


@pytest.mark.parametrize("root_orders", [False, True])
def test_legacy_order_times_are_preserved_as_sparse_evidence(legacy_connection, root_orders):
    database, metadata, now = legacy_connection
    with _connect(database) as connection:
        result, summary, _ = _summary_time_fixture(connection, metadata, now, root_orders=root_orders)
        connection.execute(
            metadata.tables["execution_artifact"].insert(),
            {
                "execution_id": "migration-exec",
                "artifact_type": "execution_summary",
                "schema_version": 2,
                "created_at": now,
                "content": summary,
            },
        )
        sources = connection.exec_driver_sql("SELECT id,artifact_type FROM execution_artifact ORDER BY id").all()
    _upgrade(database)
    actual = read_view(database, "read_execution_result", {"account_id": 1, "record_id": 1})
    evidence = actual["audit_evidence"].pop("legacy_order_update_times")
    assert actual["audit_evidence"] == result["audit_evidence"]
    assert business_evidence(actual) == business_evidence(result)
    assert evidence == [
        {
            "artifact_id": artifact_id,
            "artifact_type": kind,
            "schema_version": 2,
            "created_at": now,
            "orders": [
                {
                    "path": "/orders/0" if root_orders else "/symbol_results/A~1B~0C/orders/0",
                    "order_id": "one",
                    "summary_update_time": "2026-10-15T18:38:33.939061",
                    "result_update_time": "2026-10-15T18:38:34.582412",
                }
            ],
        }
        for artifact_id, kind in sources
    ]
    _upgrade(database)
    repeated = read_view(database, "read_execution_result", {"account_id": 1, "record_id": 1})
    assert repeated["audit_evidence"]["legacy_order_update_times"] == evidence
    with _connect(database) as connection:
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM execution_artifact").scalar_one() == 0
        assert connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == "0034"
        assert connection.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
    _downgrade(database)
    with _connect(database) as connection:
        restored = json.loads(
            connection.exec_driver_sql("SELECT raw_result FROM executerecord WHERE id=1").scalar_one()
        )
        assert restored["audit_evidence"]["legacy_order_update_times"] == evidence
        assert business_evidence(restored) == business_evidence(result)
    _upgrade(database)
    assert (
        read_view(database, "read_execution_result", {"account_id": 1, "record_id": 1})["audit_evidence"][
            "legacy_order_update_times"
        ]
        == evidence
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("price", 99),
        ("status", "已成交"),
        ("filled_volume", 2),
        ("order_id", "other"),
        ("update_time", "invalid"),
        ("update_timestamp", 1),
        ("extra", {"_update_time_str": "2026-10-15T18:38:33.939061"}),
    ],
)
def test_legacy_order_time_compatibility_still_rejects_other_conflicts(legacy_connection, field, value):
    database, metadata, now = legacy_connection
    with _connect(database) as connection:
        _result, summary, order = _summary_time_fixture(connection, metadata, now)
        # 首条摘要会写入时间证据；第二条冲突必须让它一起回滚。
        connection.execute(
            metadata.tables["execution_artifact"].insert(),
            {
                "execution_id": "migration-exec",
                "artifact_type": "execution_summary",
                "schema_version": 2,
                "created_at": now,
                "content": {**summary, "symbol_results": deepcopy(summary["symbol_results"])},
            },
        )
        order[field] = value
        connection.execute(
            metadata.tables["execution_artifact"]
            .update()
            .where(metadata.tables["execution_artifact"].c.id == 2)
            .values(content=summary)
        )
        before = connection.exec_driver_sql("SELECT * FROM execution_artifact ORDER BY id").all()
    with pytest.raises(require_native_store("migration_upgrade").NativeError, match="independent summary field"):
        _upgrade(database)
    with _connect(database) as connection:
        assert connection.exec_driver_sql("SELECT * FROM execution_artifact ORDER BY id").all() == before
        assert connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == "0033"
        assert not connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE name='execution_extension'").all()


@pytest.mark.parametrize("explicit", ["timestamp", "extra"])
def test_explicit_order_times_are_not_treated_as_legacy_clock_loss(legacy_connection, explicit):
    database, metadata, now = legacy_connection
    with _connect(database) as connection:
        result, summary, order = _summary_time_fixture(connection, metadata, now)
        fact = result["symbol_results"]["A/B~C"]["orders"][0]
        if explicit == "timestamp":
            order["update_timestamp"] = fact["update_timestamp"] = 1
        else:
            order["extra"] = fact["extra"] = {"_update_time_str": fact["update_time"]}
        connection.execute(metadata.tables["executerecord"].update().values(raw_result=result))
        connection.execute(metadata.tables["execution_artifact"].update().values(content=summary))
    with pytest.raises(require_native_store("migration_upgrade").NativeError, match="independent summary field"):
        _upgrade(database)


def test_late_outbox_keeps_full_capture_with_legacy_summary_reference(legacy_connection):
    database, metadata, now = legacy_connection
    captured = {
        "execution": {"id": "migration-exec"},
        "positions": [],
        "orders": [],
        "default_feishu_variables": {"positions": []},
    }
    with _connect(database) as connection:
        _summary_fixture(connection, metadata, now, captured=captured)
    _upgrade(database)
    event_id = "execution:migration-exec"
    with _connect(database) as connection:
        connection.execute(
            table("notification_event").insert(),
            {
                "id": event_id,
                "account_id": 1,
                "created_at": now,
                "event_type": "execution.finished",
                "context": captured,
                "status": "succeeded",
            },
        )
        connection.execute(
            table("execution_intent").insert(),
            {
                "execution_id": "migration-exec",
                "account_id": 1,
                "kind": "scheduled",
                "trigger_source": "scheduled",
                "status": "RUNNING",
                "payload": {},
                "created_at": now,
                "started_at": now,
            },
        )
        connection.execute(
            table("supplement_group").insert(),
            {
                "id": "late-group",
                "account_id": 1,
                "fingerprint": "late-context",
                "base_scheduled_at": now,
                "expires_at": now,
                "configured_count": 1,
                "effective_count": 1,
                "status": "active",
            },
        )
        connection.execute(
            table("supplement_step").insert(),
            {
                "id": "late-step",
                "group_id": "late-group",
                "index": 1,
                "scheduled_at": now,
                "status": "running",
                "execution_id": "migration-exec",
            },
        )
    assert native_store_call(
        database,
        "intent_finish",
        {"execution_id": "migration-exec", "status": "SUCCEEDED", "finished_at": now, "now": now},
    )["changed"]
    with _connect(database) as connection:
        extra = json.loads(
            connection.exec_driver_sql("SELECT extra FROM execution_extension WHERE record_id=1").scalar_one()
        )
        reference = extra["server_notification_context"]
        assert reference is not None
        assert reference == captured
    result = read_view(database, "read_execution_result", {"account_id": 1, "record_id": 1})
    assert business_evidence(result["extra"]["server_notification_context"]) == business_evidence(captured)
    with _connect(database) as connection:
        connection.exec_driver_sql(
            "UPDATE notification_event SET status='failed',error='synthetic' WHERE id='execution:migration-exec'"
        )
        connection.exec_driver_sql("UPDATE notification_event SET context='{}' WHERE id='execution:migration-exec'")
        connection.exec_driver_sql("DELETE FROM notification_event WHERE id='execution:migration-exec'")
    result = read_view(database, "read_execution_result", {"account_id": 1, "record_id": 1})
    assert result["extra"]["server_notification_context"] == captured


def test_migration_context_dedup_requires_existing_outbox_and_downgrades_losslessly(legacy_connection):
    database, metadata, now = legacy_connection
    captured = {
        "execution": {"id": "migration-exec"},
        "positions": [],
        "orders": [],
        "default_feishu_variables": {"positions": []},
    }
    with _connect(database) as connection:
        _summary_fixture(connection, metadata, now, captured=captured)
        connection.execute(
            table("notification_event").insert(),
            {
                "id": "execution:migration-exec",
                "account_id": 1,
                "created_at": now,
                "event_type": "execution.finished",
                "context": captured,
                "status": "calling",
            },
        )
    _upgrade(database)
    with _connect(database) as connection:
        persisted = json.loads(
            connection.exec_driver_sql("SELECT extra FROM execution_extension WHERE record_id=1").scalar_one()
        )
        assert persisted["server_notification_context"] == captured
    _downgrade(database)
    with _connect(database) as connection:
        restored = json.loads(
            connection.exec_driver_sql("SELECT raw_result FROM executerecord WHERE id=1").scalar_one()
        )
        assert business_evidence(restored["extra"]["server_notification_context"]) == business_evidence(captured)
        assert business_evidence(
            connection.exec_driver_sql("SELECT status FROM notification_event").scalar_one()
        ) == business_evidence("calling")


def test_independent_asset_indices_version_account_and_preserve_execution_assets(legacy_connection):
    database, metadata, now = legacy_connection
    with _connect(database) as connection:
        connection.execute(
            metadata.tables["executerecord"].insert(),
            {
                "id": 2,
                "account_id": 1,
                "execution_id": "migration-exec-2",
                "created_at": now,
                "is_success": 1,
                "raw_input": {"curr_target": {}},
                "raw_result": {"account_assets": {"total_asset": 20, "positions": []}},
            },
        )
    _upgrade(database)
    with _connect(database) as connection:
        previous_version = connection.exec_driver_sql(
            "SELECT source_version FROM account_analysis WHERE account_id=1"
        ).scalar_one()
        snapshots = table("account_asset_snapshot")
        connection.execute(
            snapshots.insert(),
            {
                "id": 101,
                "account_id": 1,
                "source": "execution",
                "execution_id": "migration-exec-2",
                "created_at": now,
                "asset_fact_id": "record:2:/account_assets",
            },
        )
    manual = native_store_call(
        database,
        "asset_snapshot_write",
        {"account_id": 1, "created_at": now, "account_assets": {"total_asset": 99, "positions": []}},
    )
    assert manual["id"] == 102
    with _connect(database) as connection:
        assert (
            connection.exec_driver_sql("SELECT source_version FROM account_analysis WHERE account_id=1").scalar_one()
            > previous_version
        )
        connection.exec_driver_sql("UPDATE business_asset_snapshot SET total_asset=15 WHERE record_id=1")
    result = read_view(database, "read_execution_result", {"account_id": 1, "record_id": 1})
    assert result["account_assets"]["total_asset"] == 15
