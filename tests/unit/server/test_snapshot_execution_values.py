"""复用执行投影时，冻结值仍符合原模型的 JSON 序列化契约。"""

import json

from axile.server.db.models.performance import AccountPerformance, PerformanceSettings
from axile.server.performance_analysis import compute_batch
from tests.unit.server.test_performance_analysis import record


def test_native_execution_values_match_original_model_serialization():
    rows = [record(1), record(2)]
    symbol = rows[0].result["symbol_results"]["A"]
    symbol.update(error=None, outcome_reason=False, reason_code="")
    fee = float("5.346525073723255485091955e-135")
    symbol["trades"][0]["extra"].update(commission=fee, commission_asset="CNY")
    rows[1].result.update(status="NOOP", symbol_results={}, account_assets={"total_asset": 0, "positions": []})
    original = json.dumps([row.result for row in rows], sort_keys=True, allow_nan=False)
    ranges, _, _ = compute_batch(rows, [], [], [], PerformanceSettings(backtest_weight_type="cs", backtest_fee_rate=0))
    value = ranges["all"]["performance"]
    legacy = AccountPerformance.model_validate(value).model_dump(mode="json")
    assert json.dumps(value, sort_keys=True, allow_nan=False) == json.dumps(legacy, sort_keys=True, allow_nan=False)
    assert value["executions"][0]["summary"]["fees"]["CNY"].hex() == fee.hex()
    assert value["executions"][1]["positionCount"] == 0
    assert value["executions"][1]["positions"] == []
    assert json.dumps([row.result for row in rows], sort_keys=True, allow_nan=False) == original
