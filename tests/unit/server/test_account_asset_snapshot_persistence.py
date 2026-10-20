"""执行记录与账户资产快照原子持久化测试."""

from __future__ import annotations

import asyncio
from types import TracebackType
from unittest.mock import AsyncMock

import pytest

from axile.server.db.models import AccountAssetSnapshot, ExecuteRecord
from axile.server.execution import records as execution_records
from axile.server.execution.records import append_success_execute_record
from tests.unit.server._execution_test_support import build_account


class _Session:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.commits = 0

    async def __aenter__(self) -> "_Session":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    def add(self, value: object) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, _value: object) -> None:
        return None


def test_success_record_persists_matching_asset_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(execution_records, "write_observation_fact", AsyncMock())
    session = _Session()
    account = build_account(id=51)
    result = {
        "account_assets": {
            "available_cash": 700.0,
            "total_asset": 1000.0,
            "market_value": 300.0,
            "positions": [],
        }
    }

    asyncio.run(
        append_success_execute_record(
            account,
            raw_input={},
            result=result,
            execution_id="exec-assets-51",
            session_factory=lambda: session,  # pyright: ignore[reportArgumentType]
        )
    )

    record = next(item for item in session.added if isinstance(item, ExecuteRecord))
    snapshot = next(item for item in session.added if isinstance(item, AccountAssetSnapshot))
    assert snapshot.account_id == 51
    assert snapshot.execution_id == "exec-assets-51"
    assert snapshot.source == "execution"
    assert snapshot.created_at == record.created_at
    assert snapshot.assets["total_asset"] == 1000.0
    assert session.commits == 1
