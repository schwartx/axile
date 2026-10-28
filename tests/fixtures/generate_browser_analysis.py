"""Regenerate sanitized WASM parity fixtures from the former native engine."""

import copy
import json
import tempfile
from pathlib import Path

from axile.server._native import _core
from tests.conftest import initialize_native_database, native_store_call
from tests.integration.server.test_native_analysis_regressions import _RESULT
from tests.unit.server._execution_test_support import build_account
from tests.unit.server._native_read_views import persist_record_fixture


def generate() -> dict:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "fixture.sqlite"
        initialize_native_database(path, revision="0036")
        account = build_account(trade_channel="tq", portfolio_id=None, backtest_fee_rate=0.0002)
        native_store_call(path, "account_create", {"account": account.model_dump(mode="json"), "now": "2026-01-01"})
        for index, (day, price) in enumerate((("2026-01-01", 100), ("2026-02-01", 110), ("2026-04-01", 121))):
            result = copy.deepcopy(_RESULT)
            result["account_assets"]["total_asset"] = price
            result["account_assets"]["positions"] = [
                {"symbol": "A", "volume": 1, "direction": "long", "market_value": price}
            ]
            symbol = result["symbol_results"]["A"]
            symbol["first_tick"].update(bid_price=price - 1, ask_price=price + 1, last_price=price)
            symbol["sizing"].update(equity=price, reference_price=price, unit_notional=price)
            symbol["trades"][0].update(
                trade_id=f"trade-{index}", trade_time=f"{day}T09:00:00.000006+08:00", trade_price=price
            )
            symbol["trades"][0]["extra"].update(commission=-0.01 if index == 0 else 0.2)
            persist_record_fixture(
                path,
                {
                    "account_id": 1,
                    "execution_id": f"fixture-{index}",
                    "created_at": f"{day}T09:00:00.000006+08:00",
                    "is_success": 1,
                    "input_id": f"input-{index}",
                    "result_input_id": f"result-{index}",
                    "raw_input": {"curr_target": {"A": 1.0}},
                    "raw_result": result,
                },
            )
        _core.queue_enqueue(str(path), 1, False)
        claim = _core.queue_claim(str(path), 1.0, "native-fixture")
        native_store_call(
            path,
            "publish_store",
            {
                "command": "publish_store",
                "protocol": 1,
                "account_id": 1,
                "expected_source_version": claim["source_version"],
                "snapshot_id": "native-fixture",
                "claim_token": claim["running_token"],
                "logic_version": "fixture-build",
                "engine_version": "equity-ledger-1",
                "computed_at": "2026-04-02T00:00:00Z",
                "settings": {"backtest_weight_type": "cs", "backtest_fee_rate": 0.0002},
                "trade_channel": "tq",
            },
        )
        store = _core.Store(str(path))
        charts = {key: json.loads(_core.chart(store, 1, "native-fixture", key)) for key in ("30", "90", "all")}
        detail = json.loads(_core.execution_detail(store, 1, "native-fixture", 1, 200, 0))
        costs = json.loads(
            _core.query_costs(
                store,
                1,
                "native-fixture",
                "all",
                "trade",
                "time",
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                1,
                "fixture",
                None,
                "fixture-build",
                "equity-ledger-1",
            )
        )
        del store
        _core.close_database(str(path))
        initialize_native_database(path)
        state = json.loads(
            _core.browser_analysis_state(str(path), json.dumps({"account_id": 1, "instance_id": "fixture"}))
        )
        pages = []
        cursor = {"seq": "0", "table": 22, "after": None}
        while True:
            page = json.loads(
                _core.browser_analysis_facts(
                    str(path),
                    json.dumps(
                        {"account_id": 1, "source_id": state["source_id"], "until": state["cursor"], "cursor": cursor}
                    ),
                )
            )
            pages.append(page)
            cursor = page["cursor"]
            if page["complete"]:
                break
        _core.close_database(str(path))
        return {"state": state, "pages": pages, "charts": charts, "detail": detail, "costs": costs}


if __name__ == "__main__":
    Path(__file__).with_name("browser_analysis.json").write_text(
        json.dumps(generate(), ensure_ascii=False, indent=2) + "\n"
    )
