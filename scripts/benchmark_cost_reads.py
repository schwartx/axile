"""Compare cost HTTP responses, decoding and process RSS on identical frozen data."""

import argparse
import asyncio
import base64
import hashlib
import json
import resource
import socket
import sqlite3
import statistics
import sys
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

try:
    from benchmark_business_fact_storage import initialize_benchmark_database
    from benchmark_performance_frames import emit, observe
except ModuleNotFoundError:
    from scripts.benchmark_business_fact_storage import initialize_benchmark_database
    from scripts.benchmark_performance_frames import emit, observe


def queries(identity, trade):
    """Cover all dimensions/ranges/sorts plus actual and empty filter scopes."""
    for range_key in ("all", "30", "90"):
        for dimension in ("summary", "execution", "symbol", "trade"):
            for order in ("time", "cost"):
                yield dict(snapshot_id=identity, range=range_key, dimension=dimension, sort=order, limit=7)
    base = dict(snapshot_id=identity, dimension="trade", limit=7)
    scopes = [dict(side=value) for value in ("buy", "sell", "none")]
    scopes += [dict(symbol_search="%"), dict(symbol_search="missing")]
    if trade:
        when, day, symbol, record_id = trade
        scopes += [
            dict(day=day),
            dict(symbol=symbol),
            dict(symbol_search=symbol),
            dict(record_id=record_id),
            dict(start=when - 1, end=when),
        ]
    for scope in scopes:
        yield base | scope
    yield base | {"snapshot_id": "missing"}


async def pages(client, query, results):
    """Check first, next and last pages and reject a changed cursor scope."""
    path = "/api/v1/account/performance/2/costs"

    async def fetch(params):
        response = await client.get(path, params=params)
        if response.status_code not in (200, 400, 410):
            raise ValueError("Unexpected HTTP status: " + str(response.status_code))
        results.append(
            dict(
                query=params,
                status=response.status_code,
                bytes=len(response.content),
                sha256=hashlib.sha256(response.content).hexdigest(),
            )
        )
        return response.json()

    payload = await fetch(query)
    cursor = payload.get("next_cursor")
    if cursor is None:
        return
    await fetch(query | {"cursor": cursor})
    await fetch(query | {"cursor": cursor, "limit": query["limit"] + 1})
    if payload["count"] > query["limit"] * 2:
        tail = json.loads(base64.urlsafe_b64decode(cursor))
        tail["offset"] = max(0, payload["count"] - query["limit"])
        token = base64.urlsafe_b64encode(json.dumps(tail).encode()).decode()
        await fetch(query | {"cursor": token})


async def worker(args):
    """Import the complete app and invoke actual cost HTTP handlers, without trading."""
    import httpx
    import psutil

    from axile.server.core import db

    initialize_benchmark_database(args.output / "working.db")
    from axile.server import performance_details
    from axile.server.app import _app

    engine = getattr(db, "engine", None)

    if not Path(performance_details.__file__).resolve().is_relative_to(args.root.resolve()):
        raise ValueError("Wrong imported source tree")
    decoded = []

    def deserialize(raw):
        decoded.append(len(raw.encode("utf-8") if isinstance(raw, str) else raw))
        return json.loads(raw)

    # Only legacy SQLAlchemy decoding is observable through this hook.
    if engine is not None:
        engine.sync_engine.dialect._json_deserializer = deserialize
    try:
        with closing(
            sqlite3.connect((args.output / "working.db").resolve().as_uri() + "?mode=ro", uri=True)
        ) as database:
            state = database.execute("SELECT current_snapshot FROM account_analysis WHERE account_id=2").fetchone()
            identity = state[0] if state else None
            trade = database.execute(
                "SELECT time,day,symbol,record_id FROM performance_cost_trade WHERE snapshot_id=? ORDER BY id LIMIT 1",
                (identity,),
            ).fetchone()
        if identity is None:
            raise ValueError("A genuine old-writer frozen snapshot is required")
        results = []
        emit("imported", source=str(Path(performance_details.__file__).resolve()))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app), base_url="http://127.0.0.1") as client:
            emit("api_start")
            for query in queries(identity, trade):
                await pages(client, query, results)
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
    parser.add_argument("--worker", action="store_true")
    args = parser.parse_args()
    if args.worker:
        with patch.object(socket.socket, "connect", side_effect=AssertionError("Outbound connections forbidden")):
            asyncio.run(worker(args))
        return
    if args.baseline is None or args.runs < 1:
        parser.error("--baseline and at least one run are required")
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
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
        raise ValueError("HTTP body, filtering, sorting or cursor behavior differs: stop acceptance")
    if hashlib.sha256(args.database.read_bytes()).hexdigest() != digest:
        raise ValueError("Original database changed")
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
        requests_per_run=len(expected),
        complete_http_bodies_equal=True,
        scope="Full app imports and real ASGI cost HTTP; identical frozen data, same interpreter, warm OS cache, alternating fresh processes, no trading lifecycle or forced GC; native JSON decoding is unobserved",
    )
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
