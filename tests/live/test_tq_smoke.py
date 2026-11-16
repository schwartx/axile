"""需要真实天勤凭据的显式启用 smoke test."""

from __future__ import annotations

import os

import pytest

from axile.common.serialization import from_payload
from axile.executor.models.unified_input import TQAccountConfig
from axile.executor.tq import TQExecutor

pytestmark = pytest.mark.live


@pytest.mark.skipif(os.getenv("AXILE_TQ_LIVE_SMOKE") != "1", reason="未启用天勤 live smoke")
def test_tq_account_can_connect_and_query_assets() -> None:
    mode = os.getenv("AXILE_TQ_ACCOUNT_MODE", "kq")
    payload: dict[str, object] = {
        "account_mode": mode,
        "tq_username": os.environ["AXILE_TQ_USERNAME"],
        "tq_password": os.environ["AXILE_TQ_PASSWORD"],
    }
    if mode == "live":
        payload.update(
            broker_name=os.environ["AXILE_TQ_BROKER_NAME"],
            account_id=os.environ["AXILE_TQ_ACCOUNT_ID"],
            account_password=os.environ["AXILE_TQ_ACCOUNT_PASSWORD"],
        )
    elif mode == "sim":
        payload["initial_balance"] = float(os.getenv("AXILE_TQ_INITIAL_BALANCE", "10000000"))

    executor = TQExecutor(from_payload(TQAccountConfig, payload))
    try:
        assets = executor.get_account_assets()
        assert assets.source == "real"
        assert assets.total_asset >= 0
    finally:
        executor.close()
