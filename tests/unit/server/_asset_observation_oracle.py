"""冻结历史占位判定，独立核验原生资产观测。"""

from collections.abc import Mapping
from typing import Any

from axile.server.asset_observations import is_asset_observation as valid_assets


def is_legacy_placeholder(result: Mapping[str, Any]) -> bool:
    assets = result.get("account_assets")
    if not isinstance(assets, dict) or assets.get("source", "real") != "real":
        return False
    if not all(
        (
            type(assets.get(key)) in (int, float) and assets[key] == 0
            for key in ("available_cash", "total_asset", "market_value")
        )
    ):
        return False
    if assets.get("positions") != [] or assets.get("extra") != {} or result.get("symbol_results") != {}:
        return False
    return _worker_placeholder(result) or _blocked_placeholder(result)


def _worker_placeholder(result: Mapping[str, Any]) -> bool:
    extra = result.get("extra")
    return (
        result.get("status") == "FAILED"
        and isinstance(extra, dict)
        and isinstance(extra.get("worker_error"), dict)
        and (result.get("inputs") in (None, {}))
        and (type(result.get("execution_time")) in (int, float))
        and (result["execution_time"] == 0)
    )


def _blocked_placeholder(result: Mapping[str, Any]) -> bool:
    return (
        result.get("status") == "BLOCKED"
        and result.get("error") == "当前不在交易时间"
        and (result.get("memory") == {"message": "当前不在交易时间"})
        and isinstance(result.get("inputs"), dict)
        and (type(result.get("execution_time")) in (int, float))
        and (result["execution_time"] >= 0)
    )


def is_asset_observation(assets, result=None):
    return valid_assets(assets) and (result is None or not is_legacy_placeholder(result))
