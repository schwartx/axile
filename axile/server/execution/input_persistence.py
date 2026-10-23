"""在下单前提交不可变的执行输入与账户观测事实。"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from starlette.concurrency import run_in_threadpool

from axile.server.db.models.base import now_str
from axile.server.native_store import native_operation_sync


def sanitize_execution_input(payload: Mapping[str, object]) -> dict[str, object]:
    """移除账户凭据与通知源码，并遮蔽通知地址。"""
    sanitized = dict(payload)
    sanitized.pop("account_config", None)
    sanitized.pop("execution_notification_code", None)
    if sanitized.get("feishu_key"):
        sanitized["feishu_key"] = "***"
    return sanitized


def _freeze_facts(command: str, request: Mapping[str, Any]) -> Any:
    """单次原生调用完成整个冻结事务;业务错误还原为既有 ValueError 语义。"""
    from axile.server._native import _core

    try:
        return native_operation_sync(command, request)
    except _core.NativeError as error:
        raise ValueError(str(error.args[1]) if len(error.args) > 1 else str(error)) from None


async def _persist_pre_execute_input(
    *,
    account_id: int,
    execution_id: str,
    payload: Mapping[str, object],
    before_account_assets: Mapping[str, object] | None = None,
) -> str:
    """将执行前输入和可用账户观测原子提交，失败时禁止继续下单。"""
    input_id, _ = await _persist_pre_execute_bundle(
        account_id=account_id,
        execution_id=execution_id,
        payload=payload,
        before_account_assets=before_account_assets,
    )
    return input_id


async def _persist_pre_execute_bundle(
    *,
    account_id: int,
    execution_id: str,
    payload: Mapping[str, object],
    audit_payload: Mapping[str, object] | None = None,
    before_account_assets: Mapping[str, object] | None = None,
) -> tuple[str, str | None]:
    """原子冻结一份业务输入与执行前观测；不同来源的历史证据独立保留。"""
    request = _pre_execute_request(
        account_id=account_id,
        execution_id=execution_id,
        payload=payload,
        audit_payload=audit_payload,
        before_account_assets=before_account_assets,
    )
    receipt = await run_in_threadpool(_freeze_facts, "freeze_execution_input", request)
    return receipt["input_id"], receipt.get("audit_id")


def _pre_execute_request(
    *,
    account_id: int,
    execution_id: str,
    payload: Mapping[str, object],
    audit_payload: Mapping[str, object] | None = None,
    before_account_assets: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    request: dict[str, Any] = {
        "account_id": account_id,
        "execution_id": execution_id,
        "input_id": str(uuid.uuid4()),
        "created_at": now_str(),
        "payload": payload,
    }
    if audit_payload is not None:
        identity = f"axile.input-evidence:{account_id}:{execution_id}:pre_execute_audit"
        request["audit_payload"] = audit_payload
        request["audit_input_id"] = str(uuid.uuid5(uuid.NAMESPACE_URL, identity))
    if before_account_assets is not None:
        request["before_assets"] = before_account_assets
    return request


def persist_pre_execute_bundle_sync(
    *,
    account_id: int,
    execution_id: str,
    payload: Mapping[str, object],
    audit_payload: Mapping[str, object],
    before_account_assets: Mapping[str, object] | None = None,
) -> tuple[str, str]:
    """等待调仓前两种独立输入事实与观测一起提交。"""
    receipt = _freeze_facts(
        "freeze_execution_input",
        _pre_execute_request(
            account_id=account_id,
            execution_id=execution_id,
            payload=payload,
            audit_payload=audit_payload,
            before_account_assets=before_account_assets,
        ),
    )
    if receipt.get("audit_id") is None:
        raise RuntimeError("调仓目标规则证据未持久化")
    return receipt["input_id"], receipt["audit_id"]


def persist_pre_execute_input_sync(
    *,
    account_id: int,
    execution_id: str,
    payload: Mapping[str, object],
    before_account_assets: Mapping[str, object] | None = None,
) -> str:
    """在线程或同步 worker 中等待执行前事实提交；异常直接传给执行入口。"""
    receipt = _freeze_facts(
        "freeze_execution_input",
        _pre_execute_request(
            account_id=account_id,
            execution_id=execution_id,
            payload=payload,
            before_account_assets=before_account_assets,
        ),
    )
    return receipt["input_id"]


async def _persist_post_execute_assets(
    *, account_id: int, execution_id: str, payload: Mapping[str, object]
) -> tuple[str, str] | None:
    """提交执行后的账户观测，先于 worker 回传结果保留故障恢复证据。"""
    receipt = await run_in_threadpool(
        _freeze_facts,
        "freeze_execution_after_assets",
        {"account_id": account_id, "execution_id": execution_id, "payload": payload},
    )
    return (receipt["input_id"], receipt["asset_id"]) if receipt else None


def persist_post_execute_assets_sync(
    *, account_id: int, execution_id: str, payload: Mapping[str, object]
) -> tuple[str, str] | None:
    """同步等待执行后观测提交，失败时保留错误而不返回成功响应。"""
    receipt = _freeze_facts(
        "freeze_execution_after_assets",
        {"account_id": account_id, "execution_id": execution_id, "payload": payload},
    )
    return (receipt["input_id"], receipt["asset_id"]) if receipt else None
