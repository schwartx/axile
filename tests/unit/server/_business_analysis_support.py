"""分析测试显式保存规范事实；不为 ORM 恢复已删除的原始文档属性。"""

from collections import defaultdict
from types import SimpleNamespace

import sqlalchemy as sa

from axile.server.db.models import ExecuteRecord, TargetWeightSnapshot


def analysis_record(*, input=None, result=None, **metadata):
    """构造可修改的测试输入对象，持久化须显式调用 persist_records。"""
    return SimpleNamespace(**metadata, input={} if input is None else input, result={} if result is None else result)


def install_analysis_triggers(connection):
    """复用发布版本的触发器，并最终安装规范事实失效与所有权约束。"""
    from axile.server.alembic.business0037_invalidation import install_triggers
    from axile.server.business_facts.repository import execution_fact_tables
    from axile.server.db.models import AccountAssetSnapshot
    from axile.server.db.models.target_weight_snapshot import target_weight_snapshot_weight
    from tests.unit.server.test_initial_migration import _MIGRATIONS_DIR, _load_migration

    _load_migration(_MIGRATIONS_DIR / "0012_performance_snapshots.py").install_triggers(connection)
    _load_migration(_MIGRATIONS_DIR / "0020_account_settings.py").install_settings_trigger(connection)
    _load_migration(_MIGRATIONS_DIR / "0034_analysis_rust_facts.py").install_triggers(connection)
    _load_migration(_MIGRATIONS_DIR / "0035_analysis_structured_results.py").install_triggers(connection)
    install_triggers(
        connection,
        (
            *execution_fact_tables(),
            AccountAssetSnapshot.__table__,
            TargetWeightSnapshot.__table__,
            target_weight_snapshot_weight,
        ),
    )


async def _insert_batches(session, batches):
    for table, rows in batches.items():
        for start in range(0, len(rows), 100):
            await session.execute(table.insert(), rows[start : start + 100])


def _new_head(record):
    return ExecuteRecord(
        **{name: getattr(record, name) for name in ("id", "account_id", "execution_id", "created_at", "is_success")}
    )


def _encode_new_record(record, head, input_batches, result_batches):
    from axile.server.business_facts import inputs
    from axile.server.business_facts.repository import encode_result_facts

    input_id = f"analysis-fixture-{record.account_id}-{record.id}"
    for table, rows in inputs.encode_inputs(
        input_id,
        record.input,
        account_id=record.account_id,
        execution_id=record.execution_id,
        created_at=record.created_at,
    ).items():
        input_batches[table].extend(rows)
    head.input_id = input_id
    for table, rows in encode_result_facts(record.id, record.result, is_success=record.is_success).items():
        if table.name == "executerecord":
            for name, value in rows[0].items():
                if name != "id":
                    setattr(head, name, value)
        else:
            result_batches[table].extend(rows)


async def _rewrite_input(session, record, head):
    from axile.server.business_facts import inputs
    from axile.server.business_facts.repository import read_execution_input

    if head.input_id is None or await read_execution_input(session, head.input_id) == record.input:
        return
    # 测试历史事实修改的失效；生产前置输入仍由仓库保持冻结。
    for table in reversed(inputs.TABLES):
        if table is not inputs.execution_input:
            await session.execute(table.delete().where(table.c.input_id == head.input_id))
    batches = inputs.encode_inputs(
        head.input_id,
        record.input,
        account_id=head.account_id,
        execution_id=head.execution_id,
        created_at=head.created_at,
    )
    values = batches.pop(inputs.execution_input)[0]
    await session.execute(
        inputs.execution_input.update()
        .where(inputs.execution_input.c.id == head.input_id)
        .values(**{key: value for key, value in values.items() if key != "id"})
    )
    await _insert_batches(session, batches)


async def _project_records(session, records):
    from axile.server.analysis_observations import observation_fact_values
    from axile.server.analysis_source import source_fact_values
    from axile.server.db.models.analysis_replay import replay_observation, replay_observation_symbol
    from axile.server.db.models.analysis_source import SOURCE_FACT_TABLES, source_execution

    fact_batches = defaultdict(list)
    record_ids = [record.id for record in records]
    for table in (*SOURCE_FACT_TABLES, replay_observation_symbol, replay_observation):
        await session.execute(table.delete().where(table.c.record_id.in_(record_ids)))
    for record in records:
        scalar, children = source_fact_values(record)
        fact_batches[source_execution].append(scalar)
        for table, rows in children.items():
            fact_batches[table].extend(rows)
        scalar, rows = observation_fact_values(record)
        fact_batches[replay_observation].append(scalar)
        fact_batches[replay_observation_symbol].extend(rows)
    await _insert_batches(session, fact_batches)


async def persist_records(session, records, *, project=False):
    """正式编码器批量保存测试事实，按需同时生成已冻结的分析投影。"""
    from axile.server.business_facts.repository import store_execution_facts

    existing = {
        head.id: head
        for head in (
            await session.scalars(
                sa.select(ExecuteRecord).where(ExecuteRecord.id.in_([record.id for record in records]))
            )
        ).all()
    }
    input_batches, result_batches = defaultdict(list), defaultdict(list)
    new_heads = []
    for record in records:
        head = existing.get(record.id)
        if head is None and record.id is not None and "inputs" not in record.result:
            head = _new_head(record)
            _encode_new_record(record, head, input_batches, result_batches)
            new_heads.append(head)
            continue
        if head is None:
            head = _new_head(record)
            session.add(head)
            await session.flush()
        else:
            for name in ("account_id", "execution_id", "created_at", "is_success"):
                setattr(head, name, getattr(record, name))
            await _rewrite_input(session, record, head)
        await store_execution_facts(session, head, raw_input=record.input, raw_result=record.result)
        record.id = head.id
    await _insert_batches(session, input_batches)
    session.add_all(new_heads)
    await session.flush()
    await _insert_batches(session, result_batches)
    if project:
        await _project_records(session, records)


async def load_record(session, record_id):
    """显式读取测试记录；返回可修改的普通对象。"""
    from axile.server.business_facts.repository import read_execution_detail

    return SimpleNamespace(**await read_execution_detail(session, await session.get(ExecuteRecord, record_id)))


async def persist_target(session, *, normalized_weights=None, raw_weights=None, snapshot=None, **metadata):
    """保留目标快照头身份，使用正式编码器写入权重明细。"""
    from axile.server.db.models.target_weight_snapshot import target_weight_snapshot_weight
    from axile.server.target_weight_snapshots import encode_snapshot_weights

    payload = {"normalized_weights": normalized_weights, "raw_weights": raw_weights}
    columns, _ = encode_snapshot_weights(0, payload)
    if snapshot is None:
        snapshot = TargetWeightSnapshot(**metadata, **columns)
        session.add(snapshot)
        await session.flush()
    else:
        for name, value in columns.items():
            setattr(snapshot, name, value)
        await session.execute(
            target_weight_snapshot_weight.delete().where(target_weight_snapshot_weight.c.snapshot_id == snapshot.id)
        )
    _, batches = encode_snapshot_weights(snapshot.id, payload)
    for table, rows in batches.items():
        if rows:
            await session.execute(table.insert(), rows)
    await session.flush()
    return snapshot
