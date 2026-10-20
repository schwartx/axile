"""在下单前提交不可变的执行输入与账户观测事实。"""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from axile.server.business_facts.inputs import canonical_input, normalize_targets, persist_owned_input_variant
from axile.server.business_facts.repository import persist_execution_after_assets, persist_execution_input
from axile.server.core.db import SessionLocal
from axile.server.db.models.base import now_str
from axile.server.execution_audit import _run_coroutine_sync


def sanitize_execution_input(payload: Mapping[str, object]) -> dict[str, object]:
    """移除账户凭据与通知源码，并遮蔽通知地址。"""
    sanitized = dict(payload)
    sanitized.pop("account_config", None)
    sanitized.pop("execution_notification_code", None)
    if sanitized.get("feishu_key"):
        sanitized["feishu_key"] = "***"
    return sanitized


async def _begin_input_transaction(session: AsyncSession) -> None:
    """确保 SQLite 外层事务已开始，避免首次 SAVEPOINT 单独提交输入。"""
    connection = await session.connection()
    if connection.dialect.name == "sqlite":
        raw_connection = await connection.get_raw_connection()
        driver_connection = raw_connection.driver_connection
        if driver_connection is None:
            raise RuntimeError("SQLite 驱动连接不可用")
        if not driver_connection.in_transaction:
            await connection.exec_driver_sql("BEGIN")


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
    async with SessionLocal() as session:
        await _begin_input_transaction(session)
        created_at = now_str()
        input_id = await persist_execution_input(
            session,
            account_id=account_id,
            execution_id=execution_id,
            payload=sanitize_execution_input(payload),
            created_at=created_at,
            before_assets=before_account_assets,
        )
        same_audit = False
        if audit_payload is not None:
            primary = canonical_input(payload)
            audit = canonical_input(audit_payload)
            keys = normalize_targets(audit_payload)
            same_audit = (
                isinstance(primary, Mapping)
                and isinstance(audit, Mapping)
                and all(primary.get(key) == audit.get(key) for key in keys)
            )
        audit_id = (
            input_id
            if same_audit
            else (
                await persist_owned_input_variant(
                    session,
                    account_id=account_id,
                    owner_execution_id=execution_id,
                    payload=sanitize_execution_input(audit_payload),
                    created_at=created_at,
                )
                if audit_payload is not None
                else None
            )
        )
        await session.commit()
        return input_id, audit_id


def persist_pre_execute_bundle_sync(
    *,
    account_id: int,
    execution_id: str,
    payload: Mapping[str, object],
    audit_payload: Mapping[str, object],
    before_account_assets: Mapping[str, object] | None = None,
) -> tuple[str, str]:
    """等待调仓前两种独立输入事实与观测一起提交。"""
    input_id, audit_id = _run_coroutine_sync(
        lambda: _persist_pre_execute_bundle(
            account_id=account_id,
            execution_id=execution_id,
            payload=payload,
            audit_payload=audit_payload,
            before_account_assets=before_account_assets,
        )
    )
    if audit_id is None:
        raise RuntimeError("调仓目标规则证据未持久化")
    return input_id, audit_id


def persist_pre_execute_input_sync(
    *,
    account_id: int,
    execution_id: str,
    payload: Mapping[str, object],
    before_account_assets: Mapping[str, object] | None = None,
) -> str:
    """在线程或同步 worker 中等待执行前事实提交；异常直接传给执行入口。"""
    return _run_coroutine_sync(
        lambda: _persist_pre_execute_input(
            account_id=account_id,
            execution_id=execution_id,
            payload=payload,
            before_account_assets=before_account_assets,
        )
    )


async def _persist_post_execute_assets(
    *, account_id: int, execution_id: str, payload: Mapping[str, object]
) -> tuple[str, str] | None:
    """提交执行后的账户观测，先于 worker 回传结果保留故障恢复证据。"""
    async with SessionLocal() as session:
        reference = await persist_execution_after_assets(
            session,
            account_id=account_id,
            execution_id=execution_id,
            payload=payload,
        )
        await session.commit()
        return reference


def persist_post_execute_assets_sync(
    *, account_id: int, execution_id: str, payload: Mapping[str, object]
) -> tuple[str, str] | None:
    """同步等待执行后观测提交，失败时保留错误而不返回成功响应。"""
    return _run_coroutine_sync(
        lambda: _persist_post_execute_assets(account_id=account_id, execution_id=execution_id, payload=payload)
    )
