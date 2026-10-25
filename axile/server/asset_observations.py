"""校验渠道返回的资产观测；历史占位识别由原生事实写入负责。"""

import sys

from axile.executor.models.unified_account_assets import is_degraded_snapshot_source


def is_asset_observation(assets: object) -> bool:
    """真实零余额有效；缺失、非有限权益与未取得资产均不是观测。"""
    if not isinstance(assets, dict) or is_degraded_snapshot_source(assets.get("source")):
        return False
    value = assets.get("total_asset")
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and -sys.float_info.max <= value <= sys.float_info.max
    )
