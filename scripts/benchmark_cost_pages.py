"""Compare complete-app cost HTTP reads in two source trees, using sealed DB copies."""

import argparse
import asyncio
import base64
import hashlib
import http.client
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
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode


def client(port, identity, output):
    """Measure HTTP independently from the server and retain full response evidence."""
    from axile.server.performance_details import CostQuery, _cursor

    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=12)
    requests = []
    try:
        for repeat in range(3):
            for options in (
                {},
                {"sort": "cost"},
                {"side": "buy"},
                {"side": "sell"},
                {"symbol_search": "POC_00"},
                {"offset": 20},
            ):
                values = dict(options)
                offset = values.pop("offset", 0)
                query = CostQuery(snapshot_id=identity, dimension="symbol", **values)
                params = query.model_dump(exclude_none=True)
                if offset:
                    digest, _ = _cursor(query, 2)
                    params["cursor"] = base64.urlsafe_b64encode(
                        json.dumps({"scope": digest, "offset": offset}).encode()
                    ).decode()
                start = time.perf_counter()
                connection.request("GET", "/api/v1/account/performance/2/costs?" + urlencode(params))
                response = connection.getresponse()
                body = response.read()
                elapsed = time.perf_counter() - start
                if response.status != 200:
                    raise RuntimeError(f"HTTP {response.status}")
                value = json.loads(body)
                canonical = json.dumps(value, sort_keys=True, allow_nan=False).encode()
                requests.append(
                    {
                        "repeat": repeat,
                        "query": options,
                        "seconds": elapsed,
                        "bytes": len(body),
                        "sha256": hashlib.sha256(canonical).hexdigest(),
                        "response": value if repeat == 0 else None,
                    }
                )
    finally:
        connection.close()
    output.write_text(json.dumps(requests, indent=2))


async def worker(root, database, output):
    """Import the complete app, serve loopback HTTP, and measure all server RSS."""
    started = time.perf_counter()
    import psutil
    import uvicorn

    from axile.server import performance_details
    from axile.server.app import _app
    from axile.server.core.db import engine

    if not Path(performance_details.__file__).resolve().is_relative_to(root):
        raise ValueError("Wrong source tree")
    with sqlite3.connect(database) as connection:
        identity = connection.execute("SELECT current_snapshot FROM account_analysis WHERE account_id=2").fetchone()[0]
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(_app, lifespan="off", access_log=False, log_level="error"))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    process = None
    try:
        async with asyncio.timeout(5):
            while not server.started:
                await asyncio.sleep(0.01)
        ready = time.perf_counter()
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(Path(__file__).resolve()),
            "--client",
            str(listener.getsockname()[1]),
            identity,
            str(output / "client.json"),
        )
        if await asyncio.wait_for(process.wait(), 15):
            raise RuntimeError("Client failed")
        samples = []
        for _ in range(10):
            await asyncio.sleep(0.1)
            samples.append(psutil.Process().memory_info().rss)
        report = {
            "startup_seconds": ready - started,
            "steady_mib": statistics.median(samples) / 2**20,
            "peak_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            / (2**20 if sys.platform == "darwin" else 1024),
            "source": str(performance_details.__file__),
            "duckdb_imported": "duckdb" in sys.modules,
            "arrow_imported": "pyarrow" in sys.modules,
        }
        (output / "server.json").write_text(json.dumps(report, indent=2))
    finally:
        if process and process.returncode is None:
            process.kill()
            await process.wait()
        server.should_exit = True
        await asyncio.wait_for(serving, 5)
        listener.close()
        await engine.dispose()


def compare(args):
    """Alternate fresh processes; each receives an isolated unmodified frozen source copy."""
    args.output.mkdir(parents=True, exist_ok=False)
    source_hash = hashlib.sha256(args.database.read_bytes()).hexdigest()
    results = []
    roots = {"baseline": args.baseline.resolve(), "candidate": args.candidate.resolve()}
    code_hashes = {
        name: hashlib.sha256((root / "axile/server/performance_details.py").read_bytes()).hexdigest()
        for name, root in roots.items()
    }
    for run in range(args.runs):
        for arm in ["baseline", "candidate"] if run % 2 == 0 else ["candidate", "baseline"]:
            target = args.output / f"{run}-{arm}"
            target.mkdir()
            working = target / "source.db"
            shutil.copyfile(args.database, working)
            working.chmod(0o600)
            config = target / "config.toml"
            config.write_text(
                f'sqlalchemy_database_uri = "sqlite+aiosqlite:///{working}"\napp_log_dir = "{target / "logs"}"\nenvironment = "local"\n'
            )
            env = os.environ | {
                "PYTHONPATH": str(roots[arm]),
                "AXILE_CONFIG_TOML": str(config),
                "OMP_NUM_THREADS": "1",
                "OPENBLAS_NUM_THREADS": "1",
                "POLARS_MAX_THREADS": "1",
            }
            with (target / "server.log").open("w") as log:
                subprocess.run(
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--worker",
                        str(roots[arm]),
                        str(working),
                        str(target),
                    ],
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    timeout=20,
                    check=True,
                )
            report = json.loads((target / "server.json").read_text())
            requests = json.loads((target / "client.json").read_text())
            results.append({"run": run, "arm": arm, **report, "requests": requests})
            (args.output / "results.json").write_text(json.dumps(results, indent=2))
            print(arm, run, "request_seconds", round(sum(row["seconds"] for row in requests), 3), flush=True)
    hashes = [[row["sha256"] for row in item["requests"]] for item in results]
    summary = {
        "source_sha256": source_hash,
        "source_unchanged": source_hash == hashlib.sha256(args.database.read_bytes()).hexdigest(),
        "code_sha256": code_hashes,
        "code_unchanged": all(
            digest == hashlib.sha256((roots[name] / "axile/server/performance_details.py").read_bytes()).hexdigest()
            for name, digest in code_hashes.items()
        ),
        "responses_equal": all(row == hashes[0] for row in hashes),
        "runs": args.runs,
    }
    for arm in roots:
        rows = [item for item in results if item["arm"] == arm]
        summary[arm] = {
            "startup_and_requests_seconds": statistics.median(
                item["startup_seconds"] + sum(row["seconds"] for row in item["requests"]) for item in rows
            ),
            "warm_requests_seconds": statistics.median(
                sum(row["seconds"] for row in item["requests"] if row["repeat"] > 0) for item in rows
            ),
            "peak_mib": statistics.median(item["peak_mib"] for item in rows),
            "steady_mib": statistics.median(item["steady_mib"] for item in rows),
        }
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    if not all(summary[key] for key in ("source_unchanged", "code_unchanged", "responses_equal")):
        raise ValueError("Evidence mismatch; inspect saved results")


def main():
    """Keep controller, client and service imports in distinct processes."""
    if sys.argv[1] == "--client":
        client(int(sys.argv[2]), sys.argv[3], Path(sys.argv[4]))
    elif sys.argv[1] == "--worker":
        with patch.object(socket.socket, "connect", side_effect=AssertionError("Outbound forbidden")):
            asyncio.run(worker(Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4])))
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("database", type=Path)
        parser.add_argument("output", type=Path)
        parser.add_argument("--baseline", type=Path, required=True)
        parser.add_argument("--candidate", type=Path, required=True)
        parser.add_argument("--runs", type=int, default=3)
        args = parser.parse_args()
        args.database, args.output = args.database.resolve(), args.output.resolve()
        compare(args)


if __name__ == "__main__":
    main()
