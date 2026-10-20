"""Attribute full analysis/API RSS to lifecycle stages on sealed local copies."""

import argparse
import asyncio
import hashlib
import json
import os
import socket
import sqlite3
import statistics
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from scripts import benchmark_performance_frames as benchmark


async def worker(args):
    """Wrap the existing real worker with RSS markers and optional allocation tracing."""
    import psutil

    original_emit = benchmark.emit
    process = psutil.Process()
    profiling = os.environ.get("AXILE_ANALYSIS_TRACEMALLOC") == "1"
    if profiling:
        import tracemalloc

    def mark(name, **values):
        if profiling and name == "imported":
            tracemalloc.start(3)
        values["rss_bytes"] = process.memory_info().rss
        if profiling and tracemalloc.is_tracing():
            values["traced_bytes"], values["traced_peak_bytes"] = tracemalloc.get_traced_memory()
            if name in ("load_end", "compute_end", "publish_end", "worker_end", "api_end", "done"):
                values["top_allocations"] = [
                    dict(location=str(item.traceback), bytes=item.size, count=item.count)
                    for item in tracemalloc.take_snapshot().statistics("lineno")[:12]
                ]
        if os.environ.get("AXILE_INSPECT_COMPILED_CACHE") == "1" and name == "worker_end":
            from axile.server.core.db import engine

            retained = []
            seen = set()
            for compiled in engine.sync_engine._compiled_cache.values():
                for bind in getattr(compiled, "binds", {}).values():
                    value = bind.value
                    if isinstance(value, dict) and "all" in value and id(value) not in seen:
                        seen.add(id(value))
                        retained.append(dict(parameter=bind._orig_key, ranges=list(value), sql=str(compiled)[:90]))
            values["compiled_retained_ranges"] = retained
        original_emit(name, **values)

    benchmark.emit = mark
    mark("boot")
    await benchmark.worker(args)


def stored_hashes(path):
    """Hash actual stored values, excluding only generated snapshot ID and wall time."""
    source = "(SELECT current_snapshot FROM account_analysis WHERE account_id=2)"
    queries = {
        "snapshot": "SELECT account_id,settings,ranges,source_version,logic_version,engine_version,data_until FROM performance_snapshot WHERE id="
        + source,
        "executions": "SELECT record_id,time,success,noop,payload FROM performance_cost_execution WHERE snapshot_id="
        + source
        + " ORDER BY record_id",
        "trades": "SELECT id,record_id,symbol,time,day,value,cost,loss_bp,fee,fee_currency,estimated,payload FROM performance_cost_trade WHERE snapshot_id="
        + source
        + " ORDER BY id",
    }
    result = {}
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as database:
        for name, query in queries.items():
            digest, count = hashlib.sha256(), 0
            for row in database.execute(query):
                digest.update(json.dumps(row, ensure_ascii=False, allow_nan=False).encode())
                digest.update(b"\n")
                count += 1
            result[name] = dict(rows=count, sha256=digest.hexdigest())
    return result


def summarize(rows):
    """Include application import and the complete worker-to-API interval."""
    result = benchmark.summarize(rows)
    for label, start, end in (
        ("chain_seconds", "worker_start", "api_end"),
        ("import_seconds", "boot", "imported"),
        ("import_to_api_seconds", "boot", "api_end"),
    ):
        result[label] = statistics.median(row[end]["when"] - row[start]["when"] for row in rows)
    return result


def main(*, worker_script=None, transport="asgi-in-process-client"):
    """Compare fresh baseline/candidate processes and preserve per-stage evidence."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("database", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--synthetic-records", type=int, default=0)
    parser.add_argument("--symbols", type=int, default=20)
    parser.add_argument(
        "--data-kind", choices=("real", "chen-repeated", "synthetic", "unspecified"), default="unspecified"
    )
    args = parser.parse_args()
    if args.worker:
        with patch.object(socket.socket, "connect", side_effect=AssertionError("Outbound connections forbidden")):
            awaitable = worker(args)
            asyncio.run(awaitable)
        return
    if not args.baseline or args.runs < 1:
        parser.error("--baseline and at least one run required")
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    original_path = args.database
    original_digest = hashlib.sha256(original_path.read_bytes()).hexdigest()
    if args.synthetic_records:
        benchmark.synthetic_source(args.database, args.output / "synthetic.db", args.synthetic_records, args.symbols)
        args.database = args.output / "synthetic.db"
    digest = hashlib.sha256(args.database.read_bytes()).hexdigest()
    results = []
    for repeat in range(args.runs):
        for arm in ("baseline", "candidate") if repeat % 2 == 0 else ("candidate", "baseline"):
            phases = benchmark.observe(
                args,
                args.baseline if arm == "baseline" else args.root,
                args.output / f"{repeat}-{arm}",
                worker_script=worker_script or Path(__file__).resolve(),
            )
            results.append(
                dict(
                    arm=arm,
                    repeat=repeat,
                    phases=phases,
                    stored=stored_hashes(args.output / f"{repeat}-{arm}" / "working.db"),
                )
            )
            print(
                json.dumps(
                    dict(
                        arm=arm,
                        phases={
                            name: dict(
                                rss_mib=row.get("rss_bytes", 0) / 2**20,
                                seconds=row.get("seconds"),
                                peak_mib=row.get("peak_bytes", 0) / 2**20,
                            )
                            for name, row in phases.items()
                            if name
                            in ("imported", "load_end", "compute_end", "publish_end", "worker_end", "api_end", "done")
                        },
                    )
                ),
                flush=True,
            )
            (args.output / "results.json").write_text(json.dumps(results, indent=2))
    expected = results[0]["phases"]["done"]["hashes"]
    if any(row["stored"] != results[0]["stored"] for row in results):
        raise ValueError("Stored snapshot or execution/trade payloads differ")
    if any(row["phases"]["done"]["hashes"] != expected for row in results):
        raise ValueError("Business API differs")
    if hashlib.sha256(args.database.read_bytes()).hexdigest() != digest:
        raise ValueError("Source changed")
    if hashlib.sha256(original_path.read_bytes()).hexdigest() != original_digest:
        raise ValueError("Original source changed")
    summary = {
        arm: summarize([row["phases"] for row in results if row["arm"] == arm]) for arm in ("baseline", "candidate")
    }
    summary["stages"] = {
        arm: {
            name: statistics.median(row["phases"][name]["rss_bytes"] / 2**20 for row in results if row["arm"] == arm)
            for name in ("imported", "load_end", "compute_end", "publish_end", "worker_end", "api_end", "done")
        }
        for arm in ("baseline", "candidate")
    }
    summary.update(
        source_sha256=digest,
        original_sha256=original_digest,
        data_kind="synthetic" if args.synthetic_records else args.data_kind,
        transport=transport,
        synthetic_records=args.synthetic_records,
        synthetic_symbols=args.symbols if args.synthetic_records else None,
        api_results_equal=True,
        stored_results_equal=True,
        runs=args.runs,
        profiling=os.environ.get("AXILE_ANALYSIS_TRACEMALLOC") == "1",
    )
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
