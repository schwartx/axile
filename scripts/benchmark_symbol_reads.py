"""Compare symbol activity HTTP responses, decoding and whole-process RSS."""

import argparse
import asyncio
import hashlib
import json
import resource
import socket
import statistics
import sys
from pathlib import Path
from unittest.mock import patch

from scripts.benchmark_journal_pages import scale_copy
from scripts.benchmark_performance_frames import emit, observe


async def pages(client, results):
    """Read the symbol overview and expand each returned symbol, including empty scopes."""
    path = "/api/v1/account/2/activity/symbols"

    async def fetch(query):
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
        return response.json()

    for since, until in (("2020", "2030"), ("2025-01-02", "2026-01-01"), ("2090", "2091")):
        query = dict(since=since, until=until)
        payload = await fetch(query)
        for row in payload["data"]:
            await fetch(query | dict(symbol=row["symbol"]))
        await fetch(query | dict(symbol="missing"))


async def worker(args):
    """Import the complete app and invoke actual journal HTTP handlers, without trading."""
    import httpx
    import psutil

    from axile.server.api.routes import account_schedule
    from axile.server.app import _app
    from axile.server.core.db import engine

    if not Path(account_schedule.__file__).resolve().is_relative_to(args.root.resolve()):
        raise ValueError("Wrong imported source tree")
    decoded = []

    def deserialize(raw):
        decoded.append(len(raw))
        return json.loads(raw)

    # Record the actual JSON sent from SQLite to Python, with the same decoder
    # and constant-size instrumentation overhead in both arms.
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
            decoded_bytes=sum(decoded),
            largest_decoded_bytes=max(decoded, default=0),
        )
    finally:
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
                        decoded_mib=phases["done"]["decoded_bytes"] / 2**20,
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
            decoded_mib=statistics.median(row["done"]["decoded_bytes"] / 2**20 for row in rows),
            largest_decoded_bytes=rows[0]["done"]["largest_decoded_bytes"],
        )
    summary.update(
        source_sha256=digest,
        original_sha256=original_digest,
        synthetic_records=args.scale_records,
        requests_per_run=len(expected),
        complete_http_bodies_equal=True,
        scope="Full app imports and symbol overview and expanded-trade ASGI HTTP; identical data, same interpreter, warm OS cache, alternating fresh processes, no trading lifecycle or forced GC",
    )
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
