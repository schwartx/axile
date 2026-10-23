"""Measure three real structured publications after source facts are frozen.

Synthetic database preparation runs in an unmeasured process. A fresh measured
Python AnalysisManager performs native JSONL staging, typed publication and SQL
INSERT SELECT cost copies. An external sampler includes Python and Rust RSS.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
PROCESS_TIMEOUT = 20


def emit(phase: str, **values) -> None:
    """Emit small timestamped phase markers for the external RSS sampler."""
    print(json.dumps({"phase": phase, "when": time.monotonic(), **values}), flush=True)


def migration(name: str):
    """Load repository-owned trigger definitions without importing test helpers."""
    path = ROOT / "axile/server/alembic/versions" / name
    spec = importlib.util.spec_from_file_location("benchmark_migration_" + name[:4], path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def record(index: int, symbols: int) -> SimpleNamespace:
    """Create complete deterministic quantity, price and actual trade evidence."""
    when = (datetime(2025, 1, 1, 9) + timedelta(hours=index)).isoformat()
    asset = 100_000.0 + index * 0.5
    results = {}
    targets = {}
    for number in range(symbols):
        symbol = f"S{number:02d}"
        price = 100.0 + number * 10 + (index % 97) * 0.125
        quantity = 1.0 + ((index // 12 + number) % 3)
        targets[symbol] = 0.5 / symbols
        results[symbol] = {
            "status": "SUCCEEDED",
            "first_tick": {"last_price": price, "bid_price": price, "ask_price": price},
            "sizing": {
                "sizing_mode": "weight",
                "status": "SIZED",
                "target_quantity": quantity,
                "equity": asset,
                "reference_price": price,
                "unit_notional": price,
                "unit_multiplier": 1.0,
            },
            "orders": [{"order_id": f"buy-{number}", "direction": "BUY", "filled_volume": quantity}],
            "trades": [
                {
                    "order_id": f"buy-{number}",
                    "trade_price": price + 0.01,
                    "trade_volume": quantity,
                    "trade_time": when,
                    "extra": {"commission": 0.01, "commission_asset": "CNY"},
                }
            ],
        }
    return SimpleNamespace(
        id=index + 1,
        account_id=1,
        execution_id=f"synthetic-{index + 1}",
        created_at=when,
        is_success=1,
        raw_input={"curr_target": targets},
        raw_result={
            "status": "SUCCEEDED",
            "execution_time": 0.01,
            "account_assets": {"total_asset": asset, "positions": []},
            "symbol_results": results,
        },
    )


def prepare(args) -> None:
    """Create canonical evidence on the actual Alembic head before measurement."""
    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from axile.server.business_facts.repository import store_execution_facts
    from axile.server.db.models import Account, ExecuteRecord

    engine = sa.create_engine(f"sqlite:///{args.prepare}")
    try:
        with engine.begin() as connection:
            for path in sorted((ROOT / "axile/server/alembic/versions").glob("*.py")):
                if not path.stem[:4].isdigit():
                    continue
                revision = migration(path.name)
                revision.op = Operations(MigrationContext.configure(connection))
                revision.upgrade()
    finally:
        engine.dispose()

    async def populate():
        engine = create_async_engine(f"sqlite+aiosqlite:///{args.prepare}")
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                session.add(Account(id=1,name="synthetic-analysis",market="期货",trade_channel="ctp",brokerage="synthetic"))
                await session.commit()
            for start in range(0,args.observations,100):
                async with factory() as session:
                    for index in range(start,min(start+100,args.observations)):
                        item = record(index,args.symbols)
                        head = ExecuteRecord(**{key:value for key,value in vars(item).items() if key not in {"raw_input","raw_result"}})
                        session.add(head)
                        await session.flush()
                        await store_execution_facts(session,head,raw_input=item.raw_input,raw_result=item.raw_result)
                    await session.commit()
        finally:
            await engine.dispose()
    asyncio.run(populate())
    emit("prepared",observations=args.observations,symbols=args.symbols)


def verify(database: Path, expected_records: int, expected_trades: int) -> dict:
    """Verify current publication, retention, copied facts and repeatable outputs."""
    from axile.server.analysis_cost_facts import COST_FACT_TABLES
    from axile.server.db.models.analysis_result import RESULT_TABLES

    digest = hashlib.sha256()
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        state = connection.execute("SELECT * FROM account_analysis WHERE account_id=1").fetchone()
        if state["error"] or state["source_version"] != state["published_version"] or state["running_version"]:
            raise AssertionError("The full refresh did not publish the claimed source version")
        identity = state["current_snapshot"]
        snapshot = connection.execute("SELECT * FROM performance_snapshot WHERE id=?", (identity,)).fetchone()
        if snapshot["result_schema_version"] != 2 or snapshot["ranges"] != "{}" or snapshot["settings"] != "{}":
            raise AssertionError("The worker published legacy JSON instead of typed result rows")
        counts = {}
        for table in (*RESULT_TABLES, *COST_FACT_TABLES):
            columns = [column.name for column in table.c if column.name != "snapshot_id"]
            order = [column.name for column in table.primary_key.columns if column.name != "snapshot_id"]
            statement = f"SELECT {','.join(columns)} FROM {table.name} WHERE snapshot_id=? ORDER BY {','.join(order)}"
            count = 0
            digest.update(table.name.encode())
            for row in connection.execute(statement, (identity,)):
                digest.update(json.dumps(tuple(row), ensure_ascii=False, separators=(",", ":")).encode())
                digest.update(b"\n")
                count += 1
            counts[table.name] = count
        if counts["performance_cost_execution"] != expected_records:
            raise AssertionError("Published execution facts are incomplete")
        if counts["performance_cost_trade"] != expected_trades or counts["performance_result_range"] != 3:
            raise AssertionError("Published trades or range results are incomplete")
        retained = connection.execute("SELECT count(*) FROM performance_snapshot").fetchone()[0]
        return {"sha256": digest.hexdigest(), "current_row_counts": counts, "retained_snapshots": retained}


async def worker(args) -> None:
    """Execute three actual native staging and atomic publication rounds."""
    import psutil
    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from axile.server import performance_analysis as analysis

    os.environ["AXILE_ANALYSIS_MODE"] = "required"
    os.environ["AXILE_ANALYSIS_BINARY"] = str(args.binary.resolve())
    emit("imported", python_rss_bytes=psutil.Process().memory_info().rss)
    engine = create_async_engine(f"sqlite+aiosqlite:///{args.worker}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    manager = analysis.AnalysisManager(sessions)
    statements = Counter()
    raw_selects = set()

    def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
        normalized = " ".join(statement.lower().split())
        statements["all"] += 1
        if normalized.startswith("select") and ("raw_input" in normalized or "raw_result" in normalized):
            statements["legacy_column_selects"] += 1
            raw_selects.add(normalized)
        if normalized.startswith("insert into performance_cost_") and " select " in normalized:
            statements["cost_insert_select"] += 1

    sa.event.listen(engine.sync_engine, "before_cursor_execute", capture)

    hashes = []
    try:
        for round_number in range(1, 4):
            emit("refresh_start", round=round_number)
            async with sessions() as session:
                await analysis.enqueue(session.get_bind().url.database, 1, force=True)
            if not await asyncio.wait_for(manager.run_once(), timeout=PROCESS_TIMEOUT):
                raise AssertionError("The queued refresh was not executed")
            emit("refresh_end", round=round_number, python_rss_bytes=psutil.Process().memory_info().rss)
            emit("verify_start", round=round_number)
            result = verify(args.worker, args.observations, args.observations * args.symbols)
            hashes.append(result["sha256"])
            if result["retained_snapshots"] != min(round_number, 2) or len(set(hashes)) != 1:
                raise AssertionError("Repeated refreshes changed results or failed snapshot retention")
            emit("verify_end", round=round_number, **result)
        emit(
            "done", sql_counts=dict(statements), legacy_column_queries=sorted(raw_selects), repeated_outputs_equal=True
        )
    finally:
        await engine.dispose()


def observe(args, directory: Path) -> dict:
    """Sample the measured Python process plus recursive Rust children every 5 ms."""
    import psutil

    environment = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONHASHSEED="0", OMP_NUM_THREADS="1")
    database = directory / "facts.sqlite"
    common = ["--observations", str(args.observations), "--symbols", str(args.symbols), "--binary", str(args.binary)]
    started = time.monotonic()
    prepared = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--prepare", str(database), *common],
        capture_output=True,
        env=environment,
        timeout=PROCESS_TIMEOUT,
        check=False,
    )
    if prepared.returncode:
        raise RuntimeError("Fixture preparation failed: " + prepared.stderr.decode()[-3000:])
    prepared_seconds = time.monotonic() - started
    before_bytes = database.stat().st_size
    samples = []
    log = directory / "worker.jsonl"
    errors = directory / "worker.stderr"
    measured_start = time.monotonic()
    with log.open("wb") as stdout, errors.open("wb") as stderr:
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--worker", str(database), *common],
            stdout=stdout,
            stderr=stderr,
            env=environment,
        )
        handle = psutil.Process(process.pid)
        try:
            while process.poll() is None:
                if time.monotonic() - measured_start > PROCESS_TIMEOUT:
                    raise TimeoutError(f"Measured worker exceeded {PROCESS_TIMEOUT} seconds")
                try:
                    parent_rss = handle.memory_info().rss
                    child_rss = 0
                    for child in handle.children(recursive=True):
                        try:
                            child_rss += child.memory_info().rss
                        except psutil.NoSuchProcess:
                            pass
                    samples.append((time.monotonic(), parent_rss, child_rss))
                except psutil.NoSuchProcess:
                    pass
                time.sleep(0.005)
        finally:
            if process.poll() is None:
                for child in handle.children(recursive=True):
                    try:
                        child.kill()
                    except psutil.NoSuchProcess:
                        pass
                process.kill()
                process.wait()
    if process.returncode:
        raise RuntimeError("Measured worker failed: " + errors.read_text()[-5000:])
    markers = [json.loads(line) for line in log.read_text().splitlines()]
    rounds = []
    for number in range(1, 4):
        first = next(row for row in markers if row["phase"] == "refresh_start" and row["round"] == number)
        last = next(row for row in markers if row["phase"] == "refresh_end" and row["round"] == number)
        selected = [sample for sample in samples if first["when"] <= sample[0] <= last["when"]]
        within = [row for row in markers if first["when"] <= row["when"] <= last["when"]]
        stages = {row["phase"]: row for row in within}
        verified = next(row for row in markers if row["phase"] == "verify_end" and row["round"] == number)
        rounds.append(
            {
                "round": number,
                "refresh_seconds": last["when"] - first["when"],
                "stage_seconds": stages["stage_end"]["when"] - stages["stage_start"]["when"],
                "publish_seconds": stages["publish_end"]["when"] - stages["publish_start"]["when"],
                "peak_combined_rss_bytes": max(parent + child for _, parent, child in selected),
                "peak_python_rss_bytes": max(parent for _, parent, _ in selected),
                "peak_children_rss_bytes": max(child for _, _, child in selected),
                "python_rss_after_refresh_bytes": last["python_rss_bytes"],
                "spool_bytes": stages["stage_end"]["spool_bytes"],
                "result_row_counts": stages["stage_end"]["result_row_counts"],
                "correctness": {key: value for key, value in verified.items() if key not in {"phase", "when", "round"}},
            }
        )
    return {
        "fixture": "synthetic_complete_source_facts_with_poisoned_legacy_json",
        "observations": args.observations,
        "symbols_per_observation": args.symbols,
        "source_trade_rows": args.observations * args.symbols,
        "fixture_preparation_seconds_unmeasured": prepared_seconds,
        "database_bytes_before": before_bytes,
        "database_bytes_after": database.stat().st_size,
        "binary_sha256": hashlib.sha256(args.binary.read_bytes()).hexdigest(),
        "python_import_rss_bytes": next(row["python_rss_bytes"] for row in markers if row["phase"] == "imported"),
        "overall_peak_combined_rss_bytes": max(parent + child for _, parent, child in samples),
        "sample_interval_seconds": 0.005,
        "memory_method": "Externally sampled sum of Python AnalysisManager and recursive Rust child RSS; shared pages may be counted twice",
        "rounds": rounds,
        "query_capture": next(row for row in markers if row["phase"] == "done"),
        "unmeasured": [
            "source ingestion/backfill",
            "full API server",
            "API tail latency",
            "concurrent source writes",
            "production history",
        ],
    }


def main() -> None:
    """Run the bounded whole-publication measurement against temporary data only."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=ROOT / "rust/analysis/target/release/axile-analysis")
    parser.add_argument("--observations", type=int, default=10_000)
    parser.add_argument("--symbols", type=int, default=10)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--prepare", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 1 <= args.observations <= 10_000 or not 1 <= args.symbols <= 10:
        parser.error("Use 1 to 10000 observations and 1 to 10 symbols")
    if args.prepare:
        prepare(args)
        return
    if args.worker:
        asyncio.run(worker(args))
        return
    if not args.binary.is_file() or not os.access(args.binary, os.X_OK):
        parser.error("Build the release binary first or pass --binary")
    with tempfile.TemporaryDirectory(prefix="axile-publication-measurement-") as temporary:
        report = observe(args, Path(temporary))
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded)
    print(encoded, end="")


if __name__ == "__main__":
    main()
