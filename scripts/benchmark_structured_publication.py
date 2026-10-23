"""Measure actual in-process native publications after source facts are frozen.

Synthetic database preparation runs in an unmeasured process. A fresh measured
Python AnalysisManager invokes the checkout's loaded extension. An external
sampler includes the Python process and any descendants.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

try:
    from benchmark_business_fact_storage import (
        harness_manifest,
        initialize_benchmark_database,
        native_artifact,
        no_network,
        prepare_database,
        source_hash,
        sqlite_settings,
    )
except ModuleNotFoundError:
    from scripts.benchmark_business_fact_storage import (
        harness_manifest,
        initialize_benchmark_database,
        native_artifact,
        no_network,
        prepare_database,
        source_hash,
        sqlite_settings,
    )

ROOT = Path(__file__).resolve().parents[1]
PROCESS_TIMEOUT = 20


def emit(phase: str, **values) -> None:
    """Emit small timestamped phase markers for the external RSS sampler."""
    print(json.dumps({"phase": phase, "when": time.monotonic(), **values}), flush=True)


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
    from axile.server._native import _core

    if callable(getattr(_core, "migration_upgrade", None)):
        from axile.executor.algorithms.utils.clock import get_default_clock
        from axile.server.execution.records import _persist_execute_record

        prepare_database(args.prepare, ROOT)

        async def populate_native():
            for index in range(args.observations):
                item = record(index, args.symbols)
                with patch.object(
                    get_default_clock(), "time", return_value=datetime.fromisoformat(item.created_at).timestamp()
                ):
                    await _persist_execute_record(
                        account_id=1,
                        execution_id=item.execution_id,
                        raw_input=item.raw_input,
                        raw_result=item.raw_result,
                        is_success=item.is_success,
                    )

        asyncio.run(populate_native())
        emit("prepared", observations=args.observations, symbols=args.symbols)
        return

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from axile.server.business_facts.repository import store_execution_facts
    from axile.server.db.models import ExecuteRecord

    prepare_database(args.prepare, ROOT)

    async def populate():
        engine = create_async_engine(f"sqlite+aiosqlite:///{args.prepare}")
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            for start in range(0, args.observations, 100):
                async with factory() as session:
                    for index in range(start, min(start + 100, args.observations)):
                        item = record(index, args.symbols)
                        head = ExecuteRecord(
                            **{
                                key: value
                                for key, value in vars(item).items()
                                if key not in {"raw_input", "raw_result"}
                            }
                        )
                        session.add(head)
                        await session.flush()
                        await store_execution_facts(session, head, raw_input=item.raw_input, raw_result=item.raw_result)
                    await session.commit()
        finally:
            await engine.dispose()

    asyncio.run(populate())
    emit("prepared", observations=args.observations, symbols=args.symbols)


def verify(database: Path, expected_records: int, expected_trades: int) -> dict:
    """Verify current publication, retention, copied facts and repeatable outputs."""
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
        tables = [
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name")
            if row[0].startswith(("performance_result_", "performance_cost_"))
        ]
        for table in tables:
            info = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
            columns = sorted(column[1] for column in info if column[1] != "snapshot_id")
            order = [
                column[1] for column in sorted(info, key=lambda row: row[5]) if column[5] and column[1] != "snapshot_id"
            ]
            statement = (
                f"SELECT {','.join(columns)} FROM {table} WHERE snapshot_id=? ORDER BY {','.join(order or columns)}"
            )
            count = 0
            digest.update(table.encode())
            for row in connection.execute(statement, (identity,)):
                digest.update(json.dumps(tuple(row), ensure_ascii=False, separators=(",", ":")).encode())
                digest.update(b"\n")
                count += 1
            counts[table] = count
        if counts["performance_cost_execution"] != expected_records:
            raise AssertionError("Published execution facts are incomplete")
        if counts["performance_cost_trade"] != expected_trades or counts["performance_result_range"] != 3:
            raise AssertionError("Published trades or range results are incomplete")
        retained = connection.execute("SELECT count(*) FROM performance_snapshot").fetchone()[0]
        return {"sha256": digest.hexdigest(), "current_row_counts": counts, "retained_snapshots": retained}


async def worker(args) -> None:
    """Execute native publications without obsolete Python staging markers."""
    import psutil

    initialize_benchmark_database(args.worker)
    from axile.server import performance_analysis as analysis
    from axile.server.core import db

    emit(
        "imported",
        python_rss_bytes=psutil.Process().memory_info().rss if args.mode == "rss" else None,
        native_artifact=native_artifact(ROOT),
    )
    engine = None
    if hasattr(db, "SessionLocal"):
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        engine = create_async_engine(f"sqlite+aiosqlite:///{args.worker}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        manager = analysis.AnalysisManager(sessions)
    else:
        manager = analysis.AnalysisManager()

    hashes = []
    try:
        for round_number in range(1, args.rounds + 1):
            emit("refresh_start", round=round_number)
            await analysis.enqueue(str(args.worker), 1, force=True)
            if not await asyncio.wait_for(manager.run_once(), timeout=PROCESS_TIMEOUT):
                raise AssertionError("The queued refresh was not executed")
            emit(
                "refresh_end",
                round=round_number,
                python_rss_bytes=psutil.Process().memory_info().rss if args.mode == "rss" else None,
            )
            emit("verify_start", round=round_number)
            result = verify(args.worker, args.observations, args.observations * args.symbols)
            hashes.append(result["sha256"])
            if result["retained_snapshots"] != min(round_number, 2) or len(set(hashes)) != 1:
                raise AssertionError("Repeated refreshes changed results or failed snapshot retention")
            emit("verify_end", round=round_number, **result)
        emit(
            "done",
            repeated_outputs_equal=True,
            sql_counts=None,
            sql_count_scope="SQLAlchemy events cannot observe native SQLite statements; no SQL-count claim",
        )
    finally:
        if engine is not None:
            await engine.dispose()


def observe(args, directory: Path) -> dict:
    """Sample the measured Python process plus recursive Rust children every 5 ms."""
    import psutil

    database = directory / "facts.sqlite"
    settings = directory / "config.toml"
    settings.write_text(
        f'sqlalchemy_database_uri = "sqlite+aiosqlite:///{database}"\napp_log_dir = "{directory}/logs"\nenvironment = "local"\n'
    )
    environment = {key: value for key, value in os.environ.items() if not key.startswith(("AXILE_", "PYTHONPATH"))}
    environment.update(
        PYTHONPATH=str(ROOT),
        PYTHONHASHSEED="0",
        OMP_NUM_THREADS="1",
        OPENBLAS_NUM_THREADS="1",
        AXILE_CONFIG_TOML=str(settings),
    )
    common = [
        "--checkout",
        str(ROOT),
        "--observations",
        str(args.observations),
        "--symbols",
        str(args.symbols),
        "--rounds",
        str(args.rounds),
        "--mode",
        args.mode,
    ]
    frozen_source = source_hash(ROOT)
    harness_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    harness_files = harness_manifest(Path(__file__))
    started = time.monotonic()
    prepared = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--prepare", str(database), *common],
        capture_output=True,
        env=environment,
        timeout=PROCESS_TIMEOUT,
        check=False,
        cwd=directory,
    )
    if prepared.returncode:
        raise RuntimeError("Fixture preparation failed: " + prepared.stderr.decode()[-3000:])
    prepared_seconds = time.monotonic() - started
    before_bytes = database.stat().st_size
    before_hash = hashlib.sha256(database.read_bytes()).hexdigest()
    with sqlite3.connect(database) as monitor:
        observed_settings = sqlite_settings(monitor)
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
            cwd=directory,
            start_new_session=True,
        )
        handle = psutil.Process(process.pid)
        try:
            while process.poll() is None:
                if time.monotonic() - measured_start > PROCESS_TIMEOUT:
                    raise TimeoutError(f"Measured worker exceeded {PROCESS_TIMEOUT} seconds")
                try:
                    if args.mode != "rss":
                        time.sleep(0.005)
                        continue
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
    for number in range(1, args.rounds + 1):
        first = next(row for row in markers if row["phase"] == "refresh_start" and row["round"] == number)
        last = next(row for row in markers if row["phase"] == "refresh_end" and row["round"] == number)
        selected = [sample for sample in samples if first["when"] <= sample[0] <= last["when"]]
        verified = next(row for row in markers if row["phase"] == "verify_end" and row["round"] == number)
        rounds.append(
            {
                "round": number,
                "refresh_seconds": last["when"] - first["when"] if args.mode == "timing" else None,
                "peak_combined_rss_bytes": max((parent + child for _, parent, child in selected), default=None),
                "peak_python_rss_bytes": max((parent for _, parent, _ in selected), default=None),
                "peak_children_rss_bytes": max((child for _, _, child in selected), default=None),
                "python_rss_after_refresh_bytes": last["python_rss_bytes"],
                "correctness": {key: value for key, value in verified.items() if key not in {"phase", "when", "round"}},
            }
        )
    if source_hash(ROOT) != frozen_source or harness_manifest(Path(__file__)) != harness_files:
        raise AssertionError("Sources or benchmark harness changed during publication measurement")
    fixture_digest = hashlib.sha256()
    for index in range(args.observations):
        fixture_digest.update(
            json.dumps(vars(record(index, args.symbols)), sort_keys=True, separators=(",", ":")).encode()
        )
    return {
        "fixture": "synthetic_complete_source_facts",
        "fixture_sha256": fixture_digest.hexdigest(),
        "source_tree_sha256": frozen_source,
        "harness_sha256": harness_hash,
        "harness_files": harness_files,
        "checkout": str(ROOT),
        "measurement_mode": args.mode,
        "observations": args.observations,
        "symbols_per_observation": args.symbols,
        "source_trade_rows": args.observations * args.symbols,
        "fixture_preparation_seconds_unmeasured": prepared_seconds,
        "database_bytes_before": before_bytes,
        "database_bytes_after": database.stat().st_size,
        "native_artifact": next(row["native_artifact"] for row in markers if row["phase"] == "imported"),
        "sqlite_settings": {"monitor_connection": observed_settings, "native_connections": None},
        "source_database_sha256_before_publication": before_hash,
        "python_import_rss_bytes": next(row["python_rss_bytes"] for row in markers if row["phase"] == "imported"),
        "overall_peak_combined_rss_bytes": max((parent + child for _, parent, child in samples), default=None),
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
    global ROOT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, default=ROOT)
    parser.add_argument("--observations", type=int, default=100)
    parser.add_argument("--symbols", type=int, default=10)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--work-directory", type=Path)
    parser.add_argument("--mode", choices=("timing", "rss"), default="timing")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--prepare", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    ROOT = args.checkout.resolve()
    sys.path.insert(0, str(ROOT))
    if not 1 <= args.observations <= 10_000 or not 1 <= args.symbols <= 10:
        parser.error("Use 1 to 10000 observations and 1 to 10 symbols")
    if not 1 <= args.rounds <= 3:
        parser.error("Use 1..3 publication rounds")
    if args.prepare:
        with no_network():
            prepare(args)
        return
    if args.worker:
        with no_network():
            asyncio.run(worker(args))
        return
    if args.work_directory:
        args.work_directory = args.work_directory.resolve()
        args.work_directory.mkdir(mode=0o700, parents=True, exist_ok=False)
        report = observe(args, args.work_directory)
    else:
        with tempfile.TemporaryDirectory(
            prefix="axile-publication-measurement-", dir=args.output.parent if args.output else Path.cwd()
        ) as temporary:
            report = observe(args, Path(temporary))
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded)
    print(encoded, end="")


if __name__ == "__main__":
    main()
