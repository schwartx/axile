"""类型化执行事实的事务编排与显式明细读取。"""

from collections.abc import Mapping, Sequence
from typing import Any, cast

import sqlalchemy as sa

from axile.server.business_facts import assets, extensions, head, inputs, orders, symbols, trades
from axile.server.business_facts.notification_context import (
    hydrate_result_notification_context,
    hydrate_result_notification_context_sync,
)

_RESULT_DOMAINS = (head, symbols, assets, orders, trades, extensions)
_INPUT_UNSET = object()
_EXECUTION_UNSET = object()


def _metadata(row: Mapping[str, object]) -> Mapping[str, object]:
    metadata = row["audit_evidence"]
    if not isinstance(metadata, Mapping):
        raise ValueError("执行字段元数据无效")
    return metadata


def execution_fact_tables() -> tuple[sa.Table, ...]:
    """返回输入父表优先、结果父表优先的完整事实表注册表。"""
    return tuple(dict.fromkeys((*inputs.TABLES, *(table for domain in _RESULT_DOMAINS for table in domain.TABLES))))


def encode_result_facts(
    record_id: int, payload: object, *, is_success: int | None = None, root_asset_id: str | None = None
) -> dict[sa.Table, list[dict[str, object]]]:
    """供实时事务和同步升级复用的纯结果转换，主表条目用于更新。"""
    if not isinstance(payload, Mapping):
        batch = {
            **head.encode_head(record_id, {}, is_success=is_success),
            **extensions.encode_result_root_state(record_id, payload),
        }
        batch[head.execution_head][0]["facts_complete"] = True
        return batch
    batch = head.encode_head(record_id, payload, is_success=is_success)
    for domain in _RESULT_DOMAINS[1:]:
        encoder = getattr(domain, "encode_" + domain.__name__.rsplit(".", 1)[1])
        facts = (
            encoder(record_id, payload, root_asset_id=root_asset_id)
            if domain is assets
            else encoder(record_id, payload)
        )
        for table, values in facts.items():
            if table in batch:
                raise ValueError(f"重复的业务事实表所有权: {table.name}")
            batch[table] = values
    batch[head.execution_head][0]["facts_complete"] = True
    return batch


def merge_result_fragment(destination: dict[str, object], fragment: Mapping[str, object]) -> None:
    """合并各领域唯一拥有的字段，拒绝用虚构空容器覆盖历史异常证据。"""
    for key, value in fragment.items():
        if key not in destination:
            destination[key] = value
            continue
        existing = destination[key]
        if isinstance(existing, dict) and isinstance(value, Mapping):
            merge_result_fragment(existing, value)
        elif existing != value:
            raise ValueError(f"业务事实领域字段冲突: {key}")


async def _write_batch(session: Any, batch: Mapping[sa.Table, Sequence[Mapping[str, object]]]) -> None:
    for table, rows in batch.items():
        for start in range(0, len(rows), 100):
            await session.execute(table.insert(), list(rows[start : start + 100]))


def _validate_input_owner(
    row: Mapping[str, object], account_id: int | None, execution_id: object = _EXECUTION_UNSET
) -> None:
    if account_id is not None and row["account_id"] != account_id:
        raise ValueError("执行输入属于其他账户")
    owner = row.get("owner_execution_id")
    owner = row.get("execution_id") if owner is None else owner
    if execution_id is not _EXECUTION_UNSET and owner != execution_id:
        raise ValueError("执行输入属于其他执行")


def _other_input_reference(input_id: str, record_id: int) -> Any:
    table = head.execution_head
    return (
        sa.select(table.c.id)
        .where(table.c.id != record_id, sa.or_(table.c.input_id == input_id, table.c.result_input_id == input_id))
        .limit(1)
    )


async def _input_row(
    session: Any,
    input_id: str,
    account_id: int | None = None,
    execution_id: object = _EXECUTION_UNSET,
    record_id: int | None = None,
) -> Mapping[str, object]:
    row = (
        (await session.execute(sa.select(inputs.execution_input).where(inputs.execution_input.c.id == input_id)))
        .mappings()
        .first()
    )
    if row is None:
        raise ValueError("执行输入不存在")
    _validate_input_owner(row, account_id, execution_id)
    if execution_id is None and record_id is not None:
        if await session.scalar(_other_input_reference(input_id, record_id)) is not None:
            raise ValueError("无执行标识的历史输入不能属于其他执行记录")
    return row


async def _find_execution_input_row(session: Any, *, account_id: int, execution_id: str) -> Mapping[str, object] | None:
    row = (
        (
            await session.execute(
                sa.select(inputs.execution_input).where(inputs.execution_input.c.execution_id == execution_id)
            )
        )
        .mappings()
        .first()
    )
    if row is not None:
        _validate_input_owner(row, account_id, execution_id)
    return row


async def find_execution_input(session: Any, *, account_id: int, execution_id: str) -> str | None:
    """按执行标识定位下单前已持久化输入，同时拒绝跨账户关联。"""
    row = await _find_execution_input_row(session, account_id=account_id, execution_id=execution_id)
    return cast(str, row["id"]) if row is not None else None


async def persist_execution_input(
    session: Any,
    *,
    account_id: int,
    execution_id: str | None,
    payload: Mapping[str, object],
    created_at: str,
    before_assets: Mapping[str, object] | None = None,
    owner_execution_id: str | None = None,
) -> str:
    """冻结交易前输入及资产；调用方成功提交事务后才可下单。"""
    input_id = await inputs.persist_input(
        session,
        account_id=account_id,
        execution_id=execution_id,
        payload=payload,
        created_at=created_at,
        owner_execution_id=owner_execution_id,
    )
    if before_assets is not None:
        existing = await session.scalar(
            sa.select(sa.func.count())
            .select_from(assets.asset_snapshot)
            .where(assets.asset_snapshot.c.input_id == input_id)
        )
        if not existing:
            await _write_batch(session, assets.encode_input_assets(input_id, {"account_assets": before_assets}))
    return input_id


async def read_execution_input(session: Any, input_id: str) -> object:
    """显式读取冻结输入，缺失的关联行视为完整性错误。"""
    row = await _input_row(session, input_id)
    return await inputs.read_input(session, input_id, head_row=row)


def _same_input(left: object, right: object) -> bool:
    return left == inputs.canonical_input(right)


async def _attach_inputs(
    session: Any, record: Any, raw_input: Mapping[str, object] | None, raw_result: Mapping[str, object]
) -> object:
    input_id = getattr(record, "input_id", None)
    row = None
    if input_id is None and record.execution_id is not None:
        row = await _find_execution_input_row(session, account_id=record.account_id, execution_id=record.execution_id)
        input_id = cast(str, row["id"]) if row is not None else None
    if input_id is None and raw_input is not None:
        input_id = await persist_execution_input(
            session,
            account_id=record.account_id,
            execution_id=record.execution_id,
            payload=raw_input,
            created_at=record.created_at,
        )
    if input_id is not None and row is None:
        row = await _input_row(session, input_id, record.account_id, record.execution_id, record.id)
    canonical_input = await inputs.read_input(session, input_id, head_row=row) if input_id is not None else {}
    record.input_id = input_id
    result_input = raw_result.get("inputs")
    if getattr(record, "result_input_id", None) is not None:
        await _input_row(session, record.result_input_id, record.account_id, record.execution_id, record.id)
    record.result_input_id = None
    if isinstance(result_input, Mapping):
        sanitized = extensions.sanitize_input(result_input)
        if input_id is not None and _same_input(canonical_input, sanitized):
            record.result_input_id = input_id
        else:
            record.result_input_id = await persist_execution_input(
                session,
                account_id=record.account_id,
                execution_id=None,
                owner_execution_id=record.execution_id,
                payload=sanitized,
                created_at=record.created_at,
            )
    return canonical_input


async def store_execution_facts(
    session: Any,
    record: Any,
    raw_input: Mapping[str, object] | None,
    raw_result: Mapping[str, object],
) -> object:
    """在调用方事务内保存完整结果事实但不提交，返回已读取的冻结输入供复用。"""
    if record.id is None:
        await session.flush()
    if record.id is None:
        raise ValueError("执行记录必须先取得主键")
    replacing = bool(getattr(record, "facts_complete", False))
    record.facts_complete = False
    canonical_input = await _attach_inputs(session, record, raw_input, raw_result)
    root_asset_id = None
    # 失败收尾可能先于 worker 成功回包；后观测仍属输入证据，不能据此补造结果资产。
    # 成功结果缺字段仍须拒绝，显式携带的任何后观测也必须与已冻结证据一致。
    if record.input_id is not None and ("account_assets" in raw_result or record.is_success == 1):
        root_asset_id = await assets.find_matching_after_asset(session, record.input_id, raw_result)
    batch = encode_result_facts(record.id, raw_result, is_success=record.is_success, root_asset_id=root_asset_id)
    for name, value in batch.pop(head.execution_head)[0].items():
        if name not in {"id", "facts_complete"}:
            setattr(record, name, value)
    if replacing:
        for domain in reversed(_RESULT_DOMAINS):
            if domain is assets:
                await _delete_result_assets(session, record.id)
                continue
            for table in reversed(domain.TABLES):
                if table is not head.execution_head:
                    await session.execute(table.delete().where(table.c.record_id == record.id))
    await session.flush()
    await _write_batch(session, batch)
    record.facts_complete = True
    await session.flush()
    return canonical_input


async def _delete_result_assets(session: Any, record_id: int) -> None:
    await session.execute(assets.asset_binding.delete().where(assets.asset_binding.c.record_id == record_id))
    owned = sa.select(assets.asset_snapshot.c.asset_id).where(assets.asset_snapshot.c.record_id == record_id)
    await session.execute(assets.asset_position.delete().where(assets.asset_position.c.asset_id.in_(owned)))
    await session.execute(assets.asset_snapshot.delete().where(assets.asset_snapshot.c.record_id == record_id))


async def persist_execution_after_assets(
    session: Any, *, account_id: int, execution_id: str, payload: Mapping[str, object]
) -> tuple[str, str] | None:
    """在 worker 返回前冻结执行后资产，使审计可引用同一份不可变观测。"""
    input_id = await find_execution_input(session, account_id=account_id, execution_id=execution_id)
    if input_id is None:
        raise ValueError("执行后资产观测缺少已冻结输入")
    if "account_assets" not in payload:
        return None
    existing = await assets.find_matching_after_asset(session, input_id, payload)
    if existing is not None:
        return input_id, existing
    await _write_batch(session, assets.encode_input_after_assets(input_id, payload, result=payload))
    return input_id, assets.input_after_asset_fact_id(input_id)


def _asset_scope(record_ids: Sequence[int], input_ids: Sequence[str], asset_ids: Sequence[str] = ()) -> Any:
    predicates = []
    if record_ids:
        bound = sa.select(assets.asset_binding.c.asset_id).where(assets.asset_binding.c.record_id.in_(record_ids))
        predicates.extend(
            (assets.asset_snapshot.c.record_id.in_(record_ids), assets.asset_snapshot.c.asset_id.in_(bound))
        )
    if input_ids:
        predicates.append(assets.asset_snapshot.c.input_id.in_(input_ids))
    if asset_ids:
        predicates.append(assets.asset_snapshot.c.asset_id.in_(asset_ids))
    return sa.or_(*predicates) if predicates else sa.false()


def execution_fact_sources(
    *, record_ids: Sequence[int] = (), input_ids: Sequence[str] = (), asset_ids: Sequence[str] = ()
) -> tuple[tuple[sa.Table, Any], ...]:
    """为证据预算与导出提供完整的表及范围谓词，查询前可计费。"""
    result = []
    for table in execution_fact_tables():
        if table is assets.asset_snapshot:
            result.append((table, _asset_scope(record_ids, input_ids, asset_ids)))
            continue
        if table is assets.asset_position:
            selected_assets = sa.select(assets.asset_snapshot.c.asset_id).where(
                _asset_scope(record_ids, input_ids, asset_ids)
            )
            result.append((table, table.c.asset_id.in_(selected_assets)))
            continue
        predicates = []
        if table is head.execution_head and record_ids:
            predicates.append(table.c.id.in_(record_ids))
        elif table is inputs.execution_input and input_ids:
            predicates.append(table.c.id.in_(input_ids))
        if "record_id" in table.c and record_ids:
            predicates.append(table.c.record_id.in_(record_ids))
        if table is assets.asset_binding and asset_ids:
            predicates.append(table.c.asset_id.in_(asset_ids))
        if "input_id" in table.c and table is not head.execution_head and input_ids:
            predicates.append(table.c.input_id.in_(input_ids))
        if predicates:
            result.append((table, sa.or_(*predicates)))
    return tuple(result)


async def read_execution_fact_rows(session: Any, record: Any) -> dict[str, list[Mapping[str, object]]]:
    """显式加载一个执行的结果事实；历史列表不调用此函数。"""
    rows = {}
    for domain in _RESULT_DOMAINS:
        if domain is assets:
            rows.update(await assets.load_result_asset_rows(session, record.id))
            continue
        for table in domain.TABLES:
            predicate = table.c.id == record.id if table is head.execution_head else table.c.record_id == record.id
            rows[table.name] = list((await session.execute(sa.select(table).where(predicate))).mappings())
    if not rows[head.execution_head.name]:
        raise ValueError("执行主记录不存在")
    if not rows[head.execution_head.name][0]["facts_complete"]:
        raise ValueError("执行事实尚未完整提交")
    return rows


async def read_execution_result(session: Any, record: Any) -> object:
    """按需从唯一类型化事实组装结果，保留各领域的缺失与异常容器。"""
    rows = await read_execution_fact_rows(session, record)
    stored = rows[head.execution_head.name][0]
    primary_input_id = stored.get("input_id")
    if isinstance(primary_input_id, str):
        await _input_row(session, primary_input_id, record.account_id, record.execution_id, record.id)
    result_input = _INPUT_UNSET
    if stored["result_input_kind"] == "value":
        input_id = stored["result_input_id"]
        if not isinstance(input_id, str):
            raise ValueError("执行结果缺少输入引用")
        account_id = stored["account_id"]
        if type(account_id) is not int:
            raise ValueError("执行记录缺少账户身份")
        record_id = stored["id"]
        if type(record_id) is not int:
            raise ValueError("执行记录主键无效")
        await _input_row(session, input_id, account_id, stored["execution_id"], record_id)
        result_input = await read_execution_input(session, input_id)
    result = decode_result_facts(rows, result_input=result_input)
    if isinstance(result, dict):
        return await hydrate_result_notification_context(
            session, result, account_id=record.account_id, execution_id=record.execution_id
        )
    return result


def decode_result_facts(
    rows: Mapping[str, Sequence[Mapping[str, object]]], *, result_input: object = _INPUT_UNSET
) -> object:
    """同步升级与异步业务共享同一纯解码器，调用方显式提供所引用输入。"""
    kind, root_value = extensions.decode_result_root_state(rows)
    if kind != "object":
        return root_value
    result: dict[str, object] = {}
    for domain in _RESULT_DOMAINS:
        decoder = getattr(domain, "decode_" + domain.__name__.rsplit(".", 1)[1])
        merge_result_fragment(result, decoder(rows))
    stored = rows[head.execution_head.name][0]
    input_kind = stored["result_input_kind"]
    if input_kind == "value":
        if result_input is _INPUT_UNSET:
            raise ValueError("执行结果缺少已解析输入引用")
        result["inputs"] = result_input
    elif input_kind == "null":
        result["inputs"] = None
    elif input_kind == "invalid":
        result["inputs"] = stored["result_input_value"]
    return dict(result)


def read_execution_input_sync(connection: Any, input_id: str) -> object:
    """为数据库升级显式读取冻结输入，不创建运行时兼容存储路径。"""
    rows = {}
    for table in inputs.TABLES:
        column = table.c.id if table is inputs.execution_input else table.c.input_id
        rows[table.name] = list(connection.execute(sa.select(table).where(column == input_id)).mappings())
    if not rows[inputs.execution_input.name]:
        raise ValueError("执行输入不存在")
    return inputs.decode_inputs(rows)


def _validate_asset_ownership_sync(connection: Any, record_id: int) -> None:
    record = (
        connection.execute(sa.select(head.execution_head).where(head.execution_head.c.id == record_id))
        .mappings()
        .first()
    )
    if record is None:
        raise ValueError("执行主记录不存在")
    bindings = list(
        connection.execute(
            sa.select(assets.asset_binding).where(assets.asset_binding.c.record_id == record_id)
        ).mappings()
    )
    columns = ("asset_id", "record_id", "input_id", "account_snapshot_id", "path", "scope", "ordinal", "map_key")
    roots = list(
        connection.execute(
            sa.select(*[assets.asset_snapshot.c[name] for name in columns]).where(
                assets.asset_snapshot.c.asset_id.in_([row["asset_id"] for row in bindings])
            )
        ).mappings()
    )
    owner_rows = list(
        connection.execute(
            sa.select(
                inputs.execution_input.c.id,
                inputs.execution_input.c.account_id,
                inputs.execution_input.c.execution_id,
                inputs.execution_input.c.owner_execution_id,
            ).where(inputs.execution_input.c.id.in_([row["input_id"] for row in roots if row["input_id"] is not None]))
        ).mappings()
    )
    assets.validate_result_asset_owners(record, roots, owner_rows, bindings=bindings)


def read_execution_result_sync(connection: Any, record: Any) -> object:
    """为已类型化数据库的窄范围升级读取结果，保持既有输入与资产身份。"""
    record_id = record if isinstance(record, int) else record["id"] if isinstance(record, Mapping) else record.id
    _validate_asset_ownership_sync(connection, record_id)
    rows = {
        table.name: list(connection.execute(sa.select(table).where(predicate)).mappings())
        for table, predicate in execution_fact_sources(record_ids=(record_id,))
    }
    headers = rows.get(head.execution_head.name, ())
    if not headers:
        raise ValueError("执行主记录不存在")
    stored = headers[0]
    if not stored["facts_complete"]:
        raise ValueError("执行事实未完整提交")
    if stored["input_id"] is not None:
        primary = (
            connection.execute(
                sa.select(inputs.execution_input).where(inputs.execution_input.c.id == stored["input_id"])
            )
            .mappings()
            .first()
        )
        if primary is None:
            raise ValueError("执行输入不存在")
        _validate_input_owner(primary, stored["account_id"], stored["execution_id"])
        if (
            stored["execution_id"] is None
            and connection.scalar(_other_input_reference(stored["input_id"], stored["id"])) is not None
        ):
            raise ValueError("无执行标识的历史输入不能属于其他执行记录")
    result_input = _INPUT_UNSET
    if stored["result_input_kind"] == "value":
        input_id = stored["result_input_id"]
        input_owner = (
            connection.execute(sa.select(inputs.execution_input).where(inputs.execution_input.c.id == input_id))
            .mappings()
            .first()
        )
        if input_owner is None:
            raise ValueError("执行输入不存在")
        _validate_input_owner(input_owner, stored["account_id"], stored["execution_id"])
        if (
            stored["execution_id"] is None
            and connection.scalar(_other_input_reference(input_id, stored["id"])) is not None
        ):
            raise ValueError("无执行标识的历史输入不能属于其他执行记录")
        result_input = read_execution_input_sync(connection, input_id)
    result = decode_result_facts(rows, result_input=result_input)
    if isinstance(result, dict):
        return hydrate_result_notification_context_sync(
            connection, result, account_id=stored["account_id"], execution_id=stored["execution_id"]
        )
    return result


async def read_execution_detail(session: Any, record: Any) -> dict[str, object]:
    """提供详情、通知、审计共用的显式完整业务表示。"""
    input_id = getattr(record, "input_id", None)
    if input_id is not None:
        await _input_row(session, input_id, record.account_id, record.execution_id, record.id)
    return {
        "id": record.id,
        "account_id": record.account_id,
        "execution_id": record.execution_id,
        "created_at": record.created_at,
        "is_success": record.is_success,
        "input": await read_execution_input(session, input_id) if input_id is not None else None,
        "result": await read_execution_result(session, record),
    }


async def read_execution_targets(session: Any, input_id: str, *, role: str = "curr_target") -> dict[str, object]:
    """只查询一种冻结目标，未提供与异常形状不伪造为目标映射。"""
    present, value = await inputs.load_input_target(session, input_id, role=role)
    if not present:
        raise ValueError("执行输入未提供该目标")
    if not isinstance(value, dict):
        raise ValueError("执行输入目标不是映射")
    return value


def business_result(payload: object, *, is_success: int | None = None) -> object:
    """迁移核对使用显式业务 DTO，表示细节不属于等价条件。"""
    batch = encode_result_facts(0, payload, is_success=is_success)
    rows = {table.name: values for table, values in batch.items()}
    result_input = (
        inputs.canonical_input(payload["inputs"])
        if isinstance(payload, Mapping) and isinstance(payload.get("inputs"), Mapping)
        else _INPUT_UNSET
    )
    return decode_result_facts(rows, result_input=result_input)
