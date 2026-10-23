"""原生资产事实操作与稳定观测身份。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from axile.server.db.models.account_asset import AccountAssetSnapshot


def asset_fact_id(record_id: int, path: str = "/account_assets") -> str:
    """返回终态记录中某个资产观测的稳定发生标识。"""
    return f"record:{record_id}:{path}"


def input_asset_fact_id(input_id: str) -> str:
    """返回执行前资产观测标识，允许尚未产生终态记录。"""
    return f"input:{input_id}:/before_account_assets"


def input_after_asset_fact_id(input_id: str) -> str:
    """返回 worker 在终态记录之前保存的执行后观测标识。"""
    return f"input:{input_id}:/after_account_assets"


def snapshot_asset_fact_id(snapshot_id: int) -> str:
    """返回一次手工资产观测的稳定发生标识。"""
    return f"snapshot:{snapshot_id}:/account_assets"


async def save_manual_asset_snapshot(
    account_id: int,
    assets: Mapping[str, object],
    *,
    created_at: str | None = None,
    result: Mapping[str, object] | None = None,
) -> AccountAssetSnapshot:
    """一次提交手工观测头、类型化资产及持仓事实。"""
    from axile.server.db.models.account_asset import AccountAssetSnapshot
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
    return AccountAssetSnapshot.model_validate(receipt)


async def latest_asset_snapshot(account_id: int) -> dict[str, Any] | None:
    """按历史编号读取最近可信资产，保留事实中的来源与观测时间。"""
    return await _asset_operation("asset_snapshot_latest", {"account_id": account_id})


async def read_input_asset_observations(_session: Any, input_id: str) -> list[dict[str, object]]:
    """读取已提交输入的原始前后观测，不依赖终态记录存续。"""
    return await _asset_operation("asset_input_observations", {"input_id": input_id})


async def read_snapshots_assets(
    _session: Any, snapshots: Sequence[Any], *, include_positions: bool = True
) -> dict[int, dict[str, object]]:
    """一次读取已提交资产快照；标量读取不装载持仓子表。"""
    if not snapshots:
        return {}
    rows = await _asset_operation(
        "asset_snapshots_read",
        {
            "snapshots": [
                {
                    "id": snapshot.id,
                    "account_id": snapshot.account_id,
                    "asset_fact_id": snapshot.asset_fact_id,
                    "execution_id": snapshot.execution_id,
                }
                for snapshot in snapshots
            ],
            "include_positions": include_positions,
        },
    )
    return {row["id"]: row["assets"] for row in rows}


async def read_snapshot_assets(_session: Any, snapshot: Any, *, include_positions: bool = True) -> dict[str, object]:
    """显式读取一条已提交资产观测，拒绝不完整或越权引用。"""
    return (await read_snapshots_assets(None, [snapshot], include_positions=include_positions))[snapshot.id]


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
