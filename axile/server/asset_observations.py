"""共享资产观测口径；保留失败审计，仅排除可精确识别的占位值。"""

import sys
from collections.abc import Mapping
from typing import Any

from axile.executor.models.unified_account_assets import is_degraded_snapshot_source


def is_legacy_placeholder(result: Mapping[str, Any]) -> bool:
    """识别旧构造器的完整结构，不按失败状态或错误文案猜测余额。"""
    assets = result.get("account_assets")
    if not isinstance(assets, dict) or assets.get("source", "real") != "real":
        return False
    if not all(
        type(assets.get(key)) in (int, float) and assets[key] == 0
        for key in ("available_cash", "total_asset", "market_value")
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
        and result.get("inputs") in (None, {})
        and type(result.get("execution_time")) in (int, float)
        and result["execution_time"] == 0
    )


def _blocked_placeholder(result: Mapping[str, Any]) -> bool:
    return (
        result.get("status") == "BLOCKED"
        and result.get("error") == "当前不在交易时间"
        and result.get("memory") == {"message": "当前不在交易时间"}
        and isinstance(result.get("inputs"), dict)
        and type(result.get("execution_time")) in (int, float)
        and result["execution_time"] >= 0
    )


def is_asset_observation(assets: object, result: Mapping[str, Any] | None = None) -> bool:
    """真实零余额有效；缺失、非有限权益与未取得资产均不是观测。"""
    if not isinstance(assets, dict) or is_degraded_snapshot_source(assets.get("source")):
        return False
    value = assets.get("total_asset")
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and -sys.float_info.max <= value <= sys.float_info.max
        and (result is None or not is_legacy_placeholder(result))
    )
