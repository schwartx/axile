"""原生资产事实操作与稳定观测身份。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def input_asset_fact_id(input_id: str) -> str:
    """返回执行前资产观测标识，允许尚未产生终态记录。"""
    return f"input:{input_id}:/before_account_assets"


async def save_manual_asset_snapshot(
    account_id: int,
    assets: Mapping[str, object],
    *,
    created_at: str | None = None,
    result: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    """一次提交手工观测头、类型化资产及持仓事实。"""
    from axile.server.db.models.base import now_str

    receipt = await _asset_operation(
        "asset_snapshot_write",
        {
            "account_id": account_id,
            "created_at": created_at if created_at is not None else now_str(),
            "account_assets": assets,
            "result": result,
        },
    )
    return receipt


async def latest_asset_snapshot(account_id: int) -> dict[str, Any] | None:
    """按历史编号读取最近可信资产，保留事实中的来源与观测时间。"""
    return await _asset_operation("asset_snapshot_latest", {"account_id": account_id})


async def _asset_operation(name: str, request: object) -> Any:
    """资产事实完整性错误沿用既有 ValueError 语义。"""
    from axile.server._native import _core
    from axile.server.native_store import native_operation

    try:
        return await native_operation(name, request)
    except _core.NativeError as error:
        if error.args and error.args[0] in {"BUSINESS_FACTS", "INVALID_DATA"}:
            raise ValueError(error.args[1]) from None
        raise
