"""Compare full app imports, real analysis worker and ASGI APIs in fresh processes.

Uses sealed local databases and disables trading/scheduler lifespan equally.
Each process gets its own database; no original file or live service is changed.
"""

import argparse
import asyncio
import hashlib
import json
import os
import resource
import shutil
import socket
import sqlite3
import statistics
import subprocess
import sys
import time
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch


def emit(name, **values):
    """Emit bounded phase markers for the external sampler."""
    print(json.dumps(dict(phase=name, when=time.monotonic(), **values)), flush=True)


async def worker(args):
    """Run actual worker and API handlers in the selected source tree."""
    import httpx
    import psutil

    from axile.server import performance_analysis as service
    from axile.server.app import _app
    from axile.server.core.db import SessionLocal, engine

    if not Path(service.__file__).resolve().is_relative_to(args.root.resolve()):
        raise ValueError("Wrong source tree")
    manager = service.AnalysisManager(SessionLocal)
    _app.state.analysis_manager = manager
    original_load, original_compute, original_publish = manager._load, service.compute_batch, manager._publish

    async def load(*values):
        emit("load_start")
        result = await original_load(*values)
        emit("load_end")
        return result

    def compute(*values):
        emit("compute_start")
        result = original_compute(*values)
        emit("compute_end")
        return result

    async def publish(*values):
        emit("publish_start")
        result = await original_publish(*values)
        emit("publish_end")
        return result

    manager._load, service.compute_batch, manager._publish = load, compute, publish
    emit("imported", source=str(Path(service.__file__).resolve()))
    try:
        emit("worker_start")
        async with SessionLocal() as session:
            await service.enqueue(session, 2, True)
        assert await manager.run_once()
        emit("worker_end")
        hashes = []
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app), base_url="http://127.0.0.1") as client:
            emit("api_start")
            identity = None
            for suffix in (
                "snapshot?range=all",
                "snapshot?range=30",
                "snapshot?range=90",
                "?range=all&include_backtest=true",
                "costs",
            ):
                path = "/api/v1/account/performance/2" + (suffix if suffix.startswith("?") else "/" + suffix)
                if suffix == "costs":
                    path += "?snapshot_id=" + identity + "&dimension=summary"
                response = await client.get(path)
                if response.status_code != 200:
                    raise ValueError("API failed: " + suffix)
                payload = response.json()
                if "snapshot_id" in payload:
                    if "status" in payload and payload["status"] != "ready":
                        raise ValueError("Snapshot not ready")
                    identity = payload["snapshot_id"]
                payload = {key: value for key, value in payload.items() if key not in ("snapshot_id", "computed_at")}
                body = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
                hashes.append(hashlib.sha256(body).hexdigest())
            emit("api_end")
        samples = []
        for _ in range(10):
            await asyncio.sleep(0.1)
            samples.append(psutil.Process().memory_info().rss)
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == "darwin" else 1024)
        emit(
            "done",
            peak_bytes=peak,
            steady_bytes=statistics.median(samples),
            hashes=hashes,
            duckdb_imported="duckdb" in sys.modules,
            arrow_imported="pyarrow" in sys.modules,
        )
    finally:
        await engine.dispose()


def observe(args, root, directory, *, worker_script=None, phase_names=("load", "compute", "publish", "worker", "api")):
    """Sample total process RSS while retaining private diagnostic output."""
    import psutil

    directory.mkdir(mode=0o700)
    shutil.copy2(args.database, directory / "working.db")
    (directory / "working.db").chmod(0o600)
    config = directory / "config.toml"
    config.write_text(
        f'sqlalchemy_database_uri = "sqlite+aiosqlite:///{directory}/working.db"\napp_log_dir = "{directory}/logs"\nenvironment = "local"\n'
    )
    config.chmod(0o600)
    environment = dict(
        os.environ,
        AXILE_CONFIG_TOML=str(config),
        PYTHONPATH=str(root),
        PYTHONHASHSEED="0",
        OMP_NUM_THREADS="1",
        OPENBLAS_NUM_THREADS="1",
    )
    samples = []
    with (directory / "process.log").open("w") as stream:
        started = time.monotonic()
        process = subprocess.Popen(
            [
                sys.executable,
                str(worker_script or Path(__file__).resolve()),
                str(root),
                str(args.database),
                str(directory),
                "--worker",
            ],
            env=environment,
            stdout=stream,
            stderr=subprocess.STDOUT,
        )
        handle = psutil.Process(process.pid)
        while process.poll() is None:
            if time.monotonic() - started > 60:
                process.kill()
                process.wait()
                raise TimeoutError(str(directory))
            try:
                samples.append((time.monotonic(), handle.memory_info().rss))
            except psutil.NoSuchProcess:
                pass
            time.sleep(0.005)
        if process.returncode:
            raise RuntimeError("Inspect private log: " + str(directory / "process.log"))
    phases = {}
    for line in (directory / "process.log").read_text().splitlines():
        if line.startswith('{"phase":'):
            row = json.loads(line)
            phases[row["phase"]] = row
    for name in phase_names:
        start, finish = phases[name + "_start"]["when"], phases[name + "_end"]["when"]
        phases[name + "_end"].update(
            seconds=finish - start, peak_bytes=max((rss for when, rss in samples if start <= when <= finish), default=0)
        )
    return phases


def summarize(rows):
    """Summarize alternating process runs using medians."""
    return dict(
        peak_mib=statistics.median(row["done"]["peak_bytes"] / 2**20 for row in rows),
        steady_mib=statistics.median(row["done"]["steady_bytes"] / 2**20 for row in rows),
        worker_peak_mib=statistics.median(row["worker_end"]["peak_bytes"] / 2**20 for row in rows),
        worker_seconds=statistics.median(row["worker_end"]["seconds"] for row in rows),
        compute_seconds=statistics.median(row["compute_end"]["seconds"] for row in rows),
        api_seconds=statistics.median(row["api_end"]["seconds"] for row in rows),
    )


def require_legacy_fixture(source: Path) -> None:
    """Reject typed databases before a historical JSON fixture creates any output."""
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(executerecord)")}
    if not {"raw_input", "raw_result"} <= columns:
        raise ValueError("Legacy synthetic fixtures require an Axile <=0036 baseline checkout and database")


def synthetic_source(source, output, count, symbols):
    """Build an explicitly synthetic multi-symbol load, preserving the original."""
    require_legacy_fixture(source)
    with (
        closing(sqlite3.connect(source.as_uri() + "?mode=ro&immutable=1", uri=True)) as original,
        closing(sqlite3.connect(output)) as writer,
    ):
        original.backup(writer)
        writer.execute("DELETE FROM executerecord WHERE account_id=2")
        start = datetime(2026, 1, 1)
        target = {f"S{i}": 1.0 / symbols for i in range(symbols)}
        for index in range(count):
            results = {
                symbol: {
                    "first_tick": {"bid_price": 100 + i + index * 0.001, "ask_price": 100 + i + index * 0.001},
                    "sizing": {"unit_multiplier": 10},
                }
                for i, symbol in enumerate(target)
            }
            writer.execute(
                "INSERT INTO executerecord(account_id,execution_id,created_at,is_success,raw_input,raw_result) VALUES(?,?,?,?,?,?)",
                (
                    2,
                    "synthetic-" + str(index),
                    (start + timedelta(hours=index)).isoformat(),
                    1,
                    json.dumps({"curr_target": target}),
                    json.dumps(
                        {
                            "status": "SUCCESS",
                            "account_assets": {"total_asset": 100000.123456789 + index},
                            "symbol_results": results,
                        }
                    ),
                ),
            )
        writer.commit()
    output.chmod(0o600)


def main():
    """Compare immutable source copies without starting trading services."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("database", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--synthetic-records", type=int, default=0)
    parser.add_argument("--symbols", type=int, default=20)
    args = parser.parse_args()
    if args.worker:
        with patch.object(socket.socket, "connect", side_effect=AssertionError("Outbound connections forbidden")):
            asyncio.run(worker(args))
        return
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    original_hash = hashlib.sha256(args.database.read_bytes()).hexdigest()
    original_path = args.database
    if args.synthetic_records:
        synthetic_source(args.database, args.output / "synthetic.db", args.synthetic_records, args.symbols)
        args.database = args.output / "synthetic.db"
    results = []
    for repeat in range(args.runs):
        for arm in ("baseline", "candidate") if repeat % 2 == 0 else ("candidate", "baseline"):
            root = args.baseline if arm == "baseline" else args.root
            phases = observe(args, root, args.output / f"{repeat}-{arm}")
            results.append(dict(arm=arm, repeat=repeat, phases=phases))
            print(
                json.dumps(
                    dict(
                        arm=arm,
                        repeat=repeat,
                        worker_seconds=phases["worker_end"]["seconds"],
                        compute_seconds=phases["compute_end"]["seconds"],
                        peak_mib=phases["done"]["peak_bytes"] / 2**20,
                    )
                ),
                flush=True,
            )
            (args.output / "results.json").write_text(json.dumps(results, indent=2))
    hashes = results[0]["phases"]["done"]["hashes"]
    if any(row["phases"]["done"]["hashes"] != hashes for row in results):
        raise ValueError("Business API results differ: stop acceptance")
    if hashlib.sha256(original_path.read_bytes()).hexdigest() != original_hash:
        raise ValueError("Original database changed")
    summary = {
        arm: summarize([row["phases"] for row in results if row["arm"] == arm]) for arm in ("baseline", "candidate")
    }
    summary.update(
        source_sha256=original_hash,
        synthetic_records=args.synthetic_records,
        synthetic_symbols=args.symbols if args.synthetic_records else None,
        api_results_equal=True,
        runs=args.runs,
        scope="Fresh full app imports, real worker and ASGI APIs; trading/scheduler lifespan disabled equally; same interpreter, warm OS cache, alternating runs, no forced GC",
    )
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
