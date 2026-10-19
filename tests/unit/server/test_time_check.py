"""时间诊断的协议、告警恢复、持久化与生命周期契约。"""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from axile.server.db.models.time_check import TimeCheckSettingsRow, TimeCheckStateRow
from axile.server.time_check import probes
from axile.server.time_check import service as runtime
from axile.server.time_check.models import TimeCheckSettings


def packet(t1=1700000000.0, t2=1700000000.02, t3=1700000000.03):
    """构造关联的标准服务器应答。"""
    data = bytearray(48)
    data[0], data[1] = 0x24, 2
    data[24:32] = probes._stamp(t1)
    data[32:40] = probes._stamp(t2)
    data[40:48] = probes._stamp(t3)
    return bytes(data)


def test_ntp_four_timestamps_and_era():
    for t1 in (1700000000.0, 2200000000.0):
        result = probes.decode_sample(packet(t1, t1 - 0.08, t1 - 0.07), probes._stamp(t1), t1, t1 + 0.05, 0.05)
        assert result["offset_ms"] == pytest.approx(100, abs=0.001)
        assert result["delay_ms"] == pytest.approx(40, abs=0.001)


@pytest.mark.parametrize(
    "mutation", ["short", "origin", "unsynced", "mode", "version", "zero", "order", "refused", "jump"]
)
def test_invalid_ntp_is_rejected(mutation):
    data = bytearray(packet())
    if mutation == "short":
        data = data[:30]
    elif mutation == "origin":
        data[24:32] = bytes(8)
    elif mutation == "unsynced":
        data[0] |= 0xC0
    elif mutation == "mode":
        data[0] = 0x23
    elif mutation == "version":
        data[0] = 0x04
    elif mutation == "zero":
        data[32:40] = bytes(8)
    elif mutation == "order":
        data[40:48] = probes._stamp(1699999999)
    elif mutation == "refused":
        data[1] = 0
        data[12:16] = b"RATE"
    with pytest.raises((ValueError, PermissionError)):
        probes.decode_sample(
            bytes(data), probes._stamp(1700000000), 1700000000, 1700000000.05, 1 if mutation == "jump" else 0.05
        )


@pytest.mark.parametrize("host", ["localhost", "192.168.1.1", "::1", "ntp.example.com"])
def test_custom_source(host):
    config = TimeCheckSettings(source="custom", custom_host=host, custom_port=8123)
    assert (config.host, config.port) == (host, 8123)
    assert TimeCheckSettings().host == "ntp.aliyun.com"


@pytest.mark.parametrize(
    "patch",
    [
        {"source": "custom"},
        {"source": "custom", "custom_host": "https://ntp.example.com"},
        {"custom_host": "a;reboot"},
        {"custom_port": 0},
        {"threshold_ms": 0},
        {"cron_expr": ""},
        {"cron_expr": "invalid"},
    ],
)
def test_invalid_config(patch):
    with pytest.raises(ValidationError):
        TimeCheckSettings(**patch)


def test_alerts_preserve_unverified_offset_and_recover_independently():
    alerts = runtime.update_alerts(
        {"system": {"message": "旧系统校时告警"}}, {"status": "ok", "offset_ms": 501}, 500, "first"
    )
    assert set(alerts) == {"offset"}
    alerts = runtime.update_alerts(alerts, {"status": "error", "error": "timeout"}, 500, "second")
    assert set(alerts) == {"offset", "reference"}
    assert alerts["offset"]["checked_at"] == "first"
    assert runtime.update_alerts(alerts, {"status": "ok", "offset_ms": -500}, 500, "third") == {}


@asynccontextmanager
async def database(path: Path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async with engine.begin() as connection:
        await connection.run_sync(lambda conn: TimeCheckSettingsRow.__table__.create(conn))
        await connection.run_sync(lambda conn: TimeCheckStateRow.__table__.create(conn))
    monkeypatch.setattr(runtime, "SessionLocal", async_sessionmaker(engine, expire_on_commit=False))
    sched = MagicMock()
    sched.get_job.return_value = None
    monkeypatch.setattr(runtime, "scheduler", sched)
    try:
        yield sched
    finally:
        await engine.dispose()


def test_new_install_once_old_upgrade_and_restart(tmp_path, monkeypatch):
    async def scenario():
        async with database(tmp_path / "time.db", monkeypatch):
            ntp = AsyncMock(return_value={"status": "ok", "offset_ms": 0, "delay_ms": 1})
            monkeypatch.setattr(runtime, "ntp_probe", ntp)
            old = runtime.TimeCheckService()
            await old.start()
            assert old.task is None
            assert old.snapshot()["status"] == "disabled"
            fresh = runtime.TimeCheckService()
            await fresh.start(first_run=True)
            await fresh.task
            assert ntp.await_count == 1
            restart = runtime.TimeCheckService()
            await restart.start(first_run=True)
            assert restart.task is None
            assert restart.state["result"]["trigger"] == "first"
            assert ntp.await_count == 1

    asyncio.run(scenario())


def test_overlap_config_conflict_scheduler_and_snapshot(tmp_path, monkeypatch):
    async def scenario():
        async with database(tmp_path / "time.db", monkeypatch) as sched:
            entered, release = asyncio.Event(), asyncio.Event()

            async def ntp(*args):
                entered.set()
                await release.wait()
                return {"status": "ok", "offset_ms": 700, "delay_ms": 1}

            monkeypatch.setattr(runtime, "ntp_probe", ntp)
            service = runtime.TimeCheckService()
            await service.start()
            frames = service.frames()
            assert (await anext(frames))["data"]["status"] == "disabled"
            await asyncio.gather(service.run("manual"), service.run("automatic"))
            original = service.task
            await entered.wait()
            await service.save(TimeCheckSettings(enabled=True, source="tencent"))
            sched.add_job.assert_called_once()
            with pytest.raises(ValueError, match="其他窗口"):
                await service.save(TimeCheckSettings())
            release.set()
            await original
            assert service.state["result"]["host"] == "ntp.aliyun.com"
            assert service.state["result"]["settings"]["version"] == 0
            assert service.settings.version == 1
            assert service.snapshot()["status"] == "warning"
            await service.save(service.settings.model_copy(update={"enabled": False}))
            assert service.snapshot()["status"] == "warning"
            assert (await anext(frames))["version"] > 0
            await frames.aclose()
            assert not service.subscribers

    asyncio.run(scenario())


def test_schedule_preview_is_future_and_multiple_rules():
    times = runtime.preview("0 * * * * | 30 * * * *")
    assert len(times) == 5
    assert times == sorted(set(times))
    assert all(value.endswith("+08:00") for value in times)


def test_route_validation_and_no_probe_on_reads(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from axile.server.api.routes import time_check as routes

    async def scenario():
        async with database(tmp_path / "routes.db", monkeypatch):
            service = runtime.TimeCheckService()
            await service.start()
            monkeypatch.setattr(routes, "service", service)
            spy = AsyncMock()
            monkeypatch.setattr(runtime, "ntp_probe", spy)
            app = FastAPI()
            app.include_router(routes.router)
            with TestClient(app) as client:
                assert client.get("/system/time-check").json()["status"] == "disabled"
                saved = client.put("/system/time-check/settings", json={"enabled": True})
                assert saved.status_code == 200
                assert saved.json()["settings"]["version"] == 1
                assert client.put("/system/time-check/settings", json={}).status_code == 409
                assert client.put("/system/time-check/settings", json={"threshold_ms": 0}).status_code == 422
                assert (
                    client.post("/system/time-check/schedule-preview", json={"cron_expr": "0 * * * *"}).status_code
                    == 200
                )
                assert client.post("/system/time-check/schedule-preview", json={"cron_expr": "bad"}).status_code == 422
                spy.assert_not_called()

    asyncio.run(scenario())


def test_shutdown_cancellation_recovers_as_warning(tmp_path, monkeypatch):
    async def scenario():
        async with database(tmp_path / "cancel.db", monkeypatch):
            entered = asyncio.Event()

            async def waiting(*args):
                entered.set()
                await asyncio.Event().wait()

            monkeypatch.setattr(runtime, "ntp_probe", waiting)
            service = runtime.TimeCheckService()
            await service.start()
            await service.run("manual")
            await entered.wait()
            await service.close()
            restored = runtime.TimeCheckService()
            await restored.start()
            assert not restored.state["running"]
            assert "interrupted" in restored.state["alerts"]
            assert restored.snapshot()["status"] == "warning"

    asyncio.run(scenario())


def test_time_check_migration_roundtrip():
    import importlib.util

    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    path = Path(__file__).parents[3] / "axile/server/alembic/versions/0031_time_check.py"
    spec = importlib.util.spec_from_file_location("time_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        module.op = Operations(MigrationContext.configure(connection))
        module.upgrade()
        assert set(sa.inspect(connection).get_table_names()) == {"time_check_settings", "time_check_state"}
        assert connection.execute(sa.text("SELECT count(*) FROM time_check_state")).scalar() == 0
        module.downgrade()
        assert sa.inspect(connection).get_table_names() == []
    engine.dispose()


def test_udp_wire_request_and_response():
    """本地假 NTP 服务验证真实 UDP 编码、关联及偏差方向。"""
    import socket
    import time

    from axile.executor.algorithms.utils.clock import get_default_clock

    class Server(asyncio.DatagramProtocol):
        def connection_made(self, transport):
            self.transport = transport

        def datagram_received(self, data, address):
            assert len(data) == 48 and data[0] == 0x23
            now = get_default_clock().time() + 0.3
            reply = bytearray(packet(now, now, now))
            reply[24:32] = data[40:48]
            self.transport.sendto(bytes(reply), address)

    async def scenario():
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(Server, local_addr=("127.0.0.1", 0))
        try:
            result = await asyncio.wait_for(
                probes._sample(
                    transport.get_extra_info("sockname"), socket.AF_INET, get_default_clock(), time.monotonic
                ),
                1,
            )
            assert result["offset_ms"] == pytest.approx(-300, abs=10)
            assert result["delay_ms"] >= 0
        finally:
            transport.close()

    asyncio.run(scenario())


def test_sampling_selects_lowest_delay_without_source_fallback(monkeypatch):
    async def scenario():
        sample = AsyncMock(
            side_effect=[
                {"offset_ms": 20, "delay_ms": 50},
                {"offset_ms": 10, "delay_ms": 5},
                {"offset_ms": 30, "delay_ms": 30},
            ]
        )
        monkeypatch.setattr(probes, "_sample", sample)
        result = await probes.ntp_probe("127.0.0.1", 123)
        assert result["status"] == "ok"
        assert result["samples"] == 3
        assert result["offset_ms"] == 10
        assert all(call.args[0] == ("127.0.0.1", 123) for call in sample.await_args_list)

    asyncio.run(scenario())
