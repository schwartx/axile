"""Revision 0037 trigger invariants independent of the legacy JSON parser."""

import asyncio
import json

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from axile.server.alembic.business0037_invalidation import install_triggers
from axile.server.alembic.business0037_migration import _load_input
from scripts.business_facts_contract import business_evidence
from tests.unit.server.test_initial_migration import _MIGRATIONS_DIR, _load_migration


@pytest.fixture(scope="session", autouse=True)
def _ensure_database_schema():
    """These migration tests own an isolated synthetic schema exclusively."""
    yield


@pytest.fixture
def fact_connection():
    metadata = sa.MetaData()
    account = sa.Table("account", metadata, sa.Column("id", sa.Integer, primary_key=True))
    inputs = sa.Table(
        "execution_input",
        metadata,
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("account_id", sa.Integer, sa.ForeignKey(account.c.id), nullable=False),
        sa.Column("execution_id", sa.Text),
    )
    records = sa.Table(
        "executerecord",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("account_id", sa.Integer, sa.ForeignKey(account.c.id), nullable=False),
        sa.Column("execution_id", sa.Text),
        sa.Column("input_id", sa.Text, sa.ForeignKey(inputs.c.id)),
        sa.Column("result_input_id", sa.Text, sa.ForeignKey(inputs.c.id)),
    )
    child = sa.Table(
        "business_test_trade",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("record_id", sa.Integer, sa.ForeignKey(records.c.id, ondelete="CASCADE"), nullable=False),
        sa.Column("price", sa.Float),
    )
    targets = sa.Table(
        "execution_input_test_target",
        metadata,
        sa.Column("input_id", sa.Text, sa.ForeignKey(inputs.c.id, ondelete="CASCADE"), primary_key=True),
        sa.Column("quantity", sa.Float),
    )
    connection = sa.create_engine("sqlite://").connect()
    try:
        metadata.create_all(connection)
        connection.exec_driver_sql("CREATE TABLE execution_artifact(execution_id TEXT,content TEXT)")
        connection.exec_driver_sql("CREATE TABLE execution_event(details TEXT)")
        connection.exec_driver_sql(
            "CREATE TABLE account_asset_snapshot(id INTEGER PRIMARY KEY,account_id INTEGER,asset_fact_id TEXT)"
        )
        connection.exec_driver_sql("CREATE TABLE business_asset_snapshot(asset_id TEXT,input_id TEXT)")
        connection.exec_driver_sql("CREATE TABLE target_weight_snapshot(account_id INTEGER)")
        connection.exec_driver_sql(
            "CREATE TABLE account_analysis(account_id INTEGER PRIMARY KEY,source_version INTEGER NOT NULL DEFAULT 1,"
            "requested INTEGER NOT NULL DEFAULT 0,failures INTEGER NOT NULL DEFAULT 0,current_snapshot TEXT,"
            "running_version INTEGER,retry_at REAL NOT NULL DEFAULT 0,error TEXT)"
        )
        for name in (
            "analysis_source_transaction_fee",
            "analysis_source_transaction",
            "analysis_source_attempt",
            "analysis_source_position",
            "analysis_source_execution_symbol",
            "analysis_source_trade",
            "analysis_source_execution",
            "replay_observation_symbol",
            "replay_observation",
        ):
            connection.exec_driver_sql(f"CREATE TABLE {name}(record_id INTEGER)")
        connection.execute(account.insert(), [{"id": 1}, {"id": 2}])
        connection.execute(inputs.insert(), [{"id": "one", "account_id": 1}, {"id": "two", "account_id": 2}])
        connection.execute(
            records.insert(),
            [
                {"id": 1, "account_id": 1, "input_id": "one"},
                {"id": 2, "account_id": 2, "input_id": "two"},
            ],
        )
        connection.exec_driver_sql("INSERT INTO account_analysis(account_id) VALUES(1),(2)")
        install_triggers(connection, (records, inputs, child, targets))
        yield connection
    finally:
        connection.close()


def _seed_derived(connection):
    for name in ("analysis_source_execution", "replay_observation", "replay_observation_symbol"):
        connection.exec_driver_sql(f"INSERT INTO {name}(record_id) VALUES(1),(2)")


def test_child_write_invalidates_only_its_record_and_versions_account(fact_connection):
    connection = fact_connection
    _seed_derived(connection)
    connection.exec_driver_sql("INSERT INTO business_test_trade(id,record_id,price) VALUES(1,1,0)")
    assert business_evidence(
        connection.exec_driver_sql("SELECT record_id FROM analysis_source_execution").scalars().all()
    ) == business_evidence([2])
    assert business_evidence(
        connection.exec_driver_sql("SELECT record_id FROM replay_observation").scalars().all()
    ) == business_evidence([2])
    assert business_evidence(
        connection.exec_driver_sql("SELECT account_id,source_version FROM account_analysis ORDER BY account_id").all()
    ) == business_evidence(
        [
            (1, 2),
            (2, 1),
        ]
    )


def test_input_write_does_not_match_unrelated_null_result_input_refs(fact_connection):
    connection = fact_connection
    _seed_derived(connection)
    connection.exec_driver_sql("INSERT INTO execution_input_test_target(input_id,quantity) VALUES('one',0)")
    assert business_evidence(
        connection.exec_driver_sql("SELECT record_id FROM analysis_source_execution").scalars().all()
    ) == business_evidence([2])
    assert business_evidence(
        connection.exec_driver_sql("SELECT source_version FROM account_analysis WHERE account_id=2").scalar_one()
    ) == business_evidence(1)


def test_reparent_update_invalidates_old_and_new_owners(fact_connection):
    connection = fact_connection
    connection.exec_driver_sql("INSERT INTO business_test_trade(id,record_id,price) VALUES(1,1,1)")
    _seed_derived(connection)
    connection.exec_driver_sql("UPDATE business_test_trade SET record_id=2 WHERE id=1")
    assert business_evidence(
        connection.exec_driver_sql("SELECT COUNT(*) FROM analysis_source_execution").scalar_one()
    ) == business_evidence(0)
    assert business_evidence(
        connection.exec_driver_sql("SELECT COUNT(*) FROM replay_observation").scalar_one()
    ) == business_evidence(0)
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
        connection.exec_driver_sql("INSERT INTO business_test_trade(id,record_id) VALUES(99,99)")
    with pytest.raises(sa.exc.IntegrityError, match="still referenced"):
        connection.exec_driver_sql("DELETE FROM execution_input WHERE id='one'")


def test_record_delete_cleans_children_even_with_foreign_keys_off(fact_connection):
    connection = fact_connection
    connection.exec_driver_sql("INSERT INTO business_test_trade(id,record_id,price) VALUES(1,1,1)")
    connection.exec_driver_sql("DELETE FROM executerecord WHERE id=1")
    assert business_evidence(
        connection.exec_driver_sql("SELECT COUNT(*) FROM business_test_trade").scalar_one()
    ) == business_evidence(0)


def test_analysis_publication_does_not_invalidate_business_source(fact_connection):
    connection = fact_connection
    before = connection.exec_driver_sql("SELECT source_version FROM account_analysis WHERE account_id=1").scalar_one()
    connection.exec_driver_sql("INSERT INTO analysis_source_execution(record_id) VALUES(1)")
    assert business_evidence(
        connection.exec_driver_sql("SELECT source_version FROM account_analysis WHERE account_id=1").scalar_one()
    ) == business_evidence(before)
    assert business_evidence(
        connection.exec_driver_sql("SELECT COUNT(*) FROM analysis_source_execution").scalar_one()
    ) == business_evidence(1)


@pytest.fixture
def legacy_connection():
    engine = sa.create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            for path in sorted(_MIGRATIONS_DIR.glob("*.py")):
                if path.stem[:4].isdigit() and int(path.stem[:4]) <= 36:
                    migration = _load_migration(path)
                    migration.op = Operations(MigrationContext.configure(connection))
                    migration.upgrade()
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
            yield connection, metadata, now
    finally:
        engine.dispose()


def _revision(connection):
    migration = _load_migration(_MIGRATIONS_DIR / "0037_business_execution_facts.py")
    migration.op = Operations(MigrationContext.configure(connection))
    return migration


def test_artifact_variants_and_before_assets_migrate_and_roundtrip_exactly(legacy_connection):
    connection, metadata, now = legacy_connection
    audit_input = {"curr_target": {"A": 2}, "sizing_context": {"weight_precision": 0.05}}
    evidence = [
        ("STANDARD_INPUT", {"input": audit_input}),
        ("TARGET_SNAPSHOT", {"sizing_context": {"weight_precision": 0.05}, "curr_target": {"A": 2}}),
        ("ACCOUNT_SNAPSHOT_BEFORE", {"account_assets": {"total_asset": 9, "positions": []}}),
        ("ACCOUNT_SNAPSHOT", {"orders_count": 0, "account_assets": {"total_asset": 10, "positions": []}}),
    ]
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
    migration = _revision(connection)
    migration.upgrade()
    rows = connection.exec_driver_sql("SELECT content FROM execution_artifact ORDER BY id").scalars().all()
    refs = [json.loads(content)["business_facts_ref"] for content in rows]
    assert business_evidence(_load_input(connection, refs[0]["input_id"])) == business_evidence(audit_input)
    assert refs[0]["input_id"] != connection.exec_driver_sql("SELECT input_id FROM executerecord").scalar_one()
    assert business_evidence(
        _load_input(connection, refs[1]["input_id"])["sizing_context"]["weight_precision"]
    ) == business_evidence(0.05)
    assert business_evidence(refs[3]["asset_id"]) == business_evidence("record:1:/account_assets")
    assert business_evidence(
        connection.exec_driver_sql(
            "SELECT owner_execution_id FROM execution_input WHERE id=?", (refs[0]["input_id"],)
        ).scalar_one()
    ) == business_evidence("migration-exec")
    migration.downgrade()
    restored = connection.exec_driver_sql("SELECT content,schema_version FROM execution_artifact ORDER BY id").all()
    assert business_evidence([(json.loads(content), version) for content, version in restored]) == business_evidence(
        [(content, 1) for _kind, content in evidence]
    )
    assert business_evidence([set(json.loads(content)) for content, _version in restored]) == business_evidence(
        [set(content) for _kind, content in evidence]
    )


def test_downgrade_rejects_unrepresented_pending_input_without_mutation(legacy_connection):
    connection, _metadata, now = legacy_connection
    migration = _revision(connection)
    migration.upgrade()
    from axile.server.business_facts.inputs import encode_inputs

    for table, rows in encode_inputs(
        "pending-only", {"curr_target": {}}, account_id=1, execution_id="pending-only", created_at=now
    ).items():
        if rows:
            connection.execute(table.insert(), rows)
    before = connection.exec_driver_sql("SELECT name,sql FROM sqlite_master ORDER BY name").all()
    with pytest.raises(ValueError, match="unrepresented pending execution input"):
        migration.downgrade()
    assert business_evidence(
        connection.exec_driver_sql("SELECT name,sql FROM sqlite_master ORDER BY name").all()
    ) == business_evidence(before)
    assert "raw_input" not in {column["name"] for column in sa.inspect(connection).get_columns("executerecord")}


def test_terminal_delete_retains_asset_and_input_referenced_by_audit(legacy_connection):
    connection, metadata, now = legacy_connection
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
    _revision(connection).upgrade()
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
    connection, _metadata, now = legacy_connection
    original = {"debug": {"source": "synthetic"}, "decision": {"symbol_count": 1, "curr_target": {"A": 1}}}
    _decision_event(connection, now, original)
    migration = _revision(connection)
    migration.upgrade()
    stored = json.loads(connection.exec_driver_sql("SELECT details FROM execution_event").scalar_one())
    assert business_evidence(stored["decision"]["business_facts_ref"]["select_fields"]) == business_evidence(
        ["curr_target"]
    )
    input_id = stored["decision"]["business_facts_ref"]["input_id"]
    assert business_evidence(_load_input(connection, input_id)) == business_evidence({"curr_target": {"A": 1}})
    connection.exec_driver_sql("DELETE FROM executerecord WHERE id=1")
    assert business_evidence(
        connection.exec_driver_sql("SELECT COUNT(*) FROM execution_input WHERE id=?", (input_id,)).scalar_one()
    ) == business_evidence(1)
    migration.downgrade()
    restored = json.loads(connection.exec_driver_sql("SELECT details FROM execution_event").scalar_one())
    assert business_evidence(restored) == business_evidence(original)
    assert business_evidence(list(restored["decision"])) == business_evidence(list(original["decision"]))


def test_migrated_decision_cold_resolver_uses_explicit_original_selector(legacy_connection, tmp_path):
    connection, _metadata, now = legacy_connection
    original = {"decision": {"symbol_count": 1, "curr_target": {"A": 1}}}
    _decision_event(connection, now, original)
    _revision(connection).upgrade()
    details = json.loads(connection.exec_driver_sql("SELECT details FROM execution_event").scalar_one())
    database = tmp_path / "migrated-event-selector.sqlite"
    database.write_bytes(connection.connection.driver_connection.serialize())
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from axile.server.business_facts.artifacts import resolve_event_details

    async def _resolve():
        engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
        try:
            async with async_sessionmaker(engine)() as session:
                resolved = await resolve_event_details(
                    session,
                    {"execution_id": "migration-exec", "event_type": "target_computed", "details": details},
                    account_id=1,
                )
                decision = {key: value for key, value in resolved["decision"].items() if key != "business_facts_ref"}
                assert business_evidence(decision) == business_evidence(original["decision"])
                assert business_evidence(list(decision)) == business_evidence(list(original["decision"]))
        finally:
            await engine.dispose()

    asyncio.run(_resolve())


def test_preorder_audit_input_and_asset_migrate_without_terminal_record(legacy_connection):
    connection, metadata, now = legacy_connection
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
    migration = _revision(connection)
    migration.upgrade()
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
    migration.downgrade()
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
    result = {"inputs": {"curr_target": {"A": 1}}, "symbol_results": {"A": symbol}}
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


def test_summary_compaction_protects_dependencies_and_downgrade_restores_wire(legacy_connection):
    connection, metadata, now = legacy_connection
    _result, summary = _summary_fixture(connection, metadata, now)
    migration = _revision(connection)
    migration.upgrade()
    compact = json.loads(connection.exec_driver_sql("SELECT content FROM execution_artifact").scalar_one())
    assert business_evidence(compact["business_facts_ref"]["view"]) == business_evidence("result")
    assert "orders" not in compact["symbol_results"]["A"]
    with pytest.raises(sa.exc.IntegrityError, match="summary must be materialized"):
        connection.exec_driver_sql("UPDATE execution_order SET price=2 WHERE record_id=1")
    with pytest.raises(sa.exc.IntegrityError, match="summary must be materialized"):
        connection.exec_driver_sql("DELETE FROM executerecord WHERE id=1")
    connection.exec_driver_sql("UPDATE executerecord SET display_error='display-only' WHERE id=1")
    migration.downgrade()
    restored = json.loads(connection.exec_driver_sql("SELECT content FROM execution_artifact").scalar_one())
    assert business_evidence(restored) == business_evidence(summary)
    assert set(restored["symbol_results"]["A"]) - {"availability"} == set(summary["symbol_results"]["A"])


def test_late_outbox_context_dedup_is_allowed_with_summary_reference(legacy_connection):
    connection, metadata, now = legacy_connection
    captured = {
        "execution": {"id": "migration-exec"},
        "positions": [],
        "orders": [],
        "default_feishu_variables": {"positions": []},
    }
    _summary_fixture(connection, metadata, now, captured=captured)
    _revision(connection).upgrade()
    events = sa.Table("notification_event", sa.MetaData(), autoload_with=connection)
    event_id = "execution:migration-exec"
    connection.execute(
        events.insert(),
        {
            "id": event_id,
            "account_id": 1,
            "created_at": now,
            "event_type": "execution.finished",
            "context": captured,
            "status": "succeeded",
        },
    )
    from axile.server.alembic.business0037_migration import reconstruct_result
    from axile.server.business_facts.extensions import execution_extension
    from axile.server.business_facts.head import execution_head
    from axile.server.business_facts.notification_context import compact_captured_context

    reference = compact_captured_context(captured, captured, event_id=event_id)
    assert reference is not None
    connection.execute(
        execution_extension.update()
        .where(execution_extension.c.record_id == 1)
        .values(extra={"server_notification_context": reference})
    )
    record = connection.execute(sa.select(execution_head).where(execution_head.c.id == 1)).mappings().one()
    assert business_evidence(
        reconstruct_result(connection, record)["extra"]["server_notification_context"]
    ) == business_evidence(captured)
    connection.exec_driver_sql(
        "UPDATE notification_event SET status='failed',error='synthetic' WHERE id='execution:migration-exec'"
    )
    with pytest.raises(sa.exc.IntegrityError, match="Frozen notification context"):
        connection.exec_driver_sql("UPDATE notification_event SET context='{}' WHERE id='execution:migration-exec'")
    with pytest.raises(sa.exc.IntegrityError, match="Frozen notification context"):
        connection.exec_driver_sql("DELETE FROM notification_event WHERE id='execution:migration-exec'")
    with pytest.raises(sa.exc.IntegrityError, match="Frozen notification context"):
        connection.exec_driver_sql(
            "INSERT OR REPLACE INTO notification_event(id,account_id,created_at,event_type,context,status) VALUES('execution:migration-exec',1,'synthetic','execution.finished','{}','pending')"
        )


def test_migration_context_dedup_requires_existing_outbox_and_downgrades_losslessly(legacy_connection):
    connection, metadata, now = legacy_connection
    captured = {
        "execution": {"id": "migration-exec"},
        "positions": [],
        "orders": [],
        "default_feishu_variables": {"positions": []},
    }
    _summary_fixture(connection, metadata, now, captured=captured)
    events = sa.Table("notification_event", sa.MetaData(), autoload_with=connection)
    connection.execute(
        events.insert(),
        {
            "id": "execution:migration-exec",
            "account_id": 1,
            "created_at": now,
            "event_type": "execution.finished",
            "context": captured,
            "status": "calling",
        },
    )
    migration = _revision(connection)
    migration.upgrade()
    persisted = json.loads(
        connection.exec_driver_sql("SELECT extra FROM execution_extension WHERE record_id=1").scalar_one()
    )
    assert business_evidence(
        persisted["server_notification_context"]["$axile_notification_context"]
    ) == business_evidence(1)
    assert "positions" not in persisted["server_notification_context"]
    migration.downgrade()
    restored = json.loads(connection.exec_driver_sql("SELECT raw_result FROM executerecord WHERE id=1").scalar_one())
    assert business_evidence(restored["extra"]["server_notification_context"]) == business_evidence(captured)
    assert business_evidence(
        connection.exec_driver_sql("SELECT status FROM notification_event").scalar_one()
    ) == business_evidence("calling")


def test_independent_asset_indices_preserve_old_execution_projections(legacy_connection):
    connection, metadata, now = legacy_connection
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
    _revision(connection).upgrade()
    from axile.server.business_facts import assets

    legacy = sa.MetaData()
    legacy.reflect(connection, only=["analysis_source_execution", "replay_observation", "replay_observation_symbol"])
    source_execution = legacy.tables["analysis_source_execution"]
    replay_observation = legacy.tables["replay_observation"]
    replay_observation_symbol = legacy.tables["replay_observation_symbol"]

    connection.execute(
        source_execution.insert(),
        {
            "record_id": 1,
            "account_id": 1,
            "execution_id": "migration-exec",
            "created_at": now,
            "is_success": 1,
            "time": 100.0,
            "success": True,
            "noop": False,
            "trade_count": 0,
        },
    )
    connection.execute(
        replay_observation.insert(),
        {
            "record_id": 1,
            "account_id": 1,
            "execution_id": "migration-exec",
            "time": now,
            "asset": 10,
            "sizing_fallback": True,
            "target_present": True,
            "target_weight_present": True,
            "target_units_present": False,
            "target_quantities_present": False,
        },
    )
    connection.execute(
        replay_observation_symbol.insert(), {"record_id": 1, "symbol": "A", "target": 1, "target_weight": 1}
    )
    previous_version = connection.exec_driver_sql(
        "SELECT source_version FROM account_analysis WHERE account_id=1"
    ).scalar_one()
    snapshots = sa.Table("account_asset_snapshot", sa.MetaData(), autoload_with=connection)
    connection.execute(
        snapshots.insert(),
        {
            "id": 101,
            "account_id": 1,
            "source": "execution",
            "execution_id": "migration-exec-2",
            "created_at": now,
            "asset_fact_id": assets.asset_fact_id(2),
        },
    )
    connection.execute(
        snapshots.insert(),
        {
            "id": 102,
            "account_id": 1,
            "source": "manual",
            "execution_id": None,
            "created_at": now,
            "asset_fact_id": None,
        },
    )
    for table, rows in assets.encode_snapshot_assets(
        102, {"account_assets": {"total_asset": 99, "positions": []}}
    ).items():
        if rows:
            connection.execute(table.insert(), rows)
    connection.execute(
        snapshots.update().where(snapshots.c.id == 102).values(asset_fact_id=assets.snapshot_asset_fact_id(102))
    )
    assert business_evidence(
        connection.execute(sa.select(source_execution.c.record_id)).scalars().all()
    ) == business_evidence([1])
    assert business_evidence(
        connection.execute(sa.select(replay_observation.c.record_id)).scalars().all()
    ) == business_evidence([1])
    assert (
        connection.exec_driver_sql("SELECT source_version FROM account_analysis WHERE account_id=1").scalar_one()
        > previous_version
    )
    connection.execute(
        assets.asset_snapshot.update().where(assets.asset_snapshot.c.record_id == 1).values(total_asset=15)
    )
    assert business_evidence(
        connection.execute(sa.select(sa.func.count()).select_from(source_execution)).scalar_one()
    ) == business_evidence(0)
    assert business_evidence(
        connection.execute(sa.select(sa.func.count()).select_from(replay_observation)).scalar_one()
    ) == business_evidence(0)
