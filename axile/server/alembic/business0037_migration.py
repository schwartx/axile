"""Bounded revision 0037 conversion using the canonical domain encoders.

No historical payload is logged. A conversion error identifies only its
database row and JSON path, and the enclosing migration savepoint rolls back
all schema and data changes.
"""

import json
import uuid
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

import sqlalchemy as sa

from axile.server.business_facts import assets, head, inputs, repository

_BATCH_SIZE = 100
_NAMESPACE = uuid.UUID("4e800ba5-b43d-5ec3-bbb7-f32ede910037")
_TARGET_FIELDS = ("curr_target", "last_target", "strategy_target", "account_target", "sizing_context")


def _identity(kind: str, row_id: int) -> str:
    return str(uuid.uuid5(_NAMESPACE, f"{kind}:{row_id}"))


def _batches(connection, table: str, columns: Sequence[str] | None = None) -> Iterator[list[Mapping[str, Any]]]:
    """Keyset-paginate parent rows without retaining account history in memory."""
    previous = -1
    selected = ",".join(columns) if columns else "*"
    while True:
        rows = (
            connection.execute(
                sa.text(f"SELECT {selected} FROM {table} WHERE id>:previous ORDER BY id LIMIT :limit"),
                {"previous": previous, "limit": _BATCH_SIZE},
            )
            .mappings()
            .all()
        )
        if not rows:
            return
        yield rows
        previous = rows[-1]["id"]


def _json(value: object, *, table: str, row_id: int, path: str) -> object:
    if value is None:
        return None
    try:
        return json.loads(value) if isinstance(value, (str, bytes, bytearray)) else value
    except (ValueError, TypeError, UnicodeError) as error:
        raise ValueError(f"Cannot migrate {table} id={row_id} path={path}: invalid JSON") from error


def _same(left: object, right: object) -> bool:
    """Compare business values, independent of object order and int/float spelling."""
    return left == right


def _canonical_asset(value: object) -> object:
    root, positions = assets._encode_observation(value, asset_id="check", path="", scope="after", input_id="check")
    return assets._decode_observation(root, positions)


def _write(connection, batch: Mapping[sa.Table, Sequence[Mapping[str, object]]]) -> None:
    for table, rows in batch.items():
        for offset in range(0, len(rows), _BATCH_SIZE):
            connection.execute(table.insert(), list(rows[offset : offset + _BATCH_SIZE]))


def _input(
    connection,
    payload: object,
    *,
    kind: str,
    row: Mapping[str, Any],
    execution_id: str | None,
    owner_execution_id: str | None = None,
) -> str:
    identity = _identity(kind, row["id"])
    batch = inputs.encode_inputs(
        identity,
        payload,
        account_id=row["account_id"],
        execution_id=execution_id,
        created_at=row["created_at"],
        owner_execution_id=owner_execution_id or execution_id,
    )
    _write(connection, batch)
    return identity


def backfill_records(connection) -> None:
    """Convert every terminal record, retaining independent legacy NULL identities."""
    for batch in _batches(connection, "executerecord"):
        for row in batch:
            record_id = row["id"]
            raw_input = _json(row["raw_input"], table="executerecord", row_id=record_id, path="/raw_input")
            raw_result = _json(row["raw_result"], table="executerecord", row_id=record_id, path="/raw_result")
            try:
                try:
                    input_id = _input(
                        connection, raw_input, kind="record-input", row=row, execution_id=row["execution_id"]
                    )
                except (ValueError, TypeError, KeyError, OverflowError):
                    raise ValueError(f"Cannot migrate executerecord id={record_id} path=/raw_input") from None
                result_input_id = None
                if isinstance(raw_result, Mapping) and isinstance(raw_result.get("inputs"), Mapping):
                    result_input = inputs.sanitize_input_payload(raw_result["inputs"])
                    if _same(inputs.canonical_input(raw_input), inputs.canonical_input(result_input)):
                        result_input_id = input_id
                    else:
                        result_input_id = _input(
                            connection,
                            result_input,
                            kind="record-result-input",
                            row=row,
                            execution_id=None,
                            owner_execution_id=row["execution_id"],
                        )
                raw_result = _compact_notification_capture(connection, row, raw_result)
                facts = repository.encode_result_facts(record_id, raw_result, is_success=row["is_success"])
                header = facts.pop(head.execution_head)[0]
                values = {key: value for key, value in header.items() if key != "id"}
                values.update(input_id=input_id, result_input_id=result_input_id)
                values["facts_complete"] = True
                connection.execute(
                    head.execution_head.update().where(head.execution_head.c.id == record_id).values(**values)
                )
                _write(connection, facts)
            except (ValueError, TypeError, KeyError, OverflowError) as error:
                if isinstance(error, ValueError) and str(error).startswith("Cannot migrate executerecord"):
                    raise
                raise ValueError(f"Cannot migrate executerecord id={record_id} path=/raw_result") from None


def _record_for_execution(connection, execution_id: str | None) -> Mapping[str, Any] | None:
    if execution_id is None:
        return None
    return (
        connection.execute(sa.select(head.execution_head).where(head.execution_head.c.execution_id == execution_id))
        .mappings()
        .first()
    )


def _notification_event(connection, record: Mapping[str, Any], event_id: str) -> Mapping[str, Any] | None:
    if record["execution_id"] is None or event_id != "execution:" + record["execution_id"]:
        return None
    return (
        connection.execute(
            sa.text(
                "SELECT id,account_id,event_type,context FROM notification_event "
                "WHERE id=:event AND account_id=:account AND event_type='execution.finished'"
            ),
            {"event": event_id, "account": record["account_id"]},
        )
        .mappings()
        .one_or_none()
    )


def _compact_notification_capture(connection, record: Mapping[str, Any], result: object) -> object:
    if not isinstance(result, Mapping) or not isinstance(result.get("extra"), Mapping):
        return result
    extra = result["extra"]
    captured = extra.get("server_notification_context")
    if not isinstance(captured, Mapping) or record["execution_id"] is None:
        return result
    from axile.server.business_facts.notification_context import compact_captured_context, hydrate_captured_context

    event_id = "execution:" + record["execution_id"]
    event = _notification_event(connection, record, event_id)
    if event is None:
        return result
    event_context = _json(event["context"], table="notification_event", row_id=record["id"], path="/context")
    if not isinstance(event_context, Mapping):
        return result
    reference = compact_captured_context(captured, event_context, event_id=event_id)
    if reference is None or not _same(
        captured, hydrate_captured_context(reference, event_id=event_id, event_context=event_context)
    ):
        return result
    return {**result, "extra": {**extra, "server_notification_context": reference}}


def _hydrate_notification_capture(
    connection, record: Mapping[str, Any], result: Mapping[str, object]
) -> dict[str, object]:
    extra = result.get("extra")
    if not isinstance(extra, Mapping):
        return dict(result)
    captured = extra.get("server_notification_context")
    if not isinstance(captured, Mapping):
        return dict(result)
    from axile.server.business_facts.notification_context import (
        hydrate_captured_context,
        is_notification_context_reference,
    )

    if not is_notification_context_reference(captured):
        return dict(result)
    event_id = captured.get("event_id")
    if not isinstance(event_id, str):
        raise ValueError("Invalid notification event identity")
    event = _notification_event(connection, record, event_id)
    if event is None:
        raise ValueError(f"Cannot resolve executerecord id={record['id']} path=/extra/server_notification_context")
    context = _json(event["context"], table="notification_event", row_id=record["id"], path="/context")
    if not isinstance(context, Mapping):
        raise ValueError(f"Cannot resolve executerecord id={record['id']} path=/extra/server_notification_context")
    hydrated = hydrate_captured_context(captured, event_id=event_id, event_context=context)
    return {**result, "extra": {**extra, "server_notification_context": hydrated}}


def _load_input(connection, input_id: str | None) -> object:
    if not isinstance(input_id, str):
        raise ValueError("Frozen input identity is missing")
    loaded = {
        table.name: connection.execute(
            sa.select(table).where((table.c.id if table is inputs.execution_input else table.c.input_id) == input_id)
        )
        .mappings()
        .all()
        for table in inputs.TABLES
    }
    return inputs.decode_inputs(loaded)


def _ref(view: str, *, input_id: str | None = None, asset_id: str | None = None) -> dict[str, Any]:
    reference: dict[str, object] = {"version": 1, "view": view}
    if input_id is not None:
        reference["input_id"] = input_id
    if asset_id is not None:
        reference["asset_id"] = asset_id
    return {"business_facts_ref": reference}


def _update_artifact(connection, row: Mapping[str, Any], reference: Mapping[str, object]) -> None:
    reference = dict(reference)
    raw_marker = reference["business_facts_ref"]
    if not isinstance(raw_marker, Mapping):
        raise ValueError("Invalid business fact reference")
    marker = dict(raw_marker)
    marker["legacy_schema_version"] = row["schema_version"]
    reference["business_facts_ref"] = marker
    connection.execute(
        sa.text("UPDATE execution_artifact SET content=:content,schema_version=3 WHERE id=:id"),
        {"content": json.dumps(reference, ensure_ascii=False), "id": row["id"]},
    )


def backfill_artifacts(connection) -> None:
    """Give frozen audit inputs independent identities when they differ from terminal input."""
    for batch in _batches(connection, "execution_artifact"):
        for row in batch:
            if row["artifact_type"].lower() != "standard_input":
                continue
            content = _json(row["content"], table="execution_artifact", row_id=row["id"], path="/content")
            if not isinstance(content, Mapping) or set(content) != {"input"}:
                # A historical direct-root artifact has different envelope/channel semantics.
                continue
            record = _record_for_execution(connection, row["execution_id"])
            input_id = record["input_id"] if record else None
            sanitized = inputs.sanitize_input_payload(content["input"])
            if input_id is None or not _same(_load_input(connection, input_id), inputs.canonical_input(sanitized)):
                account_id = (
                    record["account_id"]
                    if record
                    else connection.execute(
                        sa.text(
                            "SELECT account_id FROM execution_event WHERE execution_id=:execution ORDER BY id LIMIT 1"
                        ),
                        {"execution": row["execution_id"]},
                    ).scalar()
                )
                if account_id is None:
                    # Without a truthful account owner, retain the independent original audit artifact.
                    continue
                owner = {**dict(row), "account_id": account_id}
                input_id = _input(
                    connection,
                    sanitized,
                    kind="artifact-input",
                    row=owner,
                    execution_id=None if record else row["execution_id"],
                    owner_execution_id=row["execution_id"],
                )
            if not _same(_load_input(connection, input_id), inputs.canonical_input(sanitized)):
                raise ValueError(f"Conversion mismatch execution_artifact id={row['id']} path=/input")
            _update_artifact(connection, row, _ref("input", input_id=input_id))
    _backfill_artifact_views(connection)


def _artifact_input_owner(connection, row: Mapping[str, Any]) -> tuple[str | None, Mapping[str, Any] | None]:
    record = _record_for_execution(connection, row["execution_id"])
    if record:
        return record["input_id"], record
    owner = (
        connection.execute(
            sa.select(inputs.execution_input).where(inputs.execution_input.c.execution_id == row["execution_id"])
        )
        .mappings()
        .first()
    )
    return (owner["id"], owner) if owner else (None, None)


def _backfill_artifact_views(connection) -> None:
    views = {
        "target_snapshot": (
            "targets",
            ("curr_target", "last_target", "strategy_target", "account_target", "sizing_context"),
        ),
        "trade_rules_snapshot": ("rules", ("trade_rules", "futures_close_policy")),
    }
    for batch in _batches(connection, "execution_artifact"):
        for row in batch:
            artifact_type = row["artifact_type"].lower()
            if artifact_type not in (*views, "account_snapshot_before", "account_snapshot"):
                continue
            content = _json(row["content"], table="execution_artifact", row_id=row["id"], path="/content")
            if not isinstance(content, Mapping) or "business_facts_ref" in content:
                continue
            input_id, owner = _artifact_input_owner(connection, row)
            if owner is None or input_id is None:
                continue
            if artifact_type in views:
                view, fields = views[artifact_type]
                selected = inputs.normalize_targets({key: value for key, value in content.items() if key in fields})
                if not selected:
                    continue
                payload = _load_input(connection, input_id)
                existing = (
                    {key: value for key, value in payload.items() if key in fields}
                    if isinstance(payload, Mapping)
                    else None
                )
                if not _same(inputs.canonical_input(selected), inputs.canonical_input(existing)):
                    input_id = _input(
                        connection,
                        selected,
                        kind="artifact-view-input",
                        row={**dict(row), "account_id": owner["account_id"]},
                        execution_id=None,
                        owner_execution_id=row["execution_id"],
                    )
                decoded = _load_input(connection, input_id)
                if not isinstance(decoded, Mapping) or not _same(
                    {key: value for key, value in decoded.items() if key in selected}, selected
                ):
                    raise ValueError(f"Conversion mismatch execution_artifact id={row['id']} path=/content")
                updated = {key: value for key, value in content.items() if key not in fields}
                updated.update(_ref(view, input_id=input_id))
            else:
                if "account_assets" not in content:
                    continue
                before = artifact_type == "account_snapshot_before"
                asset_id = (
                    assets.input_asset_fact_id(input_id) if before else assets.input_after_asset_fact_id(input_id)
                )
                record = _record_for_execution(connection, row["execution_id"])
                result = reconstruct_result(connection, record) if record else None
                if (
                    not before
                    and record is not None
                    and isinstance(result, Mapping)
                    and "account_assets" in result
                    and _same(result["account_assets"], _canonical_asset(content["account_assets"]))
                ):
                    asset_id = assets.asset_fact_id(record["id"])
                else:
                    encoder = assets.encode_input_assets if before else assets.encode_input_after_assets
                    _write(connection, encoder(input_id, {"account_assets": content["account_assets"]}))
                if not _same(_asset_value(connection, asset_id), _canonical_asset(content["account_assets"])):
                    raise ValueError(f"Conversion mismatch execution_artifact id={row['id']} path=/account_assets")
                updated = {key: value for key, value in content.items() if key != "account_assets"}
                updated.update(
                    _ref("assets_before" if before else "assets_after", input_id=input_id, asset_id=asset_id)
                )
            _update_artifact(connection, row, updated)


def backfill_decision_events(connection) -> None:
    """Replace historical target decision copies with immutable fact references."""
    for batch in _batches(connection, "execution_event"):
        for row in batch:
            if row["event_type"].lower() != "target_computed":
                continue
            details = _json(row["details"], table="execution_event", row_id=row["id"], path="/details")
            if not isinstance(details, Mapping):
                continue
            decision = details.get("decision")
            if not isinstance(decision, Mapping) or "business_facts_ref" in decision:
                continue
            selected = inputs.normalize_targets(
                {key: value for key, value in decision.items() if key in _TARGET_FIELDS}
            )
            if not selected:
                continue
            owner = {**dict(row), "created_at": row["ts_local_created"]}
            record = _record_for_execution(connection, row["execution_id"])
            input_id = record["input_id"] if record else None
            frozen = _load_input(connection, input_id) if input_id else None
            if not isinstance(frozen, Mapping) or any(frozen.get(key) != value for key, value in selected.items()):
                input_id = _input(
                    connection,
                    selected,
                    kind="event-decision-input",
                    row=owner,
                    execution_id=None,
                    owner_execution_id=row["execution_id"],
                )
            decoded = _load_input(connection, input_id)
            if not isinstance(decoded, Mapping) or any(decoded.get(key) != value for key, value in selected.items()):
                raise ValueError(f"Conversion mismatch execution_event id={row['id']} path=/details/decision")
            reference = _ref("targets", input_id=input_id)
            reference["business_facts_ref"]["select_fields"] = list(selected)
            updated_decision = {key: value for key, value in decision.items() if key not in _TARGET_FIELDS}
            updated_decision.update(reference)
            updated = {**details, "decision": updated_decision}
            connection.execute(
                sa.text("UPDATE execution_event SET details=:details WHERE id=:id"),
                {"details": json.dumps(updated, ensure_ascii=False), "id": row["id"]},
            )


def compact_summary_artifacts(connection) -> None:
    """Compact only summary evidence that the immutable terminal result reproduces exactly."""
    for batch in _batches(connection, "execution_artifact"):
        for row in batch:
            if row["artifact_type"].lower() != "execution_summary":
                continue
            content = _json(row["content"], table="execution_artifact", row_id=row["id"], path="/content")
            if not isinstance(content, Mapping) or "business_facts_ref" in content:
                continue
            record = _record_for_execution(connection, row["execution_id"])
            if record is None:
                continue
            legacy_result = connection.execute(
                sa.text("SELECT raw_result FROM executerecord WHERE id=:id"), {"id": record["id"]}
            ).scalar_one()
            result = _json(legacy_result, table="executerecord", row_id=record["id"], path="/raw_result")
            if not isinstance(result, Mapping):
                continue
            from axile.server.business_facts.artifacts import compact_summary_content, hydrate_summary_content

            try:
                compacted = compact_summary_content(content, result, record_id=record["id"])
                if compacted is None:
                    continue
                hydrated = hydrate_summary_content(compacted, result, include_reference=False)
            except (ValueError, TypeError, KeyError):
                continue
            if isinstance(hydrated, Mapping):
                _update_artifact(connection, row, compacted)


def backfill_asset_snapshots(connection) -> None:
    """Reuse identical terminal observations and retain differing/manual observations."""
    table = sa.Table("account_asset_snapshot", sa.MetaData(), autoload_with=connection)
    for batch in _batches(connection, table.name):
        for row in batch:
            value = _json(row["assets"], table=table.name, row_id=row["id"], path="/assets")
            record = _record_for_execution(connection, row["execution_id"])
            raw_result = None
            if record:
                raw = connection.execute(
                    sa.text("SELECT raw_result FROM executerecord WHERE id=:id"), {"id": record["id"]}
                ).scalar_one()
                raw_result = _json(raw, table="executerecord", row_id=record["id"], path="/raw_result")
            if (
                record is not None
                and isinstance(raw_result, Mapping)
                and "account_assets" in raw_result
                and _same(_canonical_asset(raw_result["account_assets"]), _canonical_asset(value))
            ):
                asset_id = assets.asset_fact_id(record["id"])
            else:
                _write(
                    connection,
                    assets.encode_snapshot_assets(
                        row["id"],
                        {"account_assets": value},
                        result=raw_result if isinstance(raw_result, Mapping) else None,
                    ),
                )
                asset_id = assets.snapshot_asset_fact_id(row["id"])
            connection.execute(table.update().where(table.c.id == row["id"]).values(asset_fact_id=asset_id))


def backfill_target_snapshots(connection) -> None:
    """Move calculated target maps into typed ordered rows using the live encoder."""
    from axile.server.target_weight_snapshots import encode_snapshot_weights

    table = sa.Table("target_weight_snapshot", sa.MetaData(), autoload_with=connection)
    for batch in _batches(connection, table.name):
        for row in batch:
            payload = {
                role: _json(row[role], table=table.name, row_id=row["id"], path="/" + role)
                for role in ("raw_weights", "normalized_weights")
            }
            columns, facts = encode_snapshot_weights(row["id"], payload)
            connection.execute(table.update().where(table.c.id == row["id"]).values(**columns))
            _write(connection, facts)


def _loaded_result(connection, record: Mapping[str, Any]) -> dict[str, list[Mapping[str, Any]]]:
    loaded: dict[str, list[Mapping[str, Any]]] = {head.execution_head.name: [record]}
    for table in repository.execution_fact_tables():
        if table is head.execution_head or "record_id" not in table.c:
            continue
        loaded[table.name] = (
            connection.execute(sa.select(table).where(table.c.record_id == record["id"])).mappings().all()
        )
    asset_ids = [row["asset_id"] for row in loaded.get("business_asset_binding", ())]
    if asset_ids:
        for table in assets.TABLES:
            if "asset_id" in table.c and table is not assets.asset_binding:
                loaded[table.name] = (
                    connection.execute(sa.select(table).where(table.c.asset_id.in_(asset_ids))).mappings().all()
                )
    return loaded


def reconstruct_result(connection, record: Mapping[str, Any]) -> object:
    """Read one fixed business view for downgrade or migration verification."""
    loaded = _loaded_result(connection, record)
    result_input = (
        _load_input(connection, record["result_input_id"])
        if record["result_input_kind"] == "value"
        else repository._INPUT_UNSET
    )
    result = repository.decode_result_facts(loaded, result_input=result_input)
    return _hydrate_notification_capture(connection, record, result) if isinstance(result, Mapping) else result


def restore_records(connection) -> None:
    """Restore compact documents in bounded batches before removing canonical tables."""
    for batch in _batches(connection, "executerecord"):
        for row in batch:
            row = (
                connection.execute(sa.select(head.execution_head).where(head.execution_head.c.id == row["id"]))
                .mappings()
                .one()
            )
            raw_input = _load_input(connection, row["input_id"]) if row["input_id"] else None
            raw_result = reconstruct_result(connection, row)
            connection.execute(
                sa.text("UPDATE executerecord SET raw_input=:input,raw_result=:result WHERE id=:id"),
                {
                    "id": row["id"],
                    "input": json.dumps(raw_input, ensure_ascii=False),
                    "result": json.dumps(raw_result, ensure_ascii=False),
                },
            )


def _asset_value(connection, asset_id: str) -> object:
    row = (
        connection.execute(sa.select(assets.asset_snapshot).where(assets.asset_snapshot.c.asset_id == asset_id))
        .mappings()
        .one()
    )
    positions = (
        connection.execute(sa.select(assets.asset_position).where(assets.asset_position.c.asset_id == asset_id))
        .mappings()
        .all()
    )
    return assets._decode_observation(row, positions)


def verify_conversion(connection) -> None:
    """Compare one reconstructed document at a time before deleting legacy columns."""
    for batch in _batches(connection, "executerecord"):
        for legacy in batch:
            record = (
                connection.execute(sa.select(head.execution_head).where(head.execution_head.c.id == legacy["id"]))
                .mappings()
                .one()
            )
            expected_input = inputs.sanitize_input_payload(
                _json(legacy["raw_input"], table="executerecord", row_id=legacy["id"], path="/raw_input")
            )
            expected_result = _json(
                legacy["raw_result"], table="executerecord", row_id=legacy["id"], path="/raw_result"
            )
            if isinstance(expected_result, Mapping) and isinstance(expected_result.get("inputs"), Mapping):
                expected_result = {
                    **expected_result,
                    "inputs": inputs.sanitize_input_payload(expected_result["inputs"]),
                }
            if not _same(inputs.canonical_input(expected_input), _load_input(connection, record["input_id"])):
                raise ValueError(f"Conversion mismatch executerecord id={legacy['id']} path=/raw_input")
            if not _same(
                repository.business_result(expected_result, is_success=record["is_success"]),
                reconstruct_result(connection, record),
            ):
                raise ValueError(f"Conversion mismatch executerecord id={legacy['id']} path=/raw_result")
    for batch in _batches(connection, "account_asset_snapshot"):
        for row in batch:
            original = _json(row["assets"], table="account_asset_snapshot", row_id=row["id"], path="/assets")
            if not _same(_canonical_asset(original), _asset_value(connection, row["asset_fact_id"])):
                raise ValueError(f"Conversion mismatch account_asset_snapshot id={row['id']} path=/assets")
    _verify_target_snapshots(connection)


def _verify_target_snapshots(connection) -> None:
    from axile.server.db.models.target_weight_snapshot import TargetWeightSnapshot, target_weight_snapshot_weight
    from axile.server.target_weight_snapshots import decode_snapshot_weights

    for batch in _batches(connection, "target_weight_snapshot"):
        for row in batch:
            parent = (
                connection.execute(
                    sa.select(getattr(TargetWeightSnapshot, "__table__")).where(TargetWeightSnapshot.id == row["id"])
                )
                .mappings()
                .one()
            )
            children = (
                connection.execute(
                    sa.select(target_weight_snapshot_weight).where(
                        target_weight_snapshot_weight.c.snapshot_id == row["id"]
                    )
                )
                .mappings()
                .all()
            )
            expected = {
                role: _json(row[role], table="target_weight_snapshot", row_id=row["id"], path="/" + role)
                for role in ("raw_weights", "normalized_weights")
            }
            if not _same(expected, decode_snapshot_weights(parent, children)):
                raise ValueError(f"Conversion mismatch target_weight_snapshot id={row['id']} path=/weights")


def preflight_downgrade(connection) -> None:
    """Refuse evidence loss when pending inputs/assets have no legacy representation."""
    unsupported = connection.exec_driver_sql(
        "SELECT id FROM execution_input AS i WHERE NOT EXISTS(SELECT 1 FROM executerecord "
        "WHERE input_id=i.id OR result_input_id=i.id) AND NOT EXISTS(SELECT 1 FROM execution_artifact "
        "WHERE json_valid(content) AND json_extract(content,'$.business_facts_ref.input_id')=i.id) "
        "AND NOT EXISTS(SELECT 1 FROM execution_event WHERE json_valid(details) "
        "AND json_extract(details,'$.decision.business_facts_ref.input_id')=i.id) LIMIT 1"
    ).first()
    if unsupported:
        raise ValueError(
            "Cannot downgrade 0037: unrepresented pending execution input; retain this database or restore its backup"
        )
    unsupported_asset = connection.exec_driver_sql(
        "SELECT asset_id FROM business_asset_snapshot AS a "
        "WHERE NOT EXISTS(SELECT 1 FROM business_asset_binding WHERE asset_id=a.asset_id) "
        "AND NOT EXISTS(SELECT 1 FROM account_asset_snapshot WHERE asset_fact_id=a.asset_id) "
        "AND NOT EXISTS(SELECT 1 FROM execution_artifact WHERE json_valid(content) "
        "AND json_extract(content,'$.business_facts_ref.asset_id')=a.asset_id) LIMIT 1"
    ).first()
    if unsupported_asset:
        raise ValueError(
            "Cannot downgrade 0037: unrepresented pending asset evidence; retain this database or restore its backup"
        )


def restore_artifacts(connection) -> None:
    """Replace explicit canonical references with their frozen legacy evidence views."""
    for batch in _batches(connection, "execution_artifact"):
        for row in batch:
            content = _json(row["content"], table="execution_artifact", row_id=row["id"], path="/content")
            if not isinstance(content, Mapping) or not isinstance(content.get("business_facts_ref"), Mapping):
                continue
            reference = content["business_facts_ref"]
            view = reference["view"]
            if view == "result":
                from axile.server.business_facts.artifacts import hydrate_summary_content

                record = (
                    connection.execute(
                        sa.select(head.execution_head).where(head.execution_head.c.id == reference["record_id"])
                    )
                    .mappings()
                    .one_or_none()
                )
                if record is None or record["execution_id"] != row["execution_id"]:
                    raise ValueError(f"Cannot restore execution_artifact id={row['id']} path=/result_reference")
                fields = hydrate_summary_content(
                    content, reconstruct_result(connection, record), include_reference=False
                )
            elif view in {"assets_before", "assets_after"}:
                fields = {"account_assets": _asset_value(connection, reference["asset_id"])}
            else:
                payload = _load_input(connection, reference["input_id"])
                if view == "input":
                    fields = {"input": payload}
                else:
                    if not isinstance(payload, Mapping):
                        raise ValueError("Frozen input view is unavailable")
                    keys = _TARGET_FIELDS if view == "targets" else ("trade_rules", "futures_close_policy")
                    fields = {key: payload[key] for key in keys if key in payload}
            restored = {key: value for key, value in content.items() if key != "business_facts_ref"}
            restored.update(fields)
            connection.execute(
                sa.text("UPDATE execution_artifact SET content=:content,schema_version=:version WHERE id=:id"),
                {
                    "content": json.dumps(restored, ensure_ascii=False),
                    "version": reference.get("legacy_schema_version", 2),
                    "id": row["id"],
                },
            )


def restore_decision_events(connection) -> None:
    """Restore target decision views before canonical input tables are removed."""
    for batch in _batches(connection, "execution_event"):
        for row in batch:
            details = _json(row["details"], table="execution_event", row_id=row["id"], path="/details")
            if not isinstance(details, Mapping):
                continue
            decision = details.get("decision")
            if not isinstance(decision, Mapping):
                continue
            reference = decision.get("business_facts_ref")
            if not isinstance(reference, Mapping):
                continue
            if reference.get("view") != "targets":
                raise ValueError(f"Cannot restore execution_event id={row['id']} path=/details/decision")
            payload = _load_input(connection, reference["input_id"])
            if not isinstance(payload, Mapping):
                raise ValueError(f"Cannot restore execution_event id={row['id']} path=/details/decision")
            selector = reference.get("select_fields")
            if not isinstance(selector, list) or any(
                key not in _TARGET_FIELDS or key not in payload for key in selector
            ):
                raise ValueError(f"Cannot restore execution_event id={row['id']} path=/details/decision")
            restored = {key: value for key, value in decision.items() if key != "business_facts_ref"}
            restored.update({key: payload[key] for key in selector})
            connection.execute(
                sa.text("UPDATE execution_event SET details=:details WHERE id=:id"),
                {"details": json.dumps({**details, "decision": restored}, ensure_ascii=False), "id": row["id"]},
            )


def restore_asset_snapshots(connection) -> None:
    """Restore each legacy asset document from its immutable observation reference."""
    for batch in _batches(connection, "account_asset_snapshot"):
        for row in batch:
            connection.execute(
                sa.text("UPDATE account_asset_snapshot SET assets=:assets WHERE id=:id"),
                {
                    "assets": json.dumps(_asset_value(connection, row["asset_fact_id"]), ensure_ascii=False),
                    "id": row["id"],
                },
            )


def restore_target_snapshots(connection) -> None:
    """Restore ordered weight documents without recomputing strategy values."""
    from axile.server.db.models.target_weight_snapshot import TargetWeightSnapshot, target_weight_snapshot_weight
    from axile.server.target_weight_snapshots import decode_snapshot_weights

    for batch in _batches(connection, "target_weight_snapshot"):
        for row in batch:
            parent = (
                connection.execute(
                    sa.select(getattr(TargetWeightSnapshot, "__table__")).where(TargetWeightSnapshot.id == row["id"])
                )
                .mappings()
                .one()
            )
            children = (
                connection.execute(
                    sa.select(target_weight_snapshot_weight).where(
                        target_weight_snapshot_weight.c.snapshot_id == row["id"]
                    )
                )
                .mappings()
                .all()
            )
            payload = decode_snapshot_weights(parent, children)
            connection.execute(
                sa.text(
                    "UPDATE target_weight_snapshot SET raw_weights=:raw,normalized_weights=:normalized WHERE id=:id"
                ),
                {
                    "raw": json.dumps(payload.get("raw_weights"), ensure_ascii=False),
                    "normalized": json.dumps(payload.get("normalized_weights"), ensure_ascii=False),
                    "id": row["id"],
                },
            )


def restore_legacy_triggers(connection) -> None:
    """Reinstall only historical raw-document invalidation after restoring its schema."""
    from importlib import import_module

    for name in ("0034_analysis_rust_facts", "0035_analysis_structured_results"):
        import_module("axile.server.alembic.versions." + name).install_triggers(connection)
