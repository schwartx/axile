"""Compare complete journal HTTP pagination, decoding and whole-process RSS."""

import argparse
import asyncio
import hashlib
import json
import resource
import socket
import sqlite3
import statistics
import sys
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

try:
    from benchmark_business_fact_storage import initialize_benchmark_database
    from benchmark_performance_frames import emit, observe, require_legacy_fixture
except ModuleNotFoundError:
    from scripts.benchmark_business_fact_storage import initialize_benchmark_database
    from scripts.benchmark_performance_frames import emit, observe, require_legacy_fixture


def scale_copy(source, target, count):
    """Expand intact account-2 payloads; label this as synthetic, never real history.

    Preserve every original row and other table. Clone both JSON columns verbatim,
    changing only identity/time, and add a calendar skip every fifth new execution
    with identical time and ID to exercise stable cross-table ties.
    """
    require_legacy_fixture(source)
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as original:
        with closing(sqlite3.connect(target)) as database, database:
            target.chmod(0o600)
            original.backup(database)
            rows = database.execute(
                "SELECT execution_id,raw_input,raw_result,is_success FROM executerecord WHERE account_id=2 ORDER BY id"
            ).fetchall()
            if not rows or count < len(rows):
                raise ValueError("Scale requires intact account-2 executions and a larger target count")
            last_id = database.execute("SELECT max(id) FROM executerecord").fetchone()[0]
            for index in range(count - len(rows)):
                _, raw_input, raw_result, success = rows[index % len(rows)]
                identity = last_id + index + 1
                when = (datetime(2025, 1, 1) + timedelta(hours=index)).isoformat()
                database.execute(
                    "INSERT INTO executerecord (id,account_id,execution_id,created_at,raw_input,raw_result,is_success) "
                    "VALUES (?,2,?,?,?,?,?)",
                    (identity, f"journal-scale-{identity}", when, raw_input, raw_result, success),
                )
                if index % 5 == 0:
                    database.execute(
                        "INSERT INTO schedule_skip "
                        "(id,account_id,channel,triggered_at,calendar_id,calendar_day,calendar_label,reason_code) "
                        "VALUES (?,2,'ctp',?,'synthetic',?,'合成休市','CALENDAR.CLOSED')",
                        (identity, when, when[:10]),
                    )


async def pages(client, results):
    """Load every page as loadJournal does; also exercise a filtered window."""
    path = "/api/v1/account/2/activity"
    for filters in ({}, {"since": "2025-01-02", "until": "2026-01-01"}):
        offset, count, seen = 0, None, set()
        while True:
            query = {"skip": offset, "limit": 500} | filters
            response = await client.get(path, params=query)
            if response.status_code != 200:
                raise ValueError("Unexpected HTTP status: " + str(response.status_code))
            results.append(
                dict(
                    query=query,
                    status=response.status_code,
                    bytes=len(response.content),
                    sha256=hashlib.sha256(response.content).hexdigest(),
                )
            )
            payload = response.json()
            if count is None:
                count = payload["count"]
            if count != payload["count"]:
                raise ValueError("Journal total changed")
            for item in payload["data"]:
                identity = (item["kind"], item["record"]["id"] if item["kind"] == "execution" else item["id"])
                if identity in seen:
                    raise ValueError("Repeated journal row")
                seen.add(identity)
            offset += len(payload["data"])
            if offset >= count:
                break
            if not payload["data"]:
                raise ValueError("Missing journal rows")
        if len(seen) != count:
            raise ValueError("Incomplete journal window")


async def worker(args):
    """Import the complete app and invoke actual journal HTTP handlers, without trading."""
    import httpx
    import psutil

    from axile.server.core import db

    initialize_benchmark_database(args.output / "working.db")
    from axile.server.api.routes import account_schedule
    from axile.server.app import _app

    engine = getattr(db, "engine", None)

    if not Path(account_schedule.__file__).resolve().is_relative_to(args.root.resolve()):
        raise ValueError("Wrong imported source tree")
    decoded = []

    def deserialize(raw):
        decoded.append(len(raw.encode("utf-8") if isinstance(raw, str) else raw))
        return json.loads(raw)

    # Only legacy SQLAlchemy decoding is observable through this hook.
    if engine is not None:
        engine.sync_engine.dialect._json_deserializer = deserialize
    try:
        results = []
        emit("imported", source=str(Path(account_schedule.__file__).resolve()))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app), base_url="http://127.0.0.1") as client:
            emit("api_start")
            await pages(client, results)
            emit("api_end")
        steady = []
        for _ in range(10):
            await asyncio.sleep(0.1)
            steady.append(psutil.Process().memory_info().rss)
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == "darwin" else 1024)
        emit(
            "done",
            peak_bytes=peak,
            steady_bytes=statistics.median(steady),
            results=results,
            decoded_bytes=sum(decoded) if engine is not None else None,
            largest_decoded_bytes=max(decoded, default=0) if engine is not None else None,
            decoded_scope="SQLAlchemy DB JSON deserializer only; native decoding is unobserved",
        )
    finally:
        if engine is not None:
            await engine.dispose()


def main():
    """Alternate fresh original/candidate processes and compare complete responses."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("database", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--scale-records", type=int, help="Synthetic account-2 size; never changes the source database")
    parser.add_argument("--worker", action="store_true")
    args = parser.parse_args()
    if args.worker:
        with patch.object(socket.socket, "connect", side_effect=AssertionError("Outbound connections forbidden")):
            asyncio.run(worker(args))
        return
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    if args.baseline is None or args.runs < 1:
        parser.error("--baseline and at least one run are required")
    original_digest = hashlib.sha256(args.database.read_bytes()).hexdigest()
    source = args.database
    if args.scale_records is not None:
        scale_copy(source, args.output / "synthetic.db", args.scale_records)
        args.database = args.output / "synthetic.db"
    digest = hashlib.sha256(args.database.read_bytes()).hexdigest()
    results = []
    for repeat in range(args.runs):
        for arm in ("baseline", "candidate") if repeat % 2 == 0 else ("candidate", "baseline"):
            phases = observe(
                args,
                args.baseline if arm == "baseline" else args.root,
                args.output / f"{repeat}-{arm}",
                worker_script=Path(__file__).resolve(),
                phase_names=("api",),
            )
            results.append(dict(arm=arm, repeat=repeat, phases=phases))
            print(
                json.dumps(
                    dict(
                        arm=arm,
                        repeat=repeat,
                        seconds=phases["api_end"]["seconds"],
                        peak_mib=phases["done"]["peak_bytes"] / 2**20,
                        decoded_mib=phases["done"]["decoded_bytes"] / 2**20
                        if phases["done"]["decoded_bytes"] is not None
                        else None,
                    )
                ),
                flush=True,
            )
            (args.output / "results.json").write_text(json.dumps(results, indent=2))
    expected = results[0]["phases"]["done"]["results"]
    if any(result["phases"]["done"]["results"] != expected for result in results):
        raise ValueError("HTTP body, filtering, sorting or pagination behavior differs: stop acceptance")
    if hashlib.sha256(args.database.read_bytes()).hexdigest() != digest:
        raise ValueError("Original database changed")
    if hashlib.sha256(source.read_bytes()).hexdigest() != original_digest:
        raise ValueError("Original source database changed")
    summary = {}
    for arm in ("baseline", "candidate"):
        rows = [result["phases"] for result in results if result["arm"] == arm]
        summary[arm] = dict(
            seconds=statistics.median(row["api_end"]["seconds"] for row in rows),
            peak_mib=statistics.median(row["done"]["peak_bytes"] / 2**20 for row in rows),
            steady_mib=statistics.median(row["done"]["steady_bytes"] / 2**20 for row in rows),
            decoded_mib=statistics.median(row["done"]["decoded_bytes"] / 2**20 for row in rows)
            if rows[0]["done"]["decoded_bytes"] is not None
            else None,
            largest_decoded_bytes=rows[0]["done"]["largest_decoded_bytes"],
        )
    summary.update(
        decoded_scope="SQLAlchemy hook only; native unobserved; legacy timing includes decoder instrumentation",
        source_sha256=digest,
        original_sha256=original_digest,
        synthetic_records=args.scale_records,
        requests_per_run=len(expected),
        complete_http_bodies_equal=True,
        scope="Full app imports and all-page journal ASGI HTTP; identical data, same interpreter, warm OS cache, alternating fresh processes, no trading lifecycle or forced GC",
    )
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
