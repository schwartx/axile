"""显式启用的 CTP 仿真查询验收，不发送任何订单。"""

import json
import os
from pathlib import Path

import pytest

from axile.common.trade_channel import TradeChannel
from axile.executor.account_control.guard import AccountControlGuard
from axile.executor.account_control.presets import resolve_account_control_policy
from axile.executor.account_control.snapshot import AccountControlCounterSnapshot
from axile.executor.algorithms.utils.clock import clock_monotonic, get_default_clock
from axile.executor.ctp.ctp_execute import CTPExecutor
from axile.executor.instrument_catalog import InstrumentCatalog, LocalInstrumentCatalogProvider
from axile.executor.models.unified_input import CTPAccountConfig

pytestmark = [pytest.mark.live, pytest.mark.ctp]


@pytest.mark.skipif(os.getenv("AXILE_CTP_CATALOG_LIVE") != "1", reason="未显式启用 CTP 仿真目录验收")
def test_ctp_catalog_live_read_only(monkeypatch):
    config = CTPAccountConfig.model_validate(json.loads(Path(os.environ["AXILE_CTP_CATALOG_CONFIG"]).read_text()))
    catalog = InstrumentCatalog()
    provider = LocalInstrumentCatalogProvider(catalog)
    executor = CTPExecutor(TradeChannel.CTP)
    executor.account_config = config
    executor.set_instrument_catalog_provider(provider)
    executor.set_account_control_guard(
        AccountControlGuard(
            account_id=98,
            execution_id="catalog-read-only-acceptance",
            channel=TradeChannel.CTP,
            policy=resolve_account_control_policy("ctp"),
            baseline=AccountControlCounterSnapshot(),
        )
    )
    calls = []
    send = executor._call_trader_request

    def read_only(name, request, **kwargs):
        assert name not in {"ReqOrderInsert", "ReqOrderAction", "ReqExecOrderInsert", "ReqOptionSelfCloseInsert"}
        calls.append((name, str(getattr(request, "InstrumentID", "")), str(getattr(request, "ProductID", ""))))
        return send(name, request, **kwargs)

    monkeypatch.setattr(executor, "_call_trader_request", read_only)
    try:
        executor._initialize_connection(config)
        assert executor._verify_connection()
        assert not any(name == "ReqQryInstrument" and not symbol and not product for name, symbol, product in calls)
        # A product-scoped probe selects a current contract instead of assuming a fixed delivery month.
        from openctp_ctp import thosttraderapi as td

        request = td.CThostFtdcQryInstrumentField()
        request.ExchangeID, request.ProductID = "SHFE", "rb"
        rows = executor._query("ReqQryInstrument", request)
        valid = [row for row in rows if getattr(row, "IsTrading", False)]
        assert valid
        symbol = str(valid[0].InstrumentID)
        before = len(calls)
        record = provider.ensure([f"SHFE.{symbol}"])[f"SHFE.{symbol}"]
        assert record.multiplier > 0 and record.tick > 0
        assert calls[before:] and all(
            name != "ReqQryInstrument" or item == symbol for name, item, product in calls[before:]
        )
        before = len(calls)
        assert provider.ensure([f"SHFE.{symbol}"])[f"SHFE.{symbol}"] == record
        assert len(calls) == before
        print(
            json.dumps(
                {
                    "login": "passed",
                    "sample": record.key,
                    "targeted_query": "passed",
                    "cache_hit": "passed",
                    "orders": 0,
                }
            )
        )
        if os.getenv("AXILE_CTP_CATALOG_FULL") == "1":
            before_full = len(calls)
            task = catalog.refresh()
            deadline = clock_monotonic() + catalog.limits.refresh + 5
            while clock_monotonic() < deadline:
                status = catalog.task(task["task_id"])
                if status["status"] != "running":
                    print(json.dumps({"full_refresh": status}, ensure_ascii=False))
                    assert status["status"] == "succeeded"
                    assert status["records"] > 0
                    full_calls = calls[before_full:]
                    assert sum(name == "ReqQryInstrument" for name, _, _ in full_calls) == 1
                    break
                get_default_clock().sleep(0.1)
            else:
                pytest.fail("full refresh exceeded deadline")
    finally:
        executor.close()
        catalog.close()
