"""国内常驻通道准备任务测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from axile.server.execution import ctp_channels
from tests.unit.server._runtime_db_support import runtime_database, runtime_rows


def test_register_china_channel_jobs_uses_fixed_session_windows() -> None:
    scheduler = SimpleNamespace(add_job=MagicMock())

    ctp_channels.register_china_channel_jobs(scheduler)

    calls = scheduler.add_job.call_args_list
    assert [(call.kwargs["id"], call.kwargs["hour"], call.kwargs["minute"]) for call in calls] == [
        (ctp_channels.CHINA_NIGHT_PREPARE_JOB_ID, 20, 30),
        (ctp_channels.CHINA_DAY_PREPARE_JOB_ID, 8, 30),
    ]
    assert all(call.kwargs["replace_existing"] is True for call in calls)
    assert all(call.kwargs["max_instances"] == 1 for call in calls)


def test_prepare_china_accounts_isolates_account_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    accounts = [1, 2]
    prepared: list[tuple[int | None, str | None]] = []

    class Manager:
        configure_catalog_account = MagicMock()

        async def prepare_account(self, account: object, expected: str | None = None) -> dict[str, object]:
            account_id = getattr(account, "id")
            prepared.append((account_id, expected))
            if account_id == 1:
                raise RuntimeError("offline")
            return {"trading_day": expected or ""}

    async def started() -> list[int]:
        return accounts

    monkeypatch.setattr(ctp_channels, "_started_china_channel_accounts", started)

    async def scenario():
        async with runtime_database(tmp_path / "runtime.db"):
            monkeypatch.setattr(ctp_channels, "get_worker_backend_manager", Manager)
            await ctp_channels.prepare_china_channel_accounts("night")

    asyncio.run(scenario())

    assert sorted(prepared) == [(1, None), (2, None)]


def test_started_china_scan_filters_disabled_and_other_channels(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with runtime_database(tmp_path / "runtime.db") as path:
            assert await ctp_channels._started_china_channel_accounts() == [1, 2]
            runtime_rows(path, "UPDATE account SET trade_channel='gm' WHERE id=2")
            assert await ctp_channels._started_china_channel_accounts() == [1]
            runtime_rows(
                path,
                "UPDATE account_settings SET schedule=json_set(schedule,'$.is_started',json('false')) WHERE account_id=1",
            )
            assert await ctp_channels._started_china_channel_accounts() == []

    asyncio.run(scenario())
