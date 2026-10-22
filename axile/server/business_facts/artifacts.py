"""用不可变业务事实身份引用审计附件，显式解析后仍保留原始引用。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, cast

import sqlalchemy as sa
from sqlmodel import col

from axile.server.business_facts.summary_refs import (
    REFERENCE_KEY,
    BusinessFactReferenceError,
    compact_summary_content,
    hydrate_summary_content,
    validate_summary_reference,
)

ArtifactView = Literal["input", "targets", "rules", "assets_before", "assets_after", "result"]
_VIEWS = {"input", "targets", "rules", "assets_before", "assets_after", "result"}
_TARGET_FIELDS = ("curr_target", "last_target", "strategy_target", "sizing_context", "plan", "audit_evidence")
_RULE_FIELDS = ("trade_rules", "futures_close_policy")
_ARTIFACT_TYPES = {
    "input": "standard_input",
    "targets": "target_snapshot",
    "rules": "trade_rules_snapshot",
    "assets_before": "account_snapshot_before",
    "assets_after": "account_snapshot",
    "result": "execution_summary",
}


def make_artifact_reference(
    *,
    view: ArtifactView,
    input_id: str | None = None,
    asset_id: str | None = None,
    record_id: int | None = None,
    select_fields: list[str] | None = None,
) -> dict[str, object]:
    """构造版本固定的引用；身份必须来自已经持久化的事实。"""
    reference: dict[str, object] = {"version": 1, "view": view}
    if input_id is not None:
        reference["input_id"] = input_id
    if asset_id is not None:
        reference["asset_id"] = asset_id
    if record_id is not None:
        reference["record_id"] = record_id
    if view == "result":
        reference["projection_version"] = 2
    if select_fields is not None:
        reference["select_fields"] = list(select_fields)
    parse_artifact_reference({REFERENCE_KEY: reference})
    return {REFERENCE_KEY: reference}


def parse_artifact_reference(content: object) -> Mapping[str, object] | None:
    """历史正文原样保留；显式新引用损坏时直接报错。"""
    if not isinstance(content, Mapping) or REFERENCE_KEY not in content:
        return None
    reference = content[REFERENCE_KEY]
    if not isinstance(reference, Mapping):
        raise BusinessFactReferenceError("审计业务事实引用不是对象")
    view = reference.get("view")
    if (
        type(reference.get("version")) is not int
        or reference["version"] != 1
        or not isinstance(view, str)
        or view not in _VIEWS
    ):
        raise BusinessFactReferenceError("审计业务事实引用版本或视图无效")
    if view == "result":
        validate_summary_reference(reference)
        return reference
    _validate_select_fields(reference, view)
    for key in ("input_id", "asset_id"):
        value = reference.get(key)
        if key in reference and (not isinstance(value, str) or not value):
            raise BusinessFactReferenceError("审计业务事实引用身份无效")
    required = "asset_id" if view in {"assets_before", "assets_after"} else "input_id"
    if required not in reference:
        raise BusinessFactReferenceError("审计业务事实引用缺少不可变身份")
    return reference


def _validate_select_fields(reference: Mapping[str, object], view: str) -> None:
    if "select_fields" not in reference:
        return
    selected = reference["select_fields"]
    allowed = _TARGET_FIELDS if view == "targets" else _RULE_FIELDS if view == "rules" else ()
    if (
        not isinstance(selected, list)
        or not selected
        or any(not isinstance(key, str) or key not in allowed for key in selected)
        or len(set(selected)) != len(selected)
    ):
        raise BusinessFactReferenceError("审计业务事实引用的字段选择无效")


def _input_view_fields(payload: object, reference: Mapping[str, object]) -> dict[str, object]:
    if reference["view"] == "input":
        return {"input": payload}
    if not isinstance(payload, Mapping):
        raise BusinessFactReferenceError("审计目标或规则引用的输入不是对象")
    selected = reference.get("select_fields")
    if selected is not None:
        if any(key not in payload for key in cast(list[str], selected)):
            raise BusinessFactReferenceError("审计业务事实引用的选定字段缺失")
        return {key: payload[key] for key in cast(list[str], selected)}
    fields = _TARGET_FIELDS if reference["view"] == "targets" else _RULE_FIELDS
    return {key: payload[key] for key in fields if key in payload}


def _check_owner(owner: Mapping[str, object] | None, execution_id: object, account_id: int | None) -> None:
    if owner is None:
        raise BusinessFactReferenceError("审计业务事实引用目标不存在")
    context_id = owner.get("owner_execution_id")
    if context_id is None:
        context_id = owner["execution_id"]
    if (
        context_id != execution_id
        or (owner["execution_id"] is not None and owner["execution_id"] != execution_id)
        or (account_id is not None and owner["account_id"] != account_id)
    ):
        raise BusinessFactReferenceError("审计业务事实引用与执行账户不一致")


async def _validate_input_owner(session: Any, input_id: str, *, execution_id: object, account_id: int | None) -> None:
    from axile.server.business_facts.inputs import execution_input

    owner = (
        (
            await session.execute(
                sa.select(
                    execution_input.c.account_id, execution_input.c.execution_id, execution_input.c.owner_execution_id
                ).where(execution_input.c.id == input_id)
            )
        )
        .mappings()
        .first()
    )
    _check_owner(owner, execution_id, account_id)


async def _read_owned_input(session: Any, input_id: str, *, execution_id: object, account_id: int | None) -> object:
    from axile.server.business_facts.inputs import read_input

    await _validate_input_owner(session, input_id, execution_id=execution_id, account_id=account_id)
    return await read_input(session, input_id)


async def read_artifact_input_id(
    session: Any,
    *,
    execution_id: str,
    view: Literal["input", "targets", "rules"] = "targets",
    account_id: int | None = None,
) -> str | None:
    """只验证附件的冻结输入身份，不加载目标或完整输入明细。"""
    from axile.domain.execution import ExecutionArtifactType
    from axile.server.db.models.execution import ExecutionArtifact

    if view not in {"input", "targets", "rules"}:
        raise BusinessFactReferenceError("附件输入引用视图无效")
    content = await session.scalar(
        sa.select(col(ExecutionArtifact.content)).where(
            col(ExecutionArtifact.execution_id) == execution_id,
            col(ExecutionArtifact.artifact_type) == ExecutionArtifactType(_ARTIFACT_TYPES[view]),
        )
    )
    reference = parse_artifact_reference(content)
    if reference is None:
        return None
    if reference["view"] != view:
        raise BusinessFactReferenceError("审计业务事实引用与附件类型不一致")
    input_id = str(reference["input_id"])
    await _validate_input_owner(session, input_id, execution_id=execution_id, account_id=account_id)
    return input_id


async def _read_owned_asset(
    session: Any,
    reference: Mapping[str, object],
    *,
    execution_id: object,
    account_id: int | None,
) -> object:
    from axile.server.business_facts.assets import asset_position, asset_snapshot, decode_assets
    from axile.server.business_facts.inputs import execution_input
    from axile.server.db.models import ExecuteRecord

    asset_id = reference["asset_id"]
    row = (
        (await session.execute(sa.select(asset_snapshot).where(asset_snapshot.c.asset_id == asset_id)))
        .mappings()
        .first()
    )
    if row is None:
        raise BusinessFactReferenceError("审计资产事实引用目标不存在")
    before = reference["view"] == "assets_before"
    if row["scope"] not in ({"before"} if before else {"after", "after_pending"}):
        raise BusinessFactReferenceError("审计资产事实引用时点不一致")
    if row["input_id"] is not None:
        owner = (
            (
                await session.execute(
                    sa.select(
                        execution_input.c.account_id,
                        execution_input.c.execution_id,
                        execution_input.c.owner_execution_id,
                    ).where(execution_input.c.id == row["input_id"])
                )
            )
            .mappings()
            .first()
        )
        if reference.get("input_id", row["input_id"]) != row["input_id"]:
            raise BusinessFactReferenceError("审计资产事实与输入引用不一致")
    elif row["record_id"] is not None:
        owner = (
            (
                await session.execute(
                    sa.select(
                        col(ExecuteRecord.account_id), col(ExecuteRecord.execution_id), col(ExecuteRecord.input_id)
                    ).where(col(ExecuteRecord.id) == row["record_id"])
                )
            )
            .mappings()
            .first()
        )
        if "input_id" in reference and (owner is None or reference["input_id"] != owner["input_id"]):
            raise BusinessFactReferenceError("审计资产事实与输入引用不一致")
    else:
        raise BusinessFactReferenceError("执行审计资产事实缺少执行所有者")
    _check_owner(owner, execution_id, account_id)
    positions = (
        (
            await session.execute(
                sa.select(asset_position)
                .where(asset_position.c.asset_id == asset_id)
                .order_by(asset_position.c.ordinal)
            )
        )
        .mappings()
        .all()
    )
    decoded = decode_assets({asset_snapshot.name: [row], asset_position.name: positions})
    key = (
        "before_account_assets"
        if before
        else ("after_account_assets" if row["scope"] == "after_pending" else "account_assets")
    )
    if key not in decoded:
        raise BusinessFactReferenceError("审计资产事实正文不完整")
    return decoded[key]


async def _resolve_content(
    session: Any,
    content: dict[str, object],
    reference: Mapping[str, object],
    *,
    execution_id: object,
    account_id: int | None,
) -> dict[str, object]:
    view = reference["view"]
    if view == "result":
        _record, result = await _read_owned_result(
            session, cast(int, reference["record_id"]), execution_id=execution_id, account_id=account_id
        )
        return hydrate_summary_content(content, result)
    if view in {"assets_before", "assets_after"}:
        fields = {
            "account_assets": await _read_owned_asset(
                session, reference, execution_id=execution_id, account_id=account_id
            )
        }
    else:
        payload = await _read_owned_input(
            session, str(reference["input_id"]), execution_id=execution_id, account_id=account_id
        )
        fields = _input_view_fields(payload, reference)
    if any(key in content and content[key] != value for key, value in fields.items()):
        raise BusinessFactReferenceError("审计附件正文与不可变引用冲突")
    return {**content, **fields}


async def _read_owned_result(
    session: Any, record_id: int, *, execution_id: object, account_id: int | None
) -> tuple[Any, object]:
    from axile.server.business_facts.repository import read_execution_result
    from axile.server.db.models import ExecuteRecord

    owner = (
        (
            await session.execute(
                sa.select(col(ExecuteRecord.account_id), col(ExecuteRecord.execution_id)).where(
                    col(ExecuteRecord.id) == record_id
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    _check_owner(owner, execution_id, account_id)
    record = await session.get(ExecuteRecord, record_id)
    return record, await read_execution_result(session, record)


async def compact_execution_summary(session: Any, record: Any, *, result: object) -> None:
    """与终态事实同事务压缩已存在的 worker journal，失败不提交半套引用。"""
    from axile.domain.execution import ExecutionArtifactType
    from axile.server.db.models.execution import ExecutionArtifact

    if record.id is None or record.execution_id is None:
        return
    artifact = (
        (
            await session.execute(
                sa.select(ExecutionArtifact).where(
                    col(ExecutionArtifact.execution_id) == record.execution_id,
                    col(ExecutionArtifact.artifact_type) == ExecutionArtifactType.EXECUTION_SUMMARY,
                )
            )
        )
        .scalars()
        .one_or_none()
    )
    if artifact is None or parse_artifact_reference(artifact.content) is not None:
        return
    compact = compact_summary_content(artifact.content, result, record_id=record.id)
    if compact is not None:
        cast(dict[str, object], compact[REFERENCE_KEY])["legacy_schema_version"] = artifact.schema_version
        artifact.content = compact
        artifact.schema_version = 3
        session.add(artifact)
        await session.flush()


async def detach_execution_summaries(session: Any, record: Any) -> None:
    """删除终态事实前物化独立 journal，恢复原附件内容和协议版本。"""
    from axile.domain.execution import ExecutionArtifactType
    from axile.server.business_facts.repository import read_execution_result
    from axile.server.db.models.execution import ExecutionArtifact

    statement = sa.select(ExecutionArtifact).where(
        col(ExecutionArtifact.artifact_type) == ExecutionArtifactType.EXECUTION_SUMMARY,
        col(ExecutionArtifact.content)[REFERENCE_KEY]["view"].as_string() == "result",
        col(ExecutionArtifact.content)[REFERENCE_KEY]["record_id"].as_integer() == record.id,
    )
    artifacts = (await session.execute(statement)).scalars().all()
    if not artifacts:
        return
    result = await read_execution_result(session, record)
    for artifact in artifacts:
        _check_owner(
            {"account_id": record.account_id, "execution_id": record.execution_id},
            artifact.execution_id,
            record.account_id,
        )
        reference = parse_artifact_reference(artifact.content)
        if reference is None:
            raise BusinessFactReferenceError("执行摘要缺少结果引用")
        artifact.content = hydrate_summary_content(artifact.content, result, include_reference=False)
        artifact.schema_version = reference.get("legacy_schema_version", 2)
        session.add(artifact)
    await session.flush()


async def resolve_artifact_content(session: Any, artifact: Any, *, account_id: int | None = None) -> dict[str, object]:
    """在调用方事务中解析引用，并保留引用标记及独立历史附加证据。"""
    content = artifact.get("content") if isinstance(artifact, Mapping) else artifact.content
    execution_id = artifact.get("execution_id") if isinstance(artifact, Mapping) else artifact.execution_id
    if not isinstance(content, dict):
        raise BusinessFactReferenceError("审计附件正文不是对象")
    reference = parse_artifact_reference(content)
    if reference is None:
        return content
    kind = artifact.get("artifact_type") if isinstance(artifact, Mapping) else artifact.artifact_type
    if getattr(kind, "value", kind) != _ARTIFACT_TYPES[cast(str, reference["view"])]:
        raise BusinessFactReferenceError("审计业务事实引用与附件类型不一致")
    return await _resolve_content(session, content, reference, execution_id=execution_id, account_id=account_id)


async def resolve_event_details(session: Any, event: Any, *, account_id: int | None = None) -> Any:
    """解析目标计算事件中的不可变决策引用，保留事件本身的独立字段。"""
    details = event.get("details") if isinstance(event, Mapping) else event.details
    if not isinstance(details, dict):
        return details
    decision = details.get("decision")
    if not isinstance(decision, dict):
        return details
    reference = parse_artifact_reference(decision)
    if reference is None:
        return details
    kind = event.get("event_type") if isinstance(event, Mapping) else event.event_type
    if (
        reference["view"] != "targets"
        or "select_fields" not in reference
        or getattr(kind, "value", kind) != "target_computed"
    ):
        raise BusinessFactReferenceError("执行业务事实引用与事件类型不一致")
    execution_id = event.get("execution_id") if isinstance(event, Mapping) else event.execution_id
    resolved = await _resolve_content(session, decision, reference, execution_id=execution_id, account_id=account_id)
    return {**details, "decision": resolved}
