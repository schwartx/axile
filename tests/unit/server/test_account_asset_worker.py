"""账户资产查询的 worker 协议测试."""

from __future__ import annotations

import pytest

from axile.executor.models.unified_account_assets import UnifiedAccountAssets
from axile.server.execution.worker_backend import worker as worker_entry
from axile.server.execution.worker_backend.protocol import (
    WorkerBackendRequest,
)
from axile.server.execution.worker_backend.worker_state import _WorkerBackendState
from tests.unit.server._execution_test_support import build_account


class _Executor:
    def get_account_assets(self) -> UnifiedAccountAssets:
        return UnifiedAccountAssets(
            available_cash=750.0,
            total_asset=1000.0,
            market_value=250.0,
            positions=[],
        )


def test_worker_get_account_assets_command_returns_unified_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    account = build_account(id=61)
    request = WorkerBackendRequest(
        request_id="asset-request",
        command="get_account_assets",
        account_payload=account.model_dump(mode="json"),
        execution_id=None,
        payload={},
    )
    monkeypatch.setattr(worker_entry, "_resolve_prepared_executor", lambda **_kwargs: _Executor())
    monkeypatch.setattr(worker_entry, "_finalize_executor", lambda _executor: None)

    response = worker_entry._handle_worker_request(request, _WorkerBackendState())

    assert response.kind == "result"
    assert response.output_payload is not None
    assert response.output_payload["total_asset"] == 1000.0
