"""Measure server-only analysis/HTTP RSS with a separate validation client process."""

import asyncio
import hashlib
import json
import resource
import socket
import statistics
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import benchmark_analysis_memory as lifecycle
from scripts.benchmark_performance_frames import emit


async def client(port):
    """Keep response decoding and comparison allocations outside the server process."""
    import httpx

    hashes = []
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False) as connection:
        identity = None
        started = time.monotonic()
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
            response = await connection.get(path)
            if response.status_code != 200:
                raise ValueError("HTTP status " + str(response.status_code))
            payload = response.json()
            if "snapshot_id" in payload:
                identity = payload["snapshot_id"]
                if "status" in payload and payload["status"] != "ready":
                    raise ValueError("Snapshot not ready")
            payload = {key: value for key, value in payload.items() if key not in ("snapshot_id", "computed_at")}
            data = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
            hashes.append(hashlib.sha256(data).hexdigest())
        seconds = time.monotonic() - started
    print(
        json.dumps(
            dict(
                hashes=hashes,
                http_seconds=seconds,
                peak_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                * (1 if sys.platform == "darwin" else 1024),
            )
        )
    )


async def worker(args):
    """Run actual analysis and loopback HTTP without starting trading lifecycle."""
    import psutil

    process = psutil.Process()

    def mark(name, **values):
        emit(name, rss_bytes=process.memory_info().rss, **values)

    mark("boot")
    import uvicorn

    from axile.server import performance_analysis as service
    from axile.server.app import _app
    from axile.server.core.db import SessionLocal, engine

    if not Path(service.__file__).resolve().is_relative_to(args.root.resolve()):
        raise ValueError("Wrong source tree")
    manager = service.AnalysisManager(SessionLocal)
    _app.state.analysis_manager = manager
    original_load, original_compute, original_publish = manager._load, service.compute_batch, manager._publish

    async def load(*values):
        mark("load_start")
        result = await original_load(*values)
        mark("load_end")
        return result

    def compute(*values):
        mark("compute_start")
        result = original_compute(*values)
        mark("compute_end")
        return result

    async def publish(*values):
        mark("publish_start")
        result = await original_publish(*values)
        mark("publish_end")
        return result

    manager._load, service.compute_batch, manager._publish = load, compute, publish
    mark("imported", source=str(Path(service.__file__).resolve()))
    server, serving, validation, listener = None, None, None, None
    try:
        mark("worker_start")
        async with SessionLocal() as session:
            await service.enqueue(session, 2, True)
        assert await manager.run_once()
        mark("worker_end")
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        listener.setblocking(False)
        server = uvicorn.Server(
            uvicorn.Config(
                _app,
                host="127.0.0.1",
                port=listener.getsockname()[1],
                loop="asyncio",
                lifespan="off",
                log_level="warning",
                access_log=False,
            )
        )
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        async with asyncio.timeout(5):
            while not server.started:
                if serving.done():
                    await serving
                    raise RuntimeError("Server stopped before readiness")
                await asyncio.sleep(0.01)
        mark("api_start")
        validation = await asyncio.create_subprocess_exec(
            sys.executable,
            str(Path(__file__).resolve()),
            "--client",
            str(listener.getsockname()[1]),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        output, _ = await asyncio.wait_for(validation.communicate(), 15)
        if validation.returncode:
            raise RuntimeError("Validation client failed: " + str(validation.returncode))
        result = json.loads(output)
        mark("api_end", http_seconds=result["http_seconds"])
        samples = []
        for _ in range(10):
            await asyncio.sleep(0.1)
            samples.append(process.memory_info().rss)
        mark(
            "done",
            peak_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == "darwin" else 1024),
            steady_bytes=statistics.median(samples),
            hashes=result["hashes"],
            client_peak_bytes=result["peak_bytes"],
        )
    finally:
        if validation is not None and validation.returncode is None:
            validation.kill()
            await validation.wait()
        if server is not None:
            server.should_exit = True
        if serving is not None:
            await asyncio.wait_for(serving, 5)
        if listener is not None:
            listener.close()
        await engine.dispose()


def main():
    """Reuse alternating-process comparison, database hashes and private copies."""
    if len(sys.argv) == 3 and sys.argv[1] == "--client":
        asyncio.run(client(int(sys.argv[2])))
    elif len(sys.argv) == 5 and sys.argv[4] == "--worker":
        args = SimpleNamespace(root=Path(sys.argv[1]))
        with patch.object(socket.socket, "connect", side_effect=AssertionError("Outbound connections forbidden")):
            asyncio.run(worker(args))
    else:
        lifecycle.main(worker_script=Path(__file__).resolve(), transport="http-external-client")


if __name__ == "__main__":
    main()
