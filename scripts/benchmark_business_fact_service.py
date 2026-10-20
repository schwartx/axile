"""Compare isolated complete-service HTTP reads and fact writes on frozen fixtures.

Both arms import their complete FastAPI application but disable its trading,
scheduler and analysis lifespans. Benchmark-only loopback endpoints invoke the
production previous-target reader and terminal writer without an executor.
An independent client measures transfer latency; a separate observer samples
simultaneous service-plus-descendant RSS in fresh runs. This is a synthetic
service benchmark, not production or live-counter acceptance.

Database preparation and migration are deliberately outside measured windows.
Supply baseline/candidate databases produced from the same JSONL fixture by
``benchmark_business_fact_storage.py``. Timing and RSS use separate workers.
"""

from __future__ import annotations

# The driver and isolated arms share this source-only evidence oracle.
try:
    from scripts.business_facts_contract import business_evidence
except ModuleNotFoundError:
    from business_facts_contract import business_evidence

import argparse
import asyncio
import hashlib
import http.client
import json
import os
import signal
import socket
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SAMPLE_SECONDS = 0.005
HEAD_FIELDS = ("id", "execution_id", "created_at", "is_success")


def digest_file(path: Path) -> str:
    """Hash an input without loading it into observer memory."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def semantic_hash(value: object) -> str:
    """Preserve null, zero, booleans and collection ordering in comparisons."""
    encoded = json.dumps(
        business_evidence(value), sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


def source_tree_hash(root: Path) -> str:
    """Freeze all application Python files outside the measured service process."""
    return semantic_hash(
        {str(path.relative_to(root)): digest_file(path) for path in sorted((root / "axile").rglob("*.py"))}
    )


def source_identity(root: Path) -> dict:
    """Record archive provenance without requiring a Git checkout or Git calls."""
    archive = root / "BASELINE_SOURCE.json"
    if archive.exists():
        return json.loads(archive.read_text())
    return {"kind": "worktree snapshot", "identity": "application source hash recorded separately"}


def fixture_manifest(path: Path) -> dict:
    """Require the deterministic seed manifest and verify its complete byte hash."""
    manifest = json.loads(path.with_suffix(path.suffix + ".manifest.json").read_text())
    if digest_file(path) != manifest["sha256"]:
        raise AssertionError("Frozen fixture differs from its manifest")
    return manifest


def common_semantics(endpoint: str, body: dict, arm: str) -> object:
    """Select matching facts across intentionally different response models."""
    if endpoint == "history":
        return {"count": body["count"], "data": [{key: item[key] for key in HEAD_FIELDS} for item in body["data"]]}
    if endpoint == "detail":
        item = body["data"][0] if arm == "baseline" else body
        return {
            "head": {key: item[key] for key in HEAD_FIELDS},
            "input": item["raw_input"] if arm == "baseline" else item["input"],
            "result": item["raw_result"] if arm == "baseline" else item["result"],
        }
    return body


@contextmanager
def network_guard(port: int | None = None):
    """Allow only the independent client's exact loopback listener."""
    connect = socket.socket.connect

    def guarded(connection, address):
        if connection.family in (socket.AF_INET, socket.AF_INET6) and (
            port is None or address[:2] != ("127.0.0.1", port)
        ):
            raise AssertionError("Benchmark outbound connection forbidden")
        return connect(connection, address)

    def guarded_ex(connection, address):
        guarded(connection, address)
        return 0

    with patch.object(socket.socket, "connect", guarded), patch.object(socket.socket, "connect_ex", guarded_ex):
        yield


def imported_sources(root: Path) -> dict[str, str]:
    """Reject editable installs that resolve any Axile module outside this arm."""
    paths = {}
    for name, module in tuple(sys.modules.items()):
        filename = getattr(module, "__file__", None)
        if (name == "axile" or name.startswith("axile.")) and filename:
            resolved = Path(filename).resolve()
            if not resolved.is_relative_to(root / "axile"):
                raise RuntimeError(f"Wrong Axile import source: {name}")
            paths[name] = str(resolved)
    return paths


def prioritize_benchmark_routes(routes: list) -> None:
    """Keep isolated adapters reachable when the app includes its SPA catchall."""
    prefix = "/__business_fact_benchmark/"
    adapters = [route for route in routes if getattr(route, "path", "").startswith(prefix)]
    routes[:] = adapters + [route for route in routes if route not in adapters]


async def serve(config: dict) -> None:
    """Serve the actual application and isolated adapters without its lifespan."""
    deadline = time.monotonic() + config["timeout"]
    import uvicorn
    from fastapi import Body
    from loguru import logger

    from axile.server.app import _app
    from axile.server.core.db import SessionLocal, engine
    from axile.server.execution import rebalance, records

    root = Path(config["root"]).resolve()
    imported_sources(root)
    logger.remove()
    rebalance.SessionLocal = SessionLocal
    _app.state.analysis_manager = SimpleNamespace(wake=asyncio.Event())

    @_app.get("/__business_fact_benchmark/last-target", tags=["benchmark"])
    async def last_target():
        return await rebalance._load_last_target_snapshot(SimpleNamespace(id=config["account_id"]))

    @_app.post("/__business_fact_benchmark/write", tags=["benchmark"])
    async def write(payload: dict = Body()):
        record = await records._persist_execute_record(
            account_id=config["account_id"],
            execution_id=payload["execution_id"],
            raw_input=payload["raw_input"],
            raw_result=payload["raw_result"],
            is_success=payload["is_success"],
            session_factory=SessionLocal,
        )
        return {"id": record.id, "execution_id": record.execution_id, "is_success": record.is_success}

    prioritize_benchmark_routes(_app.router.routes)

    def reject_subprocess(event, _arguments):
        if event == "subprocess.Popen":
            raise AssertionError("Native analysis and channel subprocesses are disabled in this benchmark")

    sys.addaudithook(reject_subprocess)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(_app, lifespan="off", log_level="error", access_log=False))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(15):
            while not server.started:
                if task.done():
                    await task
                    raise RuntimeError("HTTP listener failed to start")
                await asyncio.sleep(0.01)
        readiness = Path(config["ready"])
        pending = readiness.with_suffix(".pending")
        pending.write_text(json.dumps({"port": listener.getsockname()[1]}))
        pending.replace(readiness)
        while not Path(config["stop"]).exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("Service self-watchdog exceeded the worker deadline")
            if task.done():
                await task
                raise RuntimeError("HTTP service stopped unexpectedly")
            await asyncio.sleep(0.01)
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, 5)
        finally:
            listener.close()
            await engine.dispose()
        paths = imported_sources(root)
        source_evidence = {
            "root": str(root),
            "module_count": len(paths),
            "verified_all_axile_imports": True,
            "key_modules": {
                name: paths[name]
                for name in ("axile.server.app", "axile.server.execution.records", "axile.server.execution.rebalance")
            },
            "module_paths_sha256": semantic_hash(paths),
            "frozen_source_tree_sha256": config["source_tree_sha256"],
            "native_analysis": "disabled equally; lifespan off and subprocess creation rejected",
        }
        Path(config["sources"]).write_text(json.dumps(source_evidence, indent=2))


def endpoint_paths(config: dict) -> dict[str, str]:
    """Use each version's real cold-detail route and matching common routes."""
    base = "/api/v1/account"
    history = f"{base}/execute_records/{config['account_id']}"
    detail = (
        f"{history}?record_id={config['record_id']}&limit=1"
        if config["arm"] == "baseline"
        else f"{history}/{config['record_id']}"
    )
    return {
        "accounts": base + "/",
        "history": history + "?limit=20",
        "detail": detail,
        "last_target": "/__business_fact_benchmark/last-target",
        "write": "/__business_fact_benchmark/write",
    }


def request(config: dict, endpoint: str, phase: str, payload: dict | None = None) -> tuple[dict, dict]:
    """Measure request through complete transfer; JSON decoding follows timing."""
    connection = http.client.HTTPConnection("127.0.0.1", config["port"], timeout=10)
    encoded = None if payload is None else json.dumps(payload, allow_nan=False).encode()
    started = time.monotonic()
    try:
        connection.request(
            "GET" if encoded is None else "POST",
            endpoint_paths(config)[endpoint],
            encoded,
            {} if encoded is None else {"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        body = response.read()
        ended = time.monotonic()
        if response.status != 200:
            raise RuntimeError(f"Benchmark {endpoint} returned HTTP {response.status}")
        sample = {
            "endpoint": endpoint,
            "phase": phase,
            "start": started,
            "end": ended,
            "seconds": ended - started,
            "bytes": len(body),
        }
        return sample, json.loads(body)
    finally:
        connection.close()


def run_client(config: dict) -> dict:
    """Drive reads, terminal writes and mixed read/write from a sibling process."""
    fixture = Path(config["fixture"])
    seed = fixture_manifest(fixture)
    if seed["sha256"] != config["fixture_sha256"]:
        raise AssertionError("Fixture changed before the independent client started")
    with fixture.open() as stream:
        template = json.loads(next(line for line in stream if line.strip()))
    samples, semantics, writes = [], {}, []
    initial_count, initial_target = None, None
    for repeat in range(config["repeats"] + 1):
        phase = "cold" if repeat == 0 else "warm"
        for endpoint in ("accounts", "history", "detail", "last_target"):
            sample, body = request(config, endpoint, phase)
            samples.append(sample)
            if endpoint == "history":
                initial_count = body["count"]
                if initial_count != seed["counts"]["executions"]:
                    raise AssertionError("Source database execution count differs from the frozen fixture")
            elif endpoint == "last_target":
                initial_target = body
            digest = semantic_hash(common_semantics(endpoint, body, config["arm"]))
            if endpoint in semantics and semantics[endpoint] != digest:
                raise AssertionError(f"Read changed without writes: {endpoint}")
            semantics[endpoint] = digest
    for index in range(config["writes"]):
        payload = {
            "execution_id": f"benchmark-service-write-{index}",
            **{key: template[key] for key in ("raw_input", "raw_result", "is_success")},
        }
        sample, written = request(config, "write", "write", payload)
        if written["execution_id"] != payload["execution_id"] or written["is_success"] != payload["is_success"]:
            raise AssertionError("Terminal writer returned mismatched identity/outcome")
        samples.append(sample)
        writes.append(written)
    with ThreadPoolExecutor(max_workers=3) as executor:
        for index in range(config["mixed"]):
            payload = {
                "execution_id": f"benchmark-service-mixed-{index}",
                **{key: template[key] for key in ("raw_input", "raw_result", "is_success")},
            }
            jobs = [
                (endpoint, executor.submit(request, config, endpoint, "mixed", body))
                for endpoint, body in (("write", payload), ("accounts", None), ("detail", None))
            ]
            for endpoint, job in jobs:
                sample, body = job.result()
                samples.append(sample)
                if endpoint == "write":
                    if body["execution_id"] != payload["execution_id"] or body["is_success"] != payload["is_success"]:
                        raise AssertionError("Concurrent terminal writer returned mismatched identity/outcome")
                    writes.append(body)
                elif (
                    endpoint != "write"
                    and semantic_hash(common_semantics(endpoint, body, config["arm"])) != semantics[endpoint]
                ):
                    raise AssertionError(f"Concurrent read changed stable evidence: {endpoint}")
    verified = verify_writes(config, template, writes, initial_count, initial_target)
    return {"samples": samples, "semantics": semantics, "verified_writes": verified}


def verify_writes(config: dict, template: dict, writes: list, initial_count: int, initial_target: object) -> dict:
    """Reread committed evidence outside timed POST windows instead of trusting 200."""
    identities = [row["id"] for row in writes]
    if len(set(identities)) != len(writes):
        raise AssertionError("Distinct source writes reused a terminal record")
    _, history = request(config, "history", "verification")
    if history["count"] != initial_count + len(writes):
        raise AssertionError("Terminal writes did not produce the expected committed count")
    expected = semantic_hash({"input": template["raw_input"], "result": template["raw_result"]})
    for written in writes:
        _, body = request({**config, "record_id": written["id"]}, "detail", "verification")
        detail = common_semantics("detail", body, config["arm"])
        if detail["head"]["execution_id"] != written["execution_id"]:
            raise AssertionError("Committed detail points to a different execution")
        if semantic_hash({"input": detail["input"], "result": detail["result"]}) != expected:
            raise AssertionError("Committed source write changed input/result evidence")
    expected_target = template["raw_input"].get("curr_target", {}) if template["is_success"] == 1 else initial_target
    _, target = request(config, "last_target", "verification")
    if semantic_hash(target) != semantic_hash(expected_target):
        raise AssertionError("Committed source writes changed previous-target semantics")
    return {
        "committed_records": len(writes),
        "detail_evidence_sha256": expected,
        "last_target_sha256": semantic_hash(target),
    }


def sample_rss(pid: int, stop: threading.Event, samples: list) -> None:
    """Sum live service and descendant RSS in each single sampling iteration."""
    import psutil

    parent = psutil.Process(pid)
    while not stop.is_set():
        try:
            processes = [parent, *parent.children(recursive=True)]
        except psutil.NoSuchProcess:
            break
        values = []
        for process in processes:
            try:
                values.append((process.pid, process.memory_info().rss))
            except psutil.NoSuchProcess:
                pass
        samples.append((time.monotonic(), sum(value for _, value in values), len(values) - 1))
        stop.wait(SAMPLE_SECONDS)


def memory_evidence(samples: list, requests: list) -> dict:
    """Do not add independent child high-water marks or invent unsampled peaks."""
    for request_sample in requests:
        window = [rss for when, rss, _ in samples if request_sample["start"] <= when <= request_sample["end"]]
        request_sample["sampled_combined_peak_bytes"] = max(window) if window else None
    return {
        "sampled_combined_process_peak_bytes": max((rss for _, rss, _ in samples), default=None),
        "sampled_max_children": max((children for _, _, children in samples), default=0),
        "sample_count": len(samples),
    }


def kill_group(process: subprocess.Popen) -> None:
    """Clean up only this benchmark's isolated process group."""
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=5)


def read_json_when_ready(path: Path, process: subprocess.Popen, deadline: float) -> dict:
    """Bound startup waits and fail promptly if the service exits."""
    while time.monotonic() < deadline:
        if path.exists():
            return json.loads(path.read_text())
        if process.poll() is not None:
            raise RuntimeError("HTTP service failed; inspect its isolated server.log")
        time.sleep(0.01)
    raise TimeoutError("HTTP service startup exceeded its hard deadline")


def observe(config: dict, directory: Path, mode: str) -> dict:
    """Run fresh database/service/client processes and an independent RSS observer."""
    directory.mkdir(mode=0o700)
    working = directory / "working.sqlite"
    with (
        closing(sqlite3.connect(Path(config["database"]).as_uri() + "?mode=ro", uri=True)) as source,
        closing(sqlite3.connect(working)) as destination,
    ):
        source.backup(destination)
    working.chmod(0o600)
    config = {
        **config,
        "database": str(working),
        "ready": str(directory / "ready.json"),
        "stop": str(directory / "stop"),
        "sources": str(directory / "sources.json"),
    }
    config_path = directory / "worker.json"
    config_path.write_text(json.dumps(config))
    settings = directory / "config.toml"
    settings.write_text(
        f'sqlalchemy_database_uri = "sqlite+aiosqlite:///{working}"\n'
        f'app_log_dir = {json.dumps(str(directory / "logs"))}\nenvironment = "local"\n'
    )
    environment = {key: value for key, value in os.environ.items() if not key.startswith(("AXILE_", "PYTHONPATH"))}
    environment.update(
        AXILE_CONFIG_TOML=str(settings),
        PYTHONPATH=config["root"],
        PYTHONHASHSEED="0",
        OMP_NUM_THREADS="1",
        OPENBLAS_NUM_THREADS="1",
    )
    command = [sys.executable, str(Path(__file__).resolve())]
    stop, samples, thread = threading.Event(), [], None
    with (directory / "server.log").open("w") as log:
        process = subprocess.Popen(
            [*command, "--server", str(config_path)],
            cwd=directory,
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        started = time.monotonic()
        deadline = started + config["timeout"]
        try:
            if mode == "rss":
                thread = threading.Thread(target=sample_rss, args=(process.pid, stop, samples), daemon=True)
                thread.start()
            config.update(read_json_when_ready(Path(config["ready"]), process, min(started + 20, deadline - 5)))
            startup_seconds = time.monotonic() - started
            config_path.write_text(json.dumps(config))
            result = subprocess.run(
                [*command, "--client", str(config_path)],
                cwd=directory,
                env=environment,
                capture_output=True,
                text=True,
                timeout=max(0.01, deadline - time.monotonic() - 3),
                check=False,
            )
            if result.returncode:
                (directory / "client.log").write_text(result.stderr)
                raise RuntimeError("HTTP workload failed; inspect its isolated client.log")
            measured = json.loads(result.stdout)
            idle_start = time.monotonic()
            stop.wait(min(0.1, max(0, deadline - idle_start - 3)))
            idle_end = time.monotonic()
            Path(config["stop"]).touch()
            process.wait(timeout=max(0.01, deadline - time.monotonic()))
            if process.returncode:
                raise RuntimeError("Service did not shut down cleanly")
        finally:
            kill_group(process)
            stop.set()
            if thread:
                thread.join(timeout=2)
    measured.update(startup_seconds=startup_seconds, sources=json.loads(Path(config["sources"]).read_text()))
    if mode == "rss":
        measured.update(memory_evidence(samples, measured["samples"]))
        idle = [rss for when, rss, _ in samples if idle_start <= when <= idle_end]
        measured["sampled_after_workload_median_bytes"] = statistics.median(idle) if idle else None
        if measured["sampled_max_children"]:
            raise AssertionError("Analysis-disabled service unexpectedly created a descendant process")
    return measured


def aggregate(runs: list[dict]) -> dict:
    """Keep latency samples separate from memory instrumentation and cold reads."""
    result = {}
    for arm in ("baseline", "candidate"):
        items = [run for run in runs if run["arm"] == arm]
        rows = [sample for run in items if run["mode"] == "timing" for sample in run["samples"]]
        grouped = {}
        for key in sorted({(row["phase"], row["endpoint"]) for row in rows}):
            selected = [row for row in rows if (row["phase"], row["endpoint"]) == key]
            latencies = sorted(row["seconds"] for row in selected)
            grouped["/".join(key)] = {
                "sample_count": len(selected),
                "p50_seconds": statistics.median(latencies),
                "p95_seconds": latencies[max(0, (95 * len(latencies) + 99) // 100 - 1)],
                "response_bytes": sorted({row["bytes"] for row in selected}),
            }
        result[arm] = {
            "http": grouped,
            "sampled_combined_process_peak_bytes": max(
                (run["sampled_combined_process_peak_bytes"] for run in items if run["mode"] == "rss"), default=None
            ),
            "rss_sample_count": sum(run.get("sample_count", 0) for run in items),
            "sampled_after_workload_median_bytes": [
                run["sampled_after_workload_median_bytes"] for run in items if run["mode"] == "rss"
            ],
        }
    return result


def compare(args) -> None:
    """Alternate arms over immutable semantic seed copies; never run migrations here."""
    args.output = args.output.resolve()
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    databases = {arm: getattr(args, arm + "_database").resolve() for arm in ("baseline", "candidate")}
    hashes = {arm: digest_file(path) for arm, path in databases.items()}
    harness_hash = digest_file(Path(__file__))
    oracle_path = Path(__file__).with_name("business_facts_contract.py")
    oracle_hash = digest_file(oracle_path)
    roots = {arm: getattr(args, arm).resolve() for arm in ("baseline", "candidate")}
    source_hashes = {arm: source_tree_hash(root) for arm, root in roots.items()}
    source_identities = {arm: source_identity(root) for arm, root in roots.items()}
    seed = fixture_manifest(args.fixture)
    runs = []
    expected = None
    for mode in args.modes:
        for iteration in range(args.runs):
            arms = ("baseline", "candidate") if iteration % 2 == 0 else ("candidate", "baseline")
            for arm in arms:
                config = {
                    "arm": arm,
                    "root": str(roots[arm]),
                    "source_tree_sha256": source_hashes[arm],
                    "database": str(databases[arm]),
                    "fixture": str(args.fixture.resolve()),
                    "fixture_sha256": seed["sha256"],
                    "account_id": args.account_id,
                    "record_id": args.record_id,
                    "repeats": args.repeats,
                    "writes": args.writes,
                    "mixed": args.mixed,
                    "timeout": args.timeout,
                }
                measured = observe(config, args.output / f"{mode}-{iteration}-{arm}", mode)
                semantic_evidence = {**measured["semantics"], "verified_writes": measured["verified_writes"]}
                if expected is not None and semantic_evidence != expected:
                    raise AssertionError("Baseline/candidate HTTP evidence semantics differ")
                expected = semantic_evidence
                runs.append({"arm": arm, "mode": mode, "iteration": iteration, **measured})
                (args.output / "runs.json").write_text(json.dumps(runs, indent=2))
    if hashes != {arm: digest_file(path) for arm, path in databases.items()}:
        raise AssertionError("Frozen source database changed")
    if source_hashes != {arm: source_tree_hash(root) for arm, root in roots.items()}:
        raise AssertionError("Application source files changed during the benchmark")
    if source_identities != {arm: source_identity(root) for arm, root in roots.items()}:
        raise AssertionError("Application source provenance changed during the benchmark")
    if fixture_manifest(args.fixture) != seed:
        raise AssertionError("Frozen fixture or manifest changed during the benchmark")
    if digest_file(Path(__file__)) != harness_hash:
        raise AssertionError("Benchmark harness changed during its execution")
    if digest_file(oracle_path) != oracle_hash:
        raise AssertionError("Business oracle changed during the benchmark")
    report = {
        "oracle_sha256": oracle_hash,
        "data_kind": "synthetic",
        "fixture": seed,
        "database_sha256": hashes,
        "application_source_sha256": source_hashes,
        "application_source_identity": source_identities,
        "harness_sha256": harness_hash,
        "summary": aggregate(runs),
        "semantic_hashes": expected,
        "measurement_scope": "actual complete FastAPI app, loopback HTTP, production terminal writer/last-target reader",
        "rss_scope": "simultaneous sampled service plus descendants; sibling client/observer excluded; shared pages may count twice; short peaks may be missed",
        "sample_seconds": SAMPLE_SECONDS,
        "native_analysis": "disabled equally",
        "limits": [
            "No trading/channel/scheduler/analysis lifecycle and no live account acceptance",
            "Cold is first use per endpoint in one fresh service; preceding endpoints may warm imports/DB; OS page cache is retained",
            "DB creation/migration excluded; full process peak includes app startup and untimed committed-evidence rereads",
            "Small repeat counts are descriptive and cannot establish production tail latency",
            "History payload reduction and complete detail reconstruction are separate scenarios",
            "The absolute worker deadline has a separate emergency cleanup allowance up to seven seconds",
        ],
    }
    (args.output / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


def main() -> None:
    """Dispatch isolated workers or an explicitly scheduled comparison."""
    if len(sys.argv) == 3 and sys.argv[1] in {"--server", "--client"}:
        config = json.loads(Path(sys.argv[2]).read_text())
        if sys.argv[1] == "--server":
            sys.path.insert(0, config["root"])
            with network_guard():
                asyncio.run(serve(config))
        else:
            with network_guard(config["port"]):
                print(json.dumps(run_client(config)))
        return
    parser = argparse.ArgumentParser(description=__doc__)
    for arm in ("baseline", "candidate"):
        parser.add_argument("--" + arm, type=Path, required=True)
        parser.add_argument("--" + arm + "-database", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--account-id", type=int, default=1)
    parser.add_argument("--record-id", type=int, default=1)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--modes", nargs="+", choices=("timing", "rss"), default=("timing", "rss"))
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--writes", type=int, default=3)
    parser.add_argument("--mixed", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=30, help="Absolute service/startup/workload/shutdown deadline")
    args = parser.parse_args()
    if min(args.runs, args.repeats, args.writes, args.mixed, args.timeout) < 1:
        parser.error("All workload counts and timeout must be positive")
    compare(args)


if __name__ == "__main__":
    main()
