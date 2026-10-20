"""Native process bridge ownership, bounded output and configuration contracts."""

import asyncio
import os
import sys
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from axile.server import native_analysis as native


def executable(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "axile-analysis"
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(0o755)
    return path


def session_for(path: Path | str):
    return AsyncSession(create_async_engine(f"sqlite+aiosqlite:///{path}"))


@pytest.fixture(autouse=True)
def native_environment(monkeypatch):
    for name in tuple(os.environ):
        if name.startswith("AXILE_ANALYSIS_"):
            monkeypatch.delenv(name)


def test_query_keeps_response_bytes_and_sends_only_control_request(tmp_path, monkeypatch):
    response = b'{ "points": [1, 2], "unicode": "\\u4e2d" }\n'
    binary = executable(
        tmp_path,
        "import json, sys\n"
        "request = json.load(sys.stdin)\n"
        "assert sys.argv[1:] == ['query', '--database', request['expected_database']]\n"
        "assert request['command'] == 'performance' and request['account_id'] == 2\n"
        f"sys.stdout.buffer.write({response!r})\n",
    )
    database = tmp_path / "analysis.db"

    def reject_response_reconstruction(*args, **kwargs):
        raise AssertionError("query response must not be reconstructed")

    monkeypatch.setattr(native.json, "loads", reject_response_reconstruction)
    result = asyncio.run(
        native.NativeAnalysis(binary, database).query("performance", account_id=2, expected_database=str(database))
    )
    assert result == response


def test_explicit_binary_and_required_mode_fail_clearly(tmp_path, monkeypatch):
    session = session_for(":memory:")
    monkeypatch.setenv("AXILE_ANALYSIS_BINARY", str(tmp_path / "missing"))
    with pytest.raises(native.NativeAnalysisUnavailable) as caught:
        native.get_native_analysis(session)
    assert caught.value.code == "NATIVE_BINARY_MISSING"
    monkeypatch.delenv("AXILE_ANALYSIS_BINARY")
    monkeypatch.setattr(native, "_binary_path", lambda: None)
    monkeypatch.setenv("AXILE_ANALYSIS_MODE", "required")
    with pytest.raises(native.NativeAnalysisUnavailable):
        native.get_native_analysis(session)
    monkeypatch.setenv("AXILE_ANALYSIS_MODE", "python")
    assert native.get_native_analysis(session) is None


def test_auto_mode_memory_database_is_explicitly_unsupported_when_required(tmp_path, monkeypatch):
    monkeypatch.setattr(native, "_binary_path", lambda: tmp_path / "binary")
    session = session_for(":memory:")
    assert native.get_native_analysis(session) is None
    with pytest.raises(native.NativeAnalysisUnavailable) as caught:
        native.get_native_analysis(session, required=True)
    assert caught.value.code == "NATIVE_DATABASE_UNSUPPORTED"


def test_file_database_is_resolved_from_session_and_positive_limits_checked(tmp_path, monkeypatch):
    database = tmp_path / "database with space.db"
    database.touch()
    binary = executable(tmp_path, "pass\n")
    monkeypatch.setenv("AXILE_ANALYSIS_BINARY", str(binary))
    session = session_for(database)
    selected = native.get_native_analysis(session)
    assert selected is not None
    assert selected.database == database.resolve()
    assert selected.binary == binary.resolve()
    monkeypatch.setenv("AXILE_ANALYSIS_MAX_CONCURRENCY", "0")
    with pytest.raises(native.NativeAnalysisUnavailable) as caught:
        native.get_native_analysis(session)
    assert caught.value.code == "INVALID_NATIVE_CONFIG"


def test_sqlite_uri_memory_is_never_treated_as_a_file(tmp_path, monkeypatch):
    monkeypatch.setattr(native, "_binary_path", lambda: tmp_path / "binary")
    session = AsyncSession(create_async_engine("sqlite+aiosqlite:///file:shared?mode=memory&cache=shared&uri=true"))
    assert native.get_native_analysis(session) is None


def test_runtime_error_preserves_code_without_fallback(tmp_path):
    binary = executable(
        tmp_path,
        "import sys\n"
        "sys.stdin.buffer.read()\n"
        'sys.stderr.write(\'{"error":"expired snapshot","code":"SNAPSHOT_EXPIRED"}\')\n'
        "sys.exit(2)\n",
    )
    with pytest.raises(native.NativeAnalysisError) as caught:
        asyncio.run(native.NativeAnalysis(binary, tmp_path / "db").query("costs"))
    assert caught.value.code == "SNAPSHOT_EXPIRED"
    assert str(caught.value) == "expired snapshot"


def test_output_and_request_budgets(tmp_path):
    binary = executable(tmp_path, "import sys\nsys.stdin.buffer.read()\nsys.stdout.buffer.write(b'x' * 10000)\n")
    bridge = native.NativeAnalysis(binary, tmp_path / "db", max_output_bytes=100)
    with pytest.raises(native.NativeAnalysisProtocolError) as caught:
        asyncio.run(bridge.query("costs"))
    assert caught.value.code == "NATIVE_OUTPUT_LIMIT"
    with pytest.raises(native.NativeAnalysisProtocolError) as caught:
        asyncio.run(bridge.query("costs", data="x" * native._REQUEST_LIMIT))
    assert caught.value.code == "NATIVE_REQUEST_LIMIT"


def test_capabilities_are_small_decoded_metadata(tmp_path):
    binary = executable(
        tmp_path,
        "import sys\nassert sys.argv[1:] == ['capabilities']\n"
        'sys.stdout.write(\'{"protocol":1,"commands":["costs"]}\')\n',
    )
    assert asyncio.run(native.NativeAnalysis(binary, tmp_path / "db").capabilities()) == {
        "protocol": 1,
        "commands": ["costs"],
    }


@pytest.mark.parametrize("cancel", [False, True], ids=["timeout", "cancel"])
def test_cancel_and_timeout_kill_and_reap_child(tmp_path, monkeypatch, cancel):
    binary = executable(
        tmp_path,
        "import asyncio, sys\nsys.stdin.buffer.read()\nasyncio.run(asyncio.sleep(10))\n",
    )
    spawned = []
    create = asyncio.create_subprocess_exec

    async def track(*args, **kwargs):
        process = await create(*args, **kwargs)
        spawned.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", track)

    async def check():
        bridge = native.NativeAnalysis(binary, tmp_path / "db", timeout=1 if cancel else 0.1)
        task = asyncio.create_task(bridge.query("performance"))
        if cancel:
            while not spawned:
                await asyncio.sleep(0.001)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(native.NativeAnalysisTimeout):
                await task
        assert len(spawned) == 1
        assert spawned[0].returncode is not None
        assert await spawned[0].wait() != 0

    asyncio.run(check())


def test_concurrent_requests_share_process_budget_and_queued_cancel_never_spawns(tmp_path, monkeypatch):
    binary = executable(
        tmp_path,
        "import asyncio, sys\nsys.stdin.buffer.read()\nasyncio.run(asyncio.sleep(0.05))\nsys.stdout.write('{}')\n",
    )
    create = asyncio.create_subprocess_exec
    active = 0
    peak = 0
    started = asyncio.Event()
    spawned = []

    async def track(*args, **kwargs):
        nonlocal active, peak
        process = await create(*args, **kwargs)
        spawned.append(process)
        active += 1
        peak = max(peak, active)
        started.set()
        original_wait = process.wait
        counted = False

        async def wait():
            nonlocal active, counted
            result = await original_wait()
            if not counted:
                active -= 1
                counted = True
            return result

        monkeypatch.setattr(process, "wait", wait)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", track)

    async def check():
        bridges = [native.NativeAnalysis(binary, tmp_path / "db", max_concurrency=1) for _ in range(3)]
        first = asyncio.create_task(bridges[0].query("costs"))
        await started.wait()
        queued = asyncio.create_task(bridges[1].query("costs"))
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        final = asyncio.create_task(bridges[2].query("costs"))
        assert await first == await final == b"{}"
        assert len(spawned) == 2
        assert active == 0 and peak == 1

    asyncio.run(check())


def test_invalid_capabilities_are_protocol_failure(tmp_path):
    binary = executable(tmp_path, "import sys\nsys.stdout.write('not JSON')\n")
    with pytest.raises(native.NativeAnalysisProtocolError) as caught:
        asyncio.run(native.NativeAnalysis(binary, tmp_path / "db").capabilities())
    assert caught.value.code == "INVALID_NATIVE_CAPABILITIES"


def test_empty_explicit_binary_never_selects_an_implicit_binary(tmp_path, monkeypatch):
    monkeypatch.setenv("AXILE_ANALYSIS_BINARY", "")
    with pytest.raises(native.NativeAnalysisUnavailable) as caught:
        native.get_native_analysis(session_for(":memory:"))
    assert caught.value.code == "NATIVE_BINARY_MISSING"


def test_non_sqlite_database_is_not_dispatched_to_rust(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from sqlalchemy.engine import make_url

    monkeypatch.setattr(native, "_binary_path", lambda: tmp_path / "binary")
    engine = SimpleNamespace(url=make_url("postgresql://user:secret@localhost/db"))
    session = SimpleNamespace(get_bind=lambda: SimpleNamespace(engine=engine))
    assert native.get_native_analysis(session) is None
    with pytest.raises(native.NativeAnalysisUnavailable) as caught:
        native.get_native_analysis(session, required=True)
    assert caught.value.code == "NATIVE_DATABASE_UNSUPPORTED"
    assert "secret" not in str(caught.value)


def test_uncommitted_orm_write_never_starts_a_native_read(tmp_path, monkeypatch):
    from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

    class Base(DeclarativeBase):
        pass

    class Fact(Base):
        __tablename__ = "bridge_test_fact"
        id: Mapped[int] = mapped_column(primary_key=True)

    database = tmp_path / "facts.db"
    database.touch()
    monkeypatch.setattr(native, "_binary_path", lambda: tmp_path / "binary")
    session = session_for(database)
    session.add(Fact(id=1))
    with pytest.raises(native.NativeAnalysisUnavailable) as caught:
        native.get_native_analysis(session)
    assert caught.value.code == "UNCOMMITTED_NATIVE_INPUT"


def stream_executable(tmp_path: Path, output: bytes, *, status: int = 0, stderr: bytes = b"") -> Path:
    return executable(
        tmp_path,
        "import sys\nsys.stdin.buffer.read()\n"
        f"sys.stdout.buffer.write({output!r})\nsys.stdout.buffer.flush()\n"
        f"sys.stderr.buffer.write({stderr!r})\nsys.exit({status})\n",
    )


async def streamed(bridge):
    async with bridge.query_stream("publish", account_id=7) as frames:
        return [frame async for frame in frames]


def test_stream_yields_frames_before_child_finishes_and_never_buffers_stdout(tmp_path, monkeypatch):
    binary = executable(
        tmp_path,
        "import asyncio, sys\nsys.stdin.buffer.read()\n"
        'sys.stdout.write(\'{"type":"rows","rows":[[1]]}\\n\')\nsys.stdout.flush()\n'
        "asyncio.run(asyncio.sleep(0.1))\n"
        'sys.stdout.write(\'{"type":"complete","source_version":1}\\n\')\n',
    )
    spawned = []
    create = asyncio.create_subprocess_exec
    read_limited = native._read_limited

    async def track(*args, **kwargs):
        process = await create(*args, **kwargs)
        spawned.append(process)
        return process

    async def forbid_stdout(stream, limit):
        assert stream is spawned[0].stderr
        return await read_limited(stream, limit)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", track)
    monkeypatch.setattr(native, "_read_limited", forbid_stdout)

    async def check():
        bridge = native.NativeAnalysis(binary, tmp_path / "db")
        async with bridge.query_stream("publish") as frames:
            first = await anext(frames)
            assert first == {"type": "rows", "rows": [[1]]}
            assert spawned[0].returncode is None
            remaining = [frame async for frame in frames]
            assert remaining == [{"type": "complete", "source_version": 1}]
        assert spawned[0].returncode == 0

    asyncio.run(check())


@pytest.mark.parametrize(
    ("output", "code"),
    [
        (b'{"type":"rows"}\n', "NATIVE_INCOMPLETE_STREAM"),
        (b'{"type":"complete"}\n{"type":"rows"}\n', "NATIVE_TRAILING_FRAME"),
        (b'{"type":"complete"}', "NATIVE_UNTERMINATED_FRAME"),
        (b"not-json\n", "NATIVE_INVALID_FRAME"),
        (b"[]\n", "NATIVE_INVALID_FRAME"),
        (b"\n", "NATIVE_INVALID_FRAME"),
        (b'{"type":"rows","bad":NaN}\n', "NATIVE_INVALID_FRAME"),
        (b"", "NATIVE_INCOMPLETE_STREAM"),
    ],
)
def test_stream_rejects_invalid_or_incomplete_protocol(tmp_path, output, code):
    binary = stream_executable(tmp_path, output)
    with pytest.raises(native.NativeAnalysisProtocolError) as caught:
        asyncio.run(streamed(native.NativeAnalysis(binary, tmp_path / "db")))
    assert caught.value.code == code


def test_stream_nonzero_exit_after_complete_never_reports_success(tmp_path):
    binary = stream_executable(
        tmp_path,
        b'{"type":"rows","rows":[[1]]}\n{"type":"complete"}\n',
        status=2,
        stderr=b'{"error":"source changed","code":"SOURCE_VERSION_CHANGED"}',
    )
    with pytest.raises(native.NativeAnalysisError) as caught:
        asyncio.run(streamed(native.NativeAnalysis(binary, tmp_path / "db")))
    assert caught.value.code == "SOURCE_VERSION_CHANGED"


@pytest.mark.parametrize(
    ("frame_budget", "total_budget", "code"), [(10, 1000, "NATIVE_FRAME_LIMIT"), (1000, 20, "NATIVE_OUTPUT_LIMIT")]
)
def test_stream_checks_frame_and_total_byte_budgets(tmp_path, frame_budget, total_budget, code):
    binary = stream_executable(tmp_path, b'{"type":"rows","rows":[[1]]}\n{"type":"complete"}\n')
    bridge = native.NativeAnalysis(binary, tmp_path / "db", max_frame_bytes=frame_budget, max_output_bytes=total_budget)
    with pytest.raises(native.NativeAnalysisProtocolError) as caught:
        asyncio.run(streamed(bridge))
    assert caught.value.code == code


@pytest.mark.parametrize("action", ["break", "cancel", "timeout"])
def test_stream_early_exit_cancel_and_timeout_reap_child_and_release_gate(tmp_path, monkeypatch, action):
    binary = executable(
        tmp_path,
        "import asyncio, sys\nsys.stdin.buffer.read()\n"
        'sys.stdout.write(\'{"type":"rows"}\\n\')\nsys.stdout.flush()\n'
        "asyncio.run(asyncio.sleep(10))\n",
    )
    children = []
    create = asyncio.create_subprocess_exec

    async def track(*args, **kwargs):
        process = await create(*args, **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", track)

    async def check():
        bridge = native.NativeAnalysis(
            binary, tmp_path / "db", timeout=0.1 if action == "timeout" else 2, max_concurrency=1
        )
        reached = asyncio.Event()

        async def consume():
            async with bridge.query_stream("publish") as frames:
                async for frame in frames:
                    assert frame == {"type": "rows"}
                    reached.set()
                    if action == "break":
                        break

        task = asyncio.create_task(consume())
        if action != "timeout":
            await reached.wait()
        if action == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            expected = native.NativeAnalysisTimeout if action == "timeout" else native.NativeAnalysisProtocolError
            with pytest.raises(expected):
                await task
        assert children[0].returncode is not None
        assert await children[0].wait() != 0
        # Cancellation/error must release the shared process slot for the next request.
        async with native._gate(1):
            pass

    asyncio.run(check())


def test_stream_rejects_unbounded_stderr_without_waiting_for_stdout(tmp_path):
    binary = executable(
        tmp_path,
        "import asyncio, sys\nsys.stdin.buffer.read()\n"
        "sys.stderr.buffer.write(b'x'*100000)\nsys.stderr.flush()\nasyncio.run(asyncio.sleep(10))\n",
    )
    bridge = native.NativeAnalysis(binary, tmp_path / "db", timeout=2)
    with pytest.raises(native.NativeAnalysisProtocolError) as caught:
        asyncio.run(streamed(bridge))
    assert caught.value.code == "NATIVE_OUTPUT_LIMIT"
