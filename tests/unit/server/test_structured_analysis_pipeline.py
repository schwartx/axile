"""Real Rust publication, atomic native persistence and JSON-free result reads."""

import asyncio
import json
from contextlib import asynccontextmanager

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from axile.server.analysis_cost_facts import COST_FACT_TABLES
from axile.server.analysis_observations import write_observation_fact
from axile.server.analysis_source import write_source_facts
from axile.server.db.models import ExecuteRecord
from axile.server.db.models.analysis import analysis_snapshot as snapshots
from axile.server.db.models.analysis import analysis_state as states
from axile.server.db.models.analysis_result import RESULT_TABLES
from axile.server.db.models.analysis_source import source_execution
from axile.server.db.models.analysis_view import READ_VIEW_TABLES
from axile.server.db.models.performance import PerformanceSettings
from axile.server.native_analysis import NativeAnalysis, NativeAnalysisError, NativeAnalysisProtocolError
from axile.server.performance_analysis import LOGIC_VERSION, AnalysisManager, compute_batch, enqueue
from axile.server.performance_replay import ENGINE_VERSION
from axile.server.trading_calendar import CalendarDayDecision, CalendarDecisionStatus
from tests.unit.server._execution_test_support import build_account
from tests.unit.server.test_native_analysis_contract import _cost_records, _same
from tests.unit.server.test_native_analysis_contract import native_binary as native_binary
from tests.unit.server.test_performance_analysis import _create_schema

pytestmark = pytest.mark.contract


@pytest.fixture(autouse=True)
def fixed_calendar(monkeypatch):
    def evaluate(channel, day):
        return CalendarDayDecision(channel=str(channel), day=day, status=CalendarDecisionStatus.NOT_REQUIRED)

    monkeypatch.setattr("axile.server.performance.evaluate_channel_calendar_day", evaluate)
    monkeypatch.setattr("axile.server.analysis_calendar.evaluate_channel_calendar_day", evaluate)


@asynccontextmanager
async def _database(tmp_path, binary, monkeypatch):
    monkeypatch.setenv("AXILE_ANALYSIS_MODE", "required")
    monkeypatch.setenv("AXILE_ANALYSIS_BINARY", str(binary))
    path = tmp_path / "structured.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(_create_schema)
    records = _cost_records()
    # Python's timestamp()*1000 rounds this boundary differently from a direct
    # integer-microsecond / 1000 conversion. The final execution's estimated
    # trades and failure event must remain inside the inclusive range endpoint.
    records[2].created_at = "2026-01-05T09:00:00.000003+08:00"
    settings = PerformanceSettings(backtest_weight_type="cs", backtest_fee_rate=0.001)
    async with sessions() as session:
        session.add(build_account(id=7, backtest_weight_type="cs", backtest_fee_rate=0.001))
        await session.commit()
        for record in records:
            parent = ExecuteRecord(**vars(record))
            session.add(parent)
            await session.flush()
            await write_observation_fact(session, parent)
            assert await write_source_facts(session, parent)
        await session.commit()
    try:
        yield path, engine, sessions, AnalysisManager(sessions), records, settings
    finally:
        await engine.dispose()


async def _queue(sessions):
    async with sessions() as session:
        await enqueue(session, 7, force=True)


async def _state(sessions):
    async with sessions() as session:
        return (await session.execute(sa.select(states).where(states.c.account_id == 7))).mappings().one()


async def _counts(sessions):
    async with sessions() as session:
        return {
            table.name: await session.scalar(sa.select(sa.func.count()).select_from(table))
            for table in (snapshots, *RESULT_TABLES, *COST_FACT_TABLES, *READ_VIEW_TABLES)
        }


async def _snapshot(native, range_key="all"):
    encoded = await native.query(
        "snapshot", account_id=7, range=range_key, logic_version=LOGIC_VERSION, engine_version=ENGINE_VERSION
    )
    return json.loads(encoded)


def test_native_publication_matches_oracle_without_reparsing_history(native_binary, tmp_path, monkeypatch):
    async def check():
        async with _database(tmp_path, native_binary, monkeypatch) as (path, _, sessions, manager, records, settings):
            expected, _, _ = compute_batch(records, [], [], [], settings, trade_channel="ctp")

            def no_history(*_args, **_kwargs):
                raise AssertionError("A ready typed source must not be reparsed for a new analysis round")

            monkeypatch.setattr("axile.server.analysis_source.source_fact_values", no_history)
            monkeypatch.setattr("axile.server.analysis_observations.observation_fact_values", no_history)
            monkeypatch.setattr("axile.server.performance_analysis._prepare_records", no_history)
            await _queue(sessions)
            assert await manager.run_once()
            native = NativeAnalysis(binary=native_binary, database=path)
            for range_key in ("30", "90", "all"):
                actual = await _snapshot(native, range_key)
                assert actual["status"] == "ready"
                performance = expected[range_key]["performance"]
                performance.setdefault("executions", expected["all"]["performance"]["executions"])
                _same(actual["result"], performance)
                _same(actual["daily_costs"], expected[range_key]["daily_costs"])
                _same(actual["events"], expected[range_key]["events"])
                assert actual["event_count"] == expected[range_key]["event_count"]
            async with sessions() as session:
                row = (await session.execute(sa.select(snapshots))).mappings().one()
                assert row["result_schema_version"] == 2
                assert row["read_view_schema_version"] == 1
                assert row["ranges"] == row["settings"] == {}
                assert row["backtest_weight_type"] == "cs"
                assert row["backtest_fee_rate"] == 0.001
                for table_name in ("performance_cost_execution", "performance_cost_trade"):
                    payloads = (await session.execute(sa.text(f"SELECT payload FROM {table_name}"))).scalars().all()
                    assert payloads and set(payloads) == {"{}"}
            # A second full round must also consume the frozen source facts.
            await _queue(sessions)
            assert await manager.run_once()
            assert (await _snapshot(native))["status"] == "ready"

    asyncio.run(check())


def test_native_published_results_ignore_poisoned_legacy_json(native_binary, tmp_path, monkeypatch):
    async def check():
        async with _database(tmp_path, native_binary, monkeypatch) as (path, _, sessions, manager, _, _):
            await _queue(sessions)
            assert await manager.run_once()
            native = NativeAnalysis(binary=native_binary, database=path)
            before = await _snapshot(native)
            query = dict(
                account_id=7,
                snapshot_id=before["snapshot_id"],
                range="all",
                dimension="execution",
                limit=100,
                logic_version=LOGIC_VERSION,
                engine_version=ENGINE_VERSION,
            )
            costs = json.loads(await native.query("costs", **query))
            async with sessions() as session:
                for table, columns in (
                    ("performance_snapshot", ("ranges", "settings")),
                    ("performance_cost_execution", ("payload",)),
                    ("performance_cost_trade", ("payload",)),
                ):
                    for column in columns:
                        await session.execute(sa.text(f"UPDATE {table} SET {column}='invalid-json'"))
                await session.commit()
            _same(await _snapshot(native), before)
            _same(json.loads(await native.query("costs", **query)), costs)

    asyncio.run(check())


@pytest.mark.parametrize("interruption", ["process_error", "invalid_columns", "cancelled"])
def test_failed_publication_never_exposes_partial_results(native_binary, tmp_path, monkeypatch, interruption):
    import axile.server.native_analysis as native_module

    async def check():
        async with _database(tmp_path, native_binary, monkeypatch) as (_, _, sessions, manager, _, _):
            await _queue(sessions)
            assert await manager.run_once()
            previous = await _state(sessions)
            counts = await _counts(sessions)
            processes = []
            errors = []
            query = NativeAnalysis.query

            async def capture_error(self, command, **request):
                assert command == "publish_store", "The manager must use the new native writer"
                try:
                    return await query(self, command, **request)
                except NativeAnalysisError as error:
                    errors.append(error.code)
                    raise

            monkeypatch.setattr(NativeAnalysis, "query", capture_error)
            if interruption == "cancelled":
                write_request = native_module._write_request

                async def cancel_before_input(process, request):
                    if json.loads(request)["command"] == "publish_store":
                        # The real worker exists, but has not received a task.
                        # Cancellation must reap it before releasing this claim.
                        processes.append(process)
                        raise asyncio.CancelledError
                    await write_request(process, request)

                monkeypatch.setattr(native_module, "_write_request", cancel_before_input)
            else:
                async with sessions() as session:
                    if interruption == "invalid_columns":
                        # Rust validates the actual migrated schema before staging.
                        await session.execute(sa.text("ALTER TABLE performance_result_point ADD COLUMN invalid JSON"))
                    else:
                        await session.execute(
                            sa.text("""
                            CREATE TRIGGER reject_result_insert BEFORE INSERT ON performance_result_point
                            BEGIN SELECT RAISE(ABORT, 'synthetic native result write failure'); END
                        """)
                        )
                    await session.commit()
            await _queue(sessions)
            if interruption == "cancelled":
                with pytest.raises(asyncio.CancelledError):
                    await manager.run_once()
                assert len(processes) == 1 and processes[0].returncode is not None
            else:
                assert await manager.run_once()
                assert errors == [
                    "INVALID_NATIVE_PUBLICATION" if interruption == "invalid_columns" else "PUBLICATION_STAGE"
                ]
            current = await _state(sessions)
            assert current["current_snapshot"] == previous["current_snapshot"]
            assert current["published_version"] == previous["published_version"]
            assert current["running_version"] is None and current["running_token"] is None
            assert current["requested"]
            assert await _counts(sessions) == counts

    asyncio.run(check())


def test_wrong_receipt_version_recovers_the_committed_native_publication(native_binary, tmp_path, monkeypatch):
    async def check():
        async with _database(tmp_path, native_binary, monkeypatch) as (_, _, sessions, manager, _, _):
            await _queue(sessions)
            assert await manager.run_once()
            previous = await _state(sessions)
            query = NativeAnalysis.query
            publish_store = NativeAnalysis.publish_store
            published = []
            errors = []

            async def capture_invalid_receipt(self, **request):
                try:
                    return await publish_store(self, **request)
                except NativeAnalysisProtocolError as error:
                    errors.append(error.code)
                    raise

            async def must_recover_committed_task(_row):
                raise AssertionError("A rejected receipt must recover the committed snapshot before failing a task")

            async def corrupt_receipt(self, command, **request):
                assert command == "publish_store"
                receipt = json.loads(await query(self, command, **request))
                assert receipt["published"] is True
                published.append(receipt["snapshot_id"])
                # Unlike old result frames, this control receipt follows COMMIT.
                # Its corruption cannot undo an authoritative durable snapshot.
                receipt["source_version"] += 1
                return json.dumps(receipt).encode()

            monkeypatch.setattr(NativeAnalysis, "query", corrupt_receipt)
            monkeypatch.setattr(NativeAnalysis, "publish_store", capture_invalid_receipt)
            monkeypatch.setattr(manager, "_fail", must_recover_committed_task)
            await _queue(sessions)
            assert await manager.run_once()
            assert errors == ["NATIVE_INVALID_RECEIPT"]
            current = await _state(sessions)
            assert published == [current["current_snapshot"]]
            assert current["current_snapshot"] != previous["current_snapshot"]
            assert current["previous_snapshot"] == previous["current_snapshot"]
            assert current["published_version"] == current["source_version"]
            assert current["running_version"] is None and current["running_token"] is None
            assert current["requested"] is False and current["failures"] == 0 and current["error"] is None
            async with sessions() as session:
                row = (
                    (await session.execute(sa.select(snapshots).where(snapshots.c.id == published[0]))).mappings().one()
                )
                assert row["read_view_schema_version"] == 1

    asyncio.run(check())


def test_source_change_inside_writer_fails_final_cas_and_rolls_back_publication(native_binary, tmp_path, monkeypatch):
    async def check():
        async with _database(tmp_path, native_binary, monkeypatch) as (_, _, sessions, manager, _, _):
            await _queue(sessions)
            assert await manager.run_once()
            previous = await _state(sessions)
            counts = await _counts(sessions)
            async with sessions() as session:
                # The main snapshot INSERT runs after native staging. Perturb
                # the source inside that writer transaction, so the final
                # pointer CAS must reject every staged/result/cost insert.
                await session.execute(
                    sa.text("""
                    CREATE TRIGGER change_source_inside_publication AFTER INSERT ON performance_snapshot
                    BEGIN
                        UPDATE account_analysis SET source_version=source_version+1 WHERE account_id=NEW.account_id;
                    END
                """)
                )
                await session.commit()
            errors = []
            query = NativeAnalysis.query

            async def capture_error(self, command, **request):
                assert command == "publish_store"
                try:
                    return await query(self, command, **request)
                except NativeAnalysisError as error:
                    errors.append(error.code)
                    raise

            monkeypatch.setattr(NativeAnalysis, "query", capture_error)
            await _queue(sessions)
            queued = await _state(sessions)
            assert await manager.run_once()
            assert errors == ["CLAIM_LOST"]
            current = await _state(sessions)
            assert current["current_snapshot"] == previous["current_snapshot"]
            assert current["published_version"] == previous["published_version"]
            assert current["source_version"] == queued["source_version"]
            assert current["running_version"] is None and current["running_token"] is None
            assert current["requested"]
            assert await _counts(sessions) == counts

    asyncio.run(check())


def test_source_change_before_rust_snapshot_is_rejected(native_binary, tmp_path, monkeypatch):
    async def check():
        async with _database(tmp_path, native_binary, monkeypatch) as (_, _, sessions, manager, _, _):
            await _queue(sessions)
            assert await manager.run_once()
            previous = await _state(sessions)
            counts = await _counts(sessions)
            original = NativeAnalysis.query
            receipts = []

            async def change_source(self, command, **request):
                assert command == "publish_store"
                async with sessions() as session:
                    await session.execute(
                        states.update()
                        .where(states.c.account_id == 7)
                        .values(source_version=states.c.source_version + 1)
                    )
                    await session.commit()
                output = await original(self, command, **request)
                receipts.append(json.loads(output))
                return output

            monkeypatch.setattr(NativeAnalysis, "query", change_source)
            await _queue(sessions)
            assert await manager.run_once()
            assert len(receipts) == 1
            assert receipts[0]["published"] is False and receipts[0]["reason"] == "source_changed"
            current = await _state(sessions)
            assert current["current_snapshot"] == previous["current_snapshot"]
            assert current["published_version"] == previous["published_version"]
            assert current["source_version"] == receipts[0]["source_version"] + 1
            assert current["running_version"] is None and current["running_token"] is None
            assert current["requested"] and current["failures"] == 0
            assert await _counts(sessions) == counts

    asyncio.run(check())


def test_transaction_failure_after_cost_copy_rolls_back_every_published_row(native_binary, tmp_path, monkeypatch):
    async def check():
        async with _database(tmp_path, native_binary, monkeypatch) as (_, _, sessions, manager, _, _):
            await _queue(sessions)
            assert await manager.run_once()
            previous = await _state(sessions)
            counts = await _counts(sessions)
            errors = []
            query = NativeAnalysis.query
            async with sessions() as session:
                # This pointer UPDATE follows all source-cost and result INSERTs.
                # Distinct error messages prove we reached that copy boundary.
                await session.execute(
                    sa.text("""
                    CREATE TRIGGER fail_after_cost_copy BEFORE UPDATE OF current_snapshot ON account_analysis
                    WHEN NEW.current_snapshot != OLD.current_snapshot
                    BEGIN
                        SELECT CASE WHEN
                            (SELECT COUNT(*) FROM performance_cost_trade WHERE snapshot_id=NEW.current_snapshot)=0
                            THEN RAISE(ABORT, 'source cost facts were not copied') END;
                        SELECT RAISE(ABORT, 'synthetic failure after immutable costs were copied');
                    END
                """)
                )
                await session.commit()

            async def capture_error(self, command, **request):
                assert command == "publish_store"
                try:
                    return await query(self, command, **request)
                except NativeAnalysisError as error:
                    errors.append((error.code, str(error)))
                    raise

            monkeypatch.setattr(NativeAnalysis, "query", capture_error)
            await _queue(sessions)
            assert await manager.run_once()
            assert len(errors) == 1 and errors[0][0] == "PUBLICATION_WRITE"
            assert "synthetic failure after immutable costs were copied" in errors[0][1]
            current = await _state(sessions)
            assert current["current_snapshot"] == previous["current_snapshot"]
            assert current["published_version"] == previous["published_version"]
            assert current["running_version"] is None and current["running_token"] is None
            assert current["requested"]
            assert await _counts(sessions) == counts

    asyncio.run(check())


@pytest.mark.parametrize(
    "change",
    [
        {"projection_version": 0, "projection_error": "unsupported historical scalar"},
        {"account_id": 99},
        {"execution_id": "stale-execution"},
        {"created_at": "2025-01-01T00:00:00"},
        {"is_success": 0},
        {"trade_count": 999},
    ],
)
def test_native_rejects_unsupported_or_mismatched_source_headers(native_binary, tmp_path, monkeypatch, change):
    async def check():
        async with _database(tmp_path, native_binary, monkeypatch) as (path, _, sessions, _, _, settings):
            async with sessions() as session:
                await session.execute(
                    source_execution.update().where(source_execution.c.record_id == 1).values(**change)
                )
                await session.commit()
            native = NativeAnalysis(binary=native_binary, database=path)
            with pytest.raises(NativeAnalysisError) as error:
                await native.query(
                    "performance",
                    account_id=7,
                    settings=settings.model_dump(),
                    require_source_facts=True,
                    require_observation_facts=True,
                )
            assert error.value.code == "SOURCE_FACTS_INCOMPLETE"

    asyncio.run(check())
