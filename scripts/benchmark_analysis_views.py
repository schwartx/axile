"""Compare complete snapshots with small analysis views over real loopback HTTP.

Use an already published, frozen SQLite copy with read_view_schema_version=1.
The fixture's preparation and analysis calculation are outside this read test.
Each service worker has its own SQLite backup. Trading
lifespans are disabled. Independent clients perform decoding and validation.
Timing and externally sampled Python-plus-Rust RSS use separate service workers.
Only aggregate counts, timings, sizes and semantic hashes enter the report.

Example
-------
python scripts/benchmark_analysis_views.py prepare synthetic.sqlite \
    --binary /path/to/axile-analysis --observations 1000 --symbols 10
python scripts/benchmark_analysis_views.py frozen.sqlite output-directory \
    --binary /path/to/axile-analysis --account-id 1 --data-kind synthetic
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import json
import math
import os
import shutil
import signal
import socket
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SAMPLE_SECONDS = 0.002
SUMMARY_NAMES = {
    "value": "value",
    "cost": "cost",
    "loss_bp": "lossBp",
    "coverage": "coverage",
    "covered": "covered",
    "count": "count",
    "amount_complete": "amountComplete",
    "fee_covered": "feeCovered",
}
DAILY_NAMES = (*SUMMARY_NAMES.values(), "coveredValue", "estimated")
DAILY_SQL = (*SUMMARY_NAMES, "covered_value", "estimated")
HEADER_FIELDS = (
    "range",
    "engine_version",
    "backtest_included",
    "baseline",
    "end",
    "record_count",
    "observation_count",
    "used_record_count",
    "target_used_record_count",
    "invalid_asset_count",
    "sizing_fallback_count",
    "skips",
    "target_skips",
)


def digest_file(path: Path) -> str:
    """Hash a file without retaining its content in benchmark memory."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def table_rows(table: dict) -> list[dict]:
    """Decode the wire table only in the independent client process."""
    return [dict(zip(table["columns"], row, strict=True)) for row in table["rows"]]


def semantic_hash(value) -> str:
    """Identify validated client data without writing it into the report."""
    body = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


def same(left, right, path: str = "result") -> None:
    """Compare semantics strictly, allowing only tiny floating arithmetic error."""
    if isinstance(left, bool) or isinstance(right, bool):
        equal = type(left) is type(right) and left == right
    elif isinstance(left, int) and isinstance(right, int):
        equal = left == right
    elif path.endswith((".time", ".endTime")):
        equal = left == right
    elif isinstance(left, (float, int)) and isinstance(right, (float, int)):
        equal = math.isclose(left, right, rel_tol=1e-12, abs_tol=1e-12)
    elif isinstance(left, dict) and isinstance(right, dict):
        if left.keys() != right.keys():
            raise AssertionError(f"Semantic keys differ at {path}")
        for key in left:
            same(left[key], right[key], f"{path}.{key}")
        return
    elif isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            raise AssertionError(f"Semantic row count differs at {path}")
        for index, (a, b) in enumerate(zip(left, right, strict=True)):
            same(a, b, f"{path}[{index}]")
        return
    else:
        equal = type(left) is type(right) and left == right
    if not equal:
        raise AssertionError(f"Semantic value differs at {path}")


def _summary(row: dict, fees: dict) -> dict:
    return {**{public: row[column] for column, public in SUMMARY_NAMES.items()}, "fees": fees}


def _execution(row: dict, summary: dict) -> dict:
    if any(not isinstance(row[name], bool) for name in ("noop", "positions_present", "has_transactions")):
        raise AssertionError("Execution flags must be booleans")
    return {
        "record_id": row["record_id"],
        "execution_id": row["execution_id"],
        "created_at": row["created_at"],
        "is_success": row["is_success"],
        "time": row["time"],
        "has_transactions": row["has_transactions"],
        "noop": row["noop"],
        "duration_sec": row["duration_sec"],
        "symbol_count": row["symbol_count"],
        "position_count": row["position_count"],
        "positions_present": row["positions_present"],
        "reason": row["reason"],
        "summary": summary,
    }


def old_semantics(body: dict, record_id: int | None) -> dict:
    """Select common chart and execution facts from the old complete snapshot."""
    performance = body["result"]
    executions = performance["executions"]
    index = []
    for item in executions:
        record = item["record"]
        row = {
            "record_id": record["id"],
            "execution_id": record.get("execution_id"),
            "created_at": record.get("created_at"),
            "time": _timestamp(record["created_at"]),
            "has_transactions": bool(item.get("transactions")),
            "is_success": record.get("is_success"),
            "noop": item["noop"],
            "duration_sec": item.get("durationSec"),
            "symbol_count": item.get("symbolCount"),
            "position_count": item.get("positionCount"),
            "positions_present": isinstance(item.get("positions"), list),
            "reason": item.get("reason"),
        }
        summary = {key: item["summary"].get(key) for key in (*SUMMARY_NAMES.values(), "fees")}
        index.append(_execution(row, summary))
    selected = next((item for item in executions if item["record"]["id"] == record_id), None)
    detail = (
        None
        if selected is None
        else {
            "positions": selected.get("positions"),
            "attempts": selected.get("attempts", []),
            "transactions": selected.get("transactions", []),
        }
    )
    return {
        "settings": body["settings"],
        "header": {key: performance.get(key) for key in HEADER_FIELDS},
        "points": performance["points"],
        "bindings": performance.get("bindings", []),
        "fallbacks": performance.get("sizing_fallback_ranges", []),
        "calendar": performance["calendar"],
        "daily_costs": body["daily_costs"],
        "events": body["events"],
        "event_count": body["event_count"],
        "execution_index": index,
        "detail": detail,
    }


def _timestamp(value: str) -> float:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    return parsed.timestamp() * 1000


def _new_detail(body: dict | None) -> dict | None:
    if body is None:
        return None
    tables = body["tables"]
    positions = (
        [{key: row[key] for key in ("symbol", "quantity", "direction")} for row in table_rows(tables["positions"])]
        if body["header"]["positions_present"]
        else None
    )
    attempts = [
        {key: row[key] for key in ("symbol", "planned", "filled", "reason")} for row in table_rows(tables["attempts"])
    ]
    fees = {}
    for row in table_rows(tables["transaction_fees"]):
        fees.setdefault(row["transaction_idx"], {})[row["currency"]] = row["value"]
    transactions = []
    for row in table_rows(tables["transactions"]):
        names = {key: key for key in ("symbol", "side", "quantity", "price", "reference", "time", "before", "target")}
        names.update(referenceSource="reference_source", endTime="end_time", timeEstimated="time_estimated")
        transactions.append(
            {
                **{public: row[column] for public, column in names.items()},
                "summary": _summary(row, fees.get(row["idx"], {})),
            }
        )
    return {"positions": positions, "attempts": attempts, "transactions": transactions}


def new_semantics(chart: dict, detail: dict | None) -> dict:
    """Project table responses into common fields, outside the measured service."""
    tables, header = chart["tables"], chart["header"]
    calendar = dict(header["calendar"], closed_ranges=[], unavailable_ranges=[])
    for row in table_rows(tables["calendar_ranges"]):
        calendar[row["kind"] + "_ranges"].append({key: row[key] for key in ("start", "end")})
    daily = {}
    for row in table_rows(tables["daily_costs"]):
        daily[row["day"]] = {
            public: row[column]
            for index, (column, public) in enumerate(zip(DAILY_SQL, DAILY_NAMES, strict=True))
            if row["present"] & (1 << index)
        }
        daily[row["day"]]["fees"] = {}
    for row in table_rows(tables["daily_fees"]):
        daily[row["day"]]["fees"][row["currency"]] = row["value"]
    fees = {}
    for row in table_rows(tables["execution_fees"]):
        fees.setdefault(row["record_id"], {})[row["currency"]] = row["value"]
    index = [
        _execution(row, _summary(row, fees.get(row["record_id"], {}))) for row in table_rows(tables["execution_index"])
    ]
    events = [
        {**{key: row[key] for key in ("time", "tag", "text")}, "executionId": row["execution_id"]}
        for row in table_rows(tables["events"])
    ]
    return {
        "settings": chart["settings"],
        "header": {key: header.get(key) for key in HEADER_FIELDS},
        "points": table_rows(tables["points"]),
        "bindings": table_rows(tables["bindings"]),
        "fallbacks": table_rows(tables["fallbacks"]),
        "calendar": calendar,
        "daily_costs": daily,
        "events": events,
        "event_count": header["event_count"],
        "execution_index": index,
        "detail": _new_detail(detail),
    }


@contextmanager
def network_guard(allowed_port: int | None = None):
    """Reject Internet connects; the client may connect only to its exact listener."""
    original = socket.socket.connect

    def connect(connection, address):
        if connection.family in (socket.AF_INET, socket.AF_INET6):
            if allowed_port is None or address[:2] != ("127.0.0.1", allowed_port):
                raise AssertionError("Benchmark outbound connection forbidden")
        return original(connection, address)

    def connect_ex(connection, address):
        connect(connection, address)
        return 0

    with patch.object(socket.socket, "connect", connect), patch.object(socket.socket, "connect_ex", connect_ex):
        yield


async def server(config: dict) -> None:
    """Serve repository routes over real TCP with no application lifespan."""
    import uvicorn
    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from axile.server.api.deps import get_db
    from axile.server.api.routes import account_performance

    if not Path(account_performance.__file__).resolve().is_relative_to(Path(config["root"])):
        raise RuntimeError("Benchmark imported a different source tree")
    views = importlib.import_module(config["views_router"])
    engine = create_async_engine("sqlite+aiosqlite:///" + config["database"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    app = FastAPI()
    app.include_router(account_performance.router, prefix="/api/v1/account")
    app.include_router(views.router, prefix="/api/v1/account")
    app.state.analysis_manager = SimpleNamespace(wake=asyncio.Event())

    async def session_dependency():
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_db] = session_dependency
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    listener.setblocking(False)
    instance = uvicorn.Server(
        uvicorn.Config(app, loop="asyncio", lifespan="off", log_level="warning", access_log=False)
    )
    native_starts = []

    def audit(event, arguments):
        if event == "subprocess.Popen" and Path(os.fsdecode(arguments[0])).resolve() == Path(config["binary"]):
            native_starts.append(time.monotonic())

    sys.addaudithook(audit)
    task = asyncio.create_task(instance.serve(sockets=[listener]))
    try:
        while not instance.started:
            if task.done():
                await task
                raise RuntimeError("HTTP listener did not start")
            await asyncio.sleep(0.005)
        Path(config["ready"]).write_text(json.dumps({"port": listener.getsockname()[1]}))
        while not Path(config["stop"]).exists():
            if task.done():
                await task
                raise RuntimeError("HTTP listener stopped early")
            await asyncio.sleep(0.01)
    finally:
        instance.should_exit = True
        await task
        listener.close()
        await engine.dispose()
        Path(config["native_counts"]).write_text(json.dumps({"native_process_starts": len(native_starts)}))


def _client_semantics(config: dict, bodies: dict) -> dict:
    if config["arm"] == "full":
        body = bodies["full_snapshot"]
        if body["result"] is None:
            raise AssertionError("Complete snapshot result is missing")
        semantic = old_semantics(body, config["record_id"])
    else:
        body = bodies["status"]
        detail = bodies.get("one_detail")
        if detail and (
            detail.get("next_offset") is not None
            or any(value is not None for value in detail.get("next_offsets", {}).values())
        ):
            raise ValueError("Selected execution needs multiple pages; select a smaller record for one-detail scenario")
        semantic = new_semantics(bodies["chart"], detail)
    semantic["metadata"] = {
        key: body[key]
        for key in (
            "status",
            "source_version",
            "snapshot_id",
            "snapshot_version",
            "logic_version",
            "engine_version",
            "computed_at",
            "data_until",
            "error",
            "retry_at",
        )
    }
    return semantic


def client(config: dict) -> dict:
    """Measure response transfer and compare facts in an independent process."""
    import httpx

    base = f"/api/v1/account/performance/{config['account_id']}"
    query = urlencode({"snapshot_id": config["snapshot_id"], "range": config["range"]})
    endpoints = [("full_snapshot", f"{base}/snapshot?range={config['range']}")]
    if config["arm"] == "views":
        endpoints = [("status", base + "/status"), ("chart", base + "/chart?" + query)]
        if config["record_id"] is not None:
            detail_query = urlencode(
                {"snapshot_id": config["snapshot_id"], "record_id": config["record_id"], "limit": 500}
            )
            endpoints.append(("one_detail", base + "/execution-detail?" + detail_query))
    samples, bodies, semantic = [], {}, None
    with (
        network_guard(config["port"]),
        httpx.Client(base_url=f"http://127.0.0.1:{config['port']}", trust_env=False) as connection,
    ):
        for iteration in range(config["iterations"]):
            for name, path in endpoints:
                start = time.monotonic()
                response = connection.get(path, timeout=None)
                end = time.monotonic()
                if response.status_code != 200:
                    raise RuntimeError(f"{name} returned HTTP {response.status_code}")
                samples.append(
                    {
                        "endpoint": name,
                        "iteration": iteration,
                        "start": start,
                        "end": end,
                        "seconds": end - start,
                        "bytes": len(response.content),
                    }
                )
                bodies[name] = response.json()
                if bodies[name]["snapshot_id"] != config["snapshot_id"]:
                    raise AssertionError("Response snapshot identity mismatch")
            current = _client_semantics(config, bodies)
            if semantic is not None:
                same(semantic, current)
            semantic = current
    return {"samples": samples, "semantic": semantic}


def inspect_database(path: Path, account_id: int, range_key: str, record_id: int | None) -> dict:
    """Count frozen source and result rows, without reporting any business values."""
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as database:
        tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        snapshot = database.execute(
            "SELECT current_snapshot FROM account_analysis WHERE account_id=?", (account_id,)
        ).fetchone()
        if snapshot is None or not snapshot[0]:
            raise ValueError("A frozen, already published snapshot is required")
        identity = snapshot[0]
        version = database.execute(
            "SELECT read_view_schema_version FROM performance_snapshot WHERE id=?", (identity,)
        ).fetchone()
        if version != (1,):
            raise ValueError("Snapshot must contain a complete schema-1 read view")
        counts = {}
        for name in sorted(tables):
            columns = {row[1] for row in database.execute(f'PRAGMA table_info("{name}")')}
            if name.startswith("performance_") and "snapshot_id" in columns:
                counts[name] = database.execute(
                    f'SELECT count(*) FROM "{name}" WHERE snapshot_id=?', (identity,)
                ).fetchone()[0]
            elif (
                name in ("executerecord", "analysis_source_execution", "analysis_source_trade")
                and "account_id" in columns
            ):
                counts[name] = database.execute(
                    f'SELECT count(*) FROM "{name}" WHERE account_id=?', (account_id,)
                ).fetchone()[0]
        if "analysis_source_trade" in tables:
            counts["analysis_source_trade"] = database.execute(
                "SELECT count(*) FROM analysis_source_trade t JOIN analysis_source_execution e "
                "ON t.record_id=e.record_id WHERE e.account_id=?",
                (account_id,),
            ).fetchone()[0]
        if record_id is None:
            from axile.server.performance_costs import timestamp

            bounds = database.execute(
                "SELECT baseline,end FROM performance_result_range WHERE snapshot_id=? AND range_key=?",
                (identity, range_key),
            ).fetchone()
            row = database.execute(
                "SELECT record_id FROM performance_cost_execution WHERE snapshot_id=? "
                "AND time BETWEEN ? AND ? ORDER BY time DESC,record_id DESC LIMIT 1",
                (
                    identity,
                    timestamp(bounds[0]) if bounds and bounds[0] else None,
                    timestamp(bounds[1]) if bounds and bounds[1] else None,
                ),
            ).fetchone()
            record_id = row[0] if row else None
        return {"snapshot_id": identity, "record_id": record_id, "row_counts": counts}


def _kill_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _rss_sampler(pid: int, stop: threading.Event, samples: list) -> None:
    import psutil

    parent = psutil.Process(pid)
    while not stop.is_set():
        total, children = 0, 0
        try:
            processes = [parent, *parent.children(recursive=True)]
        except psutil.NoSuchProcess:
            break
        for process in processes:
            try:
                total += process.memory_info().rss
                children += process.pid != pid
            except psutil.NoSuchProcess:
                pass
        samples.append((time.monotonic(), total, children))
        stop.wait(SAMPLE_SECONDS)


def run_worker(config: dict, directory: Path, mode: str) -> dict:
    """Run an isolated HTTP service and client, cleaning their processes on failure."""
    directory.mkdir(mode=0o700)
    working = directory / "working.sqlite"
    with (
        closing(sqlite3.connect(Path(config["database"]).as_uri() + "?mode=ro", uri=True)) as source,
        closing(sqlite3.connect(working)) as target,
    ):
        source.backup(target)
    working.chmod(0o600)
    config = {
        **config,
        "database": str(working),
        "ready": str(directory / "ready"),
        "stop": str(directory / "stop"),
        "native_counts": str(directory / "native-counts.json"),
    }
    config_path = directory / "worker.json"
    config_path.write_text(json.dumps(config))
    settings = directory / "config.toml"
    settings.write_text(
        f'sqlalchemy_database_uri = "sqlite+aiosqlite:///{working}"\napp_log_dir = "{directory}/logs"\nenvironment = "local"\n'
    )
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith(("AXILE_ANALYSIS_", "AXILE_CONFIG_"))
    }
    environment.update(
        AXILE_CONFIG_TOML=str(settings),
        AXILE_ANALYSIS_MODE="required",
        AXILE_ANALYSIS_BINARY=config["binary"],
        PYTHONPATH=config["root"],
        PYTHONHASHSEED="0",
        OMP_NUM_THREADS="1",
        OPENBLAS_NUM_THREADS="1",
    )
    samples, stop, thread = [], threading.Event(), None
    with (directory / "server.log").open("w") as log:
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--server", str(config_path)],
            cwd=config["root"],
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        started = time.monotonic()
        try:
            if mode == "rss":
                thread = threading.Thread(target=_rss_sampler, args=(process.pid, stop, samples), daemon=True)
                thread.start()
            while not Path(config["ready"]).exists():
                if process.poll() is not None:
                    raise RuntimeError("HTTP worker failed; inspect its private server.log")
                stop.wait(0.01)
            config.update(json.loads(Path(config["ready"]).read_text()))
            startup_seconds = time.monotonic() - started
            config_path.write_text(json.dumps(config))
            result = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "--client", str(config_path)],
                cwd=config["root"],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode:
                (directory / "client.log").write_text(result.stderr)
                raise RuntimeError("HTTP client failed; inspect its private client.log")
            measured = json.loads(result.stdout)
            Path(config["stop"]).touch()
            process.wait()
            if process.returncode:
                raise RuntimeError("HTTP service did not shut down cleanly")
        finally:
            _kill_group(process)
            process.wait()
            stop.set()
            if thread:
                thread.join()
    measured.update(json.loads(Path(config["native_counts"]).read_text()), startup_seconds=startup_seconds)
    if config["arm"] == "views" and measured["native_process_starts"]:
        raise AssertionError("Small view reads started Rust subprocesses")
    if mode == "rss":
        for item in measured["samples"]:
            window = [rss for when, rss, _ in samples if item["start"] <= when <= item["end"]]
            item["sampled_combined_peak_bytes"] = max(window) if window else None
        measured["sampled_combined_process_peak_bytes"] = max((row[1] for row in samples), default=0)
        measured["sampled_max_rust_children"] = max((row[2] for row in samples), default=0)
    return measured


def aggregate(results: list) -> dict:
    """Report first and repeat HTTP costs separately from sampled memory."""
    report = {}
    for arm in ("full", "views"):
        runs = [row for row in results if row["arm"] == arm]
        endpoints = sorted({sample["endpoint"] for row in runs for sample in row["samples"]})
        report[arm] = {}
        for endpoint in endpoints:
            timed = [
                item
                for row in runs
                if row["mode"] == "timing"
                for item in row["samples"]
                if item["endpoint"] == endpoint
            ]
            memory = [
                item["sampled_combined_peak_bytes"]
                for row in runs
                if row["mode"] == "rss"
                for item in row["samples"]
                if item["endpoint"] == endpoint and item.get("sampled_combined_peak_bytes") is not None
            ]
            first = [item["seconds"] for item in timed if item["iteration"] == 0]
            repeat = [item["seconds"] for item in timed if item["iteration"] > 0]
            report[arm][endpoint] = {
                "first_seconds_p50": statistics.median(first),
                "repeat_seconds_p50": statistics.median(repeat) if repeat else None,
                "repeat_seconds_p95_observed": sorted(repeat)[math.ceil(len(repeat) * 0.95) - 1] if repeat else None,
                "response_bytes": sorted({item["bytes"] for item in timed}),
                "sampled_combined_peak_bytes": max(memory) if memory else None,
                "rss_requests_with_samples": len(memory),
                "timing_sample_count": len(timed),
            }
        chain = [
            sum(
                item["seconds"]
                for item in row["samples"]
                if item["iteration"] == 0 and item["endpoint"] != "one_detail"
            )
            for row in runs
            if row["mode"] == "timing"
        ]
        report[arm]["first_open"] = {
            "http_seconds_p50": statistics.median(chain),
            "response_bytes": sum(report[arm][key]["response_bytes"][0] for key in endpoints if key != "one_detail"),
            "request_count": 1 if arm == "full" else 2,
            "excludes_client_decode_and_render": True,
        }
    return report


def source_manifest(root: Path) -> dict:
    """Identify service source files independently of unrelated UI work."""
    return {str(path.relative_to(root)): digest_file(path) for path in sorted((root / "axile/server").rglob("*.py"))}


async def _publish_fixture(database: Path, binary: Path) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from axile.server.performance_analysis import AnalysisManager, enqueue

    os.environ["AXILE_ANALYSIS_MODE"] = "required"
    os.environ["AXILE_ANALYSIS_BINARY"] = str(binary.resolve())
    engine = create_async_engine("sqlite+aiosqlite:///" + str(database))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as session:
            await enqueue(session.get_bind().url.database, 1, force=True)
        if not await AnalysisManager(sessions).run_once():
            raise AssertionError("Synthetic analysis task was not claimed")
    finally:
        await engine.dispose()


def prepare_main(arguments: list[str]) -> None:
    """Prepare deterministic typed source facts, then publish with actual Rust."""
    parser = argparse.ArgumentParser(description="Prepare an explicitly synthetic analysis fixture")
    parser.add_argument("database", type=Path)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--observations", type=int, default=1000)
    parser.add_argument("--symbols", type=int, default=10)
    parser.add_argument("--source-only", action="store_true")
    parser.add_argument("--publish-existing", action="store_true")
    args = parser.parse_args(arguments)
    if not 1 <= args.observations <= 10000 or not 1 <= args.symbols <= 10:
        parser.error("Use 1..10000 observations and 1..10 symbols")
    if not args.source_only and args.binary is None:
        parser.error("--binary is required for a real native publication")
    args.database = args.database.resolve()
    args.database.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    settings = args.database.parent / (args.database.stem + ".config.toml")
    settings.write_text(
        f'sqlalchemy_database_uri = "sqlite+aiosqlite:///{args.database}"\napp_log_dir = "{args.database.parent}/logs"\nenvironment = "local"\n'
    )
    os.environ["AXILE_CONFIG_TOML"] = str(settings)
    with network_guard():
        if not args.publish_existing:
            if args.database.exists():
                raise FileExistsError("Synthetic fixture already exists")
            import sqlalchemy as sa

            from scripts.benchmark_structured_publication import migration, prepare

            prepare(SimpleNamespace(prepare=args.database, observations=args.observations, symbols=args.symbols))
            engine = sa.create_engine("sqlite:///" + str(args.database))
            try:
                with engine.begin() as connection:
                    migration("0036_analysis_read_views.py").install_triggers(connection)
            finally:
                engine.dispose()
            args.database.chmod(0o600)
        if not args.source_only:
            asyncio.run(_publish_fixture(args.database, args.binary))
            fixture = inspect_database(args.database, 1, "all", None)
            print(json.dumps({"data_kind": "synthetic", "database": str(args.database), **fixture}))


def main() -> None:
    """Compare fixed published views without running trading or source refresh."""
    if len(sys.argv) > 1 and sys.argv[1] == "prepare":
        prepare_main(sys.argv[2:])
        return
    if len(sys.argv) == 3 and sys.argv[1] in ("--server", "--client"):
        config = json.loads(Path(sys.argv[2]).read_text())
        sys.path.insert(0, config["root"])
        if sys.argv[1] == "--server":
            with network_guard():
                asyncio.run(server(config))
        else:
            print(json.dumps(client(config), allow_nan=False))
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--account-id", type=int, required=True)
    parser.add_argument("--record-id", type=int)
    parser.add_argument("--range", choices=("all", "30", "90"), default="all")
    parser.add_argument("--data-kind", choices=("frozen", "synthetic"), required=True)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--views-router", default="axile.server.api.routes.account_analysis")
    args = parser.parse_args()
    if not 1 <= args.runs <= 5 or not 1 <= args.iterations <= 10:
        parser.error("Use 1..5 runs and 1..10 iterations to keep each worker short")
    args.output = args.output.resolve()
    args.database = args.database.resolve()
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    sources = source_manifest(args.root.resolve())
    (args.output / "source-manifest.json").write_text(json.dumps(sources, indent=2))
    frozen_binary = args.output / "axile-analysis"
    shutil.copy2(args.binary, frozen_binary)
    fingerprints = {
        str(path): digest_file(path) for path in (args.database, Path(str(args.database) + "-wal")) if path.exists()
    }
    fixture = inspect_database(args.database, args.account_id, args.range, args.record_id)
    config = {
        "root": str(args.root.resolve()),
        "database": str(args.database),
        "binary": str(frozen_binary),
        "account_id": args.account_id,
        "range": args.range,
        "iterations": args.iterations,
        "views_router": args.views_router,
        **fixture,
    }
    results, expected = [], None
    for mode in ("timing", "rss"):
        for repeat in range(args.runs):
            for arm in ("full", "views") if repeat % 2 == 0 else ("views", "full"):
                measured = run_worker({**config, "arm": arm}, args.output / f"{mode}-{repeat}-{arm}", mode)
                semantic = measured.pop("semantic")
                if expected is None:
                    expected = semantic
                same(expected, semantic)
                measured.update(arm=arm, mode=mode, repeat=repeat, semantic_sha256=semantic_hash(semantic))
                results.append(measured)
    if any(digest_file(Path(path)) != digest for path, digest in fingerprints.items()):
        raise AssertionError("Frozen source database changed")
    if source_manifest(args.root.resolve()) != sources:
        raise AssertionError("Service source changed during the comparison; rerun on a fixed revision")
    selected = expected["detail"]
    report = {
        "data_kind": args.data_kind,
        "source_sha256": fingerprints[str(args.database)],
        "binary_sha256": digest_file(frozen_binary),
        "service_source_manifest_sha256": semantic_hash(sources),
        "row_counts": fixture["row_counts"],
        "transport": "router-only loopback HTTP service; independent client; lifespan disabled",
        "scope": "same-source/same-binary endpoint comparison; published reads only; excludes calculation, whole-app middleware/residency, browser decoding and rendering",
        "rss_sample_seconds": SAMPLE_SECONDS,
        "rss_scope": "sampled simultaneous Python router service plus Rust descendants; client/driver excluded; shared pages may be counted twice and short peaks missed",
        "shared_semantics_equal": True,
        "detail_semantics_compared": ["positions", "attempts", "transactions and transaction fees"],
        "detail_semantics_unchecked": ["raw presence/status fields", "per-symbol status", "detail-only header fields"],
        "selected_detail_rows": {
            name: len(selected[name]) if selected is not None and selected[name] is not None else None
            for name in ("positions", "attempts", "transactions")
        },
        "semantic_categories": list(expected),
        "metrics": aggregate(results),
        "runs": results,
    }
    (args.output / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps({key: value for key, value in report.items() if key != "runs"}, indent=2))


if __name__ == "__main__":
    main()
