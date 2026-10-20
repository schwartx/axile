"""Normalize analysis inputs and results for bounded read-only Rust processing.

Legacy JSON is retained until a complete structured representation is verified.
This revision freezes its own DDL and never loads an entire ranges document.
"""

import json
import math
from collections.abc import Iterator

import sqlalchemy as sa
from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None

_BATCH_SIZE = 100
_RANGE_KEYS = ("30", "90", "all")
_DAILY_FIELDS = (
    "value",
    "cost",
    "lossBp",
    "coverage",
    "covered",
    "count",
    "amountComplete",
    "feeCovered",
    "coveredValue",
    "estimated",
)
_SNAPSHOT_COLUMNS = (
    sa.Column("result_schema_version", sa.Integer, nullable=False, server_default="0"),
    sa.Column("backtest_weight_type", sa.Text),
    sa.Column("backtest_fee_rate", sa.Float),
)
_RESULT_NAMES = (
    "performance_result_daily_fee",
    "performance_result_daily_cost",
    "performance_result_event",
    "performance_result_calendar_range",
    "performance_result_binding",
    "performance_result_fallback",
    "performance_result_skip",
    "performance_result_point",
    "performance_result_range",
)
_SOURCE_NAMES = (
    "analysis_source_transaction_fee",
    "analysis_source_transaction",
    "analysis_source_attempt",
    "analysis_source_position",
    "analysis_source_execution_symbol",
    "analysis_source_trade",
    "analysis_source_execution",
)
_TRIGGERS = (
    "analysis_structured_snapshot_delete",
    "analysis_structured_record_update",
    "analysis_structured_record_delete",
    "analysis_structured_source_delete",
    "analysis_structured_account_delete",
    "analysis_account_channel",
)


def _schema() -> sa.MetaData:
    """Freeze revision 0035 definitions independently of runtime model changes."""
    metadata = sa.MetaData()
    sa.Table("account", metadata, sa.Column("id", sa.Integer, primary_key=True))
    sa.Table("executerecord", metadata, sa.Column("id", sa.Integer, primary_key=True))
    sa.Table("performance_snapshot", metadata, sa.Column("id", sa.Text, primary_key=True))

    def _result_table(name: str, keys: list[sa.Column], values: list[sa.Column]) -> sa.Table:
        return sa.Table(
            name,
            metadata,
            sa.Column(
                "snapshot_id", sa.Text, sa.ForeignKey("performance_snapshot.id", ondelete="CASCADE"), primary_key=True
            ),
            sa.Column("range_key", sa.Text, primary_key=True),
            *keys,
            *values,
        )

    result_range = _result_table(
        "performance_result_range",
        [],
        [
            sa.Column("backtest_included", sa.Boolean, nullable=False),
            sa.Column("engine_version", sa.Text, nullable=False),
            sa.Column("baseline", sa.Text),
            sa.Column("end", sa.Text),
        ]
        + [
            sa.Column(name, sa.Integer, nullable=False)
            for name in (
                "record_count",
                "observation_count",
                "used_record_count",
                "target_used_record_count",
                "invalid_asset_count",
            )
        ]
        + [
            sa.Column("sizing_fallback_count", sa.Integer),
            sa.Column("calendar_status", sa.Text, nullable=False),
            sa.Column("calendar_id", sa.Text),
            sa.Column("calendar_label", sa.Text),
            sa.Column("event_count", sa.Integer, nullable=False),
        ],
    )
    result_point = _result_table(
        "performance_result_point",
        [sa.Column("idx", sa.Integer, primary_key=True)],
        [
            sa.Column("date", sa.Text, nullable=False),
            sa.Column("observed_at", sa.Text, nullable=False),
            sa.Column("record_id", sa.Integer),
            sa.Column("execution_id", sa.Text),
        ]
        + [
            sa.Column(name, sa.Float)
            for name in (
                "account_return",
                "account_equity",
                "portfolio_return",
                "account_daily_return",
                "portfolio_daily_return",
                "target_portfolio_return",
                "target_portfolio_daily_return",
                "difference",
            )
        ]
        + [sa.Column("sizing_fallback", sa.Boolean, nullable=False)],
    )
    result_skip = _result_table(
        "performance_result_skip",
        [sa.Column("kind", sa.Text, primary_key=True)],
        [sa.Column(name, sa.Integer, nullable=False) for name in ("count", "missing_target", "missing_ticks")]
        + [sa.Column("first_time", sa.Text), sa.Column("last_time", sa.Text)],
    )
    result_fallback = _result_table(
        "performance_result_fallback",
        [sa.Column("idx", sa.Integer, primary_key=True)],
        [
            sa.Column("start", sa.Text, nullable=False),
            sa.Column("end", sa.Text, nullable=False),
            sa.Column("count", sa.Integer, nullable=False),
        ],
    )
    result_binding = _result_table(
        "performance_result_binding",
        [sa.Column("idx", sa.Integer, primary_key=True)],
        [sa.Column("time", sa.Text, nullable=False), sa.Column("portfolio_id", sa.Integer)],
    )
    result_calendar_range = _result_table(
        "performance_result_calendar_range",
        [sa.Column("kind", sa.Text, primary_key=True), sa.Column("idx", sa.Integer, primary_key=True)],
        [sa.Column("start", sa.Text, nullable=False), sa.Column("end", sa.Text, nullable=False)],
    )
    result_event = _result_table(
        "performance_result_event",
        [sa.Column("idx", sa.Integer, primary_key=True)],
        [
            sa.Column("time", sa.Text, nullable=False),
            sa.Column("tag", sa.Text, nullable=False),
            sa.Column("text", sa.Text, nullable=False),
            sa.Column("execution_id", sa.Text),
        ],
    )
    result_daily_cost = _result_table(
        "performance_result_daily_cost",
        [sa.Column("day", sa.Text, primary_key=True)],
        [sa.Column(name, sa.Float) for name in ("value", "cost", "loss_bp", "coverage", "covered_value")]
        + [sa.Column(name, sa.Integer) for name in ("covered", "count", "fee_covered", "estimated")]
        + [sa.Column("amount_complete", sa.Boolean, nullable=False), sa.Column("present", sa.Integer, nullable=False)],
    )
    result_daily_fee = _result_table(
        "performance_result_daily_fee",
        [sa.Column("day", sa.Text, primary_key=True), sa.Column("currency", sa.Text, primary_key=True)],
        [sa.Column("value", sa.Float, nullable=False)],
    )

    metadata.info["result_tables"] = (
        result_daily_fee,
        result_daily_cost,
        result_event,
        result_calendar_range,
        result_binding,
        result_fallback,
        result_skip,
        result_point,
        result_range,
    )

    # 不可投影的记录也保留版本和错误，避免每次分析都重新解析历史载荷。
    source_execution = sa.Table(
        "analysis_source_execution",
        metadata,
        sa.Column("record_id", sa.Integer, sa.ForeignKey("executerecord.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("account_id", sa.Integer, sa.ForeignKey("account.id", ondelete="CASCADE"), nullable=False),
        sa.Column("projection_version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("projection_error", sa.Text),
        sa.Column("event_tag", sa.Text),
        sa.Column("event_text", sa.Text),
        sa.Column("time", sa.Float),
        sa.Column("success", sa.Boolean, nullable=False, server_default="0"),
        sa.Column("noop", sa.Boolean, nullable=False, server_default="0"),
        sa.Column("key", sa.Text),
        sa.Column("execution_id", sa.Text),
        sa.Column("created_at", sa.Text),
        sa.Column("is_success", sa.Integer),
        sa.Column("duration_sec", sa.Float),
        sa.Column("trade_count", sa.Integer),
        sa.Column("symbol_count", sa.Integer),
        sa.Column("position_count", sa.Integer),
        sa.Column("positions_present", sa.Boolean, nullable=False, server_default="0"),
        sa.Column("reason", sa.Text),
        sa.Column("raw_present", sa.Integer, nullable=False, server_default="0"),
        sa.Column("has_symbol_results", sa.Boolean, nullable=False, server_default="0"),
        *[
            sa.Column("raw_" + field, sa.Text)
            for field in (
                "status",
                "task_status",
                "error",
                "outcome",
                "outcome_reason",
                "reason_code",
                "msg",
                "execution_kind",
            )
        ],
        sa.Index("ix_analysis_source_execution_account_time", "account_id", "time", "record_id"),
    )

    def _source_child(name: str, keys: list[sa.Column], values: list[sa.Column]) -> sa.Table:
        """通过执行事实主键约束子行的生命周期。"""
        return sa.Table(
            name,
            metadata,
            sa.Column(
                "record_id",
                sa.Integer,
                sa.ForeignKey("analysis_source_execution.record_id", ondelete="CASCADE"),
                primary_key=True,
            ),
            *keys,
            *values,
        )

    source_trade = _source_child(
        "analysis_source_trade",
        [sa.Column("trade_index", sa.Integer, primary_key=True)],
        [
            sa.Column("symbol", sa.Text, nullable=False),
            sa.Column("time", sa.Float, nullable=False),
            sa.Column("day", sa.Text, nullable=False),
            sa.Column("value", sa.Float),
            sa.Column("cost", sa.Float),
            sa.Column("loss_bp", sa.Float),
            sa.Column("fee", sa.Float),
            sa.Column("fee_currency", sa.Text),
            sa.Column("estimated", sa.Boolean, nullable=False),
            sa.Column("side", sa.Text, nullable=False, server_default="none"),
            sa.Column("quantity", sa.Float),
            sa.Column("price", sa.Float),
            sa.Column("reference", sa.Float),
            sa.Column("reference_source", sa.Text),
        ],
    )
    source_execution_symbol = _source_child(
        "analysis_source_execution_symbol",
        [sa.Column("symbol", sa.Text, primary_key=True)],
        [sa.Column("present", sa.Integer, nullable=False)]
        + [sa.Column(field, sa.Text) for field in ("status", "error", "outcome", "outcome_reason", "reason_code")],
    )
    source_position = _source_child(
        "analysis_source_position",
        [sa.Column("idx", sa.Integer, primary_key=True)],
        [sa.Column("symbol", sa.Text), sa.Column("quantity", sa.Float), sa.Column("direction", sa.Text)],
    )
    source_attempt = _source_child(
        "analysis_source_attempt",
        [sa.Column("idx", sa.Integer, primary_key=True)],
        [
            sa.Column("symbol", sa.Text),
            sa.Column("planned", sa.Float),
            sa.Column("filled", sa.Float),
            sa.Column("reason", sa.Text),
        ],
    )
    source_transaction = _source_child(
        "analysis_source_transaction",
        [sa.Column("idx", sa.Integer, primary_key=True)],
        [sa.Column("symbol", sa.Text), sa.Column("side", sa.Text)]
        + [sa.Column(field, sa.Float) for field in ("quantity", "price", "reference")]
        + [sa.Column("reference_source", sa.Text)]
        + [sa.Column(field, sa.Float) for field in ("time", "end_time")]
        + [sa.Column("time_estimated", sa.Boolean, nullable=False)]
        + [sa.Column(field, sa.Float) for field in ("before", "target", "value", "cost", "loss_bp", "coverage")]
        + [sa.Column("covered", sa.Integer), sa.Column("count", sa.Integer)]
        + [sa.Column("amount_complete", sa.Boolean, nullable=False), sa.Column("fee_covered", sa.Integer)],
    )
    source_transaction_fee = _source_child(
        "analysis_source_transaction_fee",
        [sa.Column("transaction_idx", sa.Integer, primary_key=True), sa.Column("currency", sa.Text, primary_key=True)],
        [sa.Column("value", sa.Float, nullable=False)],
    )

    # 显式清理不依赖连接是否启用外键，先删除子行再删除执行事实。
    metadata.info["source_tables"] = (
        source_transaction_fee,
        source_transaction,
        source_attempt,
        source_position,
        source_execution_symbol,
        source_trade,
        source_execution,
    )
    sa.Table(
        "analysis_calendar_day",
        metadata,
        sa.Column("channel", sa.Text, primary_key=True),
        sa.Column("day", sa.Text, primary_key=True),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("calendar_id", sa.Text),
        sa.Column("label", sa.Text),
    )
    return metadata


def install_triggers(connection) -> None:
    """Clear invalidated facts in the source transaction, including FK-off connections."""
    connection.exec_driver_sql("""CREATE TRIGGER IF NOT EXISTS analysis_account_channel
        AFTER UPDATE OF trade_channel ON account WHEN OLD.trade_channel IS NOT NEW.trade_channel BEGIN
        INSERT INTO account_analysis (account_id, source_version) VALUES (NEW.id, 1)
        ON CONFLICT(account_id) DO UPDATE SET source_version = source_version + 1,
        requested = CASE WHEN failures > 0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
        THEN 1 ELSE requested END,
        failures = 0, retry_at = 0, error = NULL;
        END""")
    result_deletes = " ".join(f"DELETE FROM {name} WHERE snapshot_id = OLD.id;" for name in _RESULT_NAMES)
    connection.exec_driver_sql(
        "CREATE TRIGGER IF NOT EXISTS analysis_structured_snapshot_delete AFTER DELETE ON performance_snapshot "
        f"BEGIN {result_deletes} END"
    )
    source_deletes = " ".join(f"DELETE FROM {name} WHERE record_id = OLD.record_id;" for name in _SOURCE_NAMES[:-1])
    connection.exec_driver_sql(
        "CREATE TRIGGER IF NOT EXISTS analysis_structured_source_delete AFTER DELETE ON analysis_source_execution "
        f"BEGIN {source_deletes} END"
    )
    record_deletes = " ".join(f"DELETE FROM {name} WHERE record_id = OLD.id;" for name in _SOURCE_NAMES)
    # Repeat replay cleanup explicitly: is_success was not covered by revision0034.
    record_deletes += " DELETE FROM replay_observation_symbol WHERE record_id = OLD.id;"
    record_deletes += " DELETE FROM replay_observation WHERE record_id = OLD.id;"
    connection.exec_driver_sql(
        "CREATE TRIGGER IF NOT EXISTS analysis_structured_record_update "
        "AFTER UPDATE OF raw_input, raw_result, created_at, account_id, execution_id, is_success ON executerecord "
        f"BEGIN {record_deletes} END"
    )
    connection.exec_driver_sql(
        "CREATE TRIGGER IF NOT EXISTS analysis_structured_record_delete AFTER DELETE ON executerecord "
        f"BEGIN {record_deletes} END"
    )
    account_deletes = " ".join(
        f"DELETE FROM {name} WHERE record_id IN "
        "(SELECT record_id FROM analysis_source_execution WHERE account_id = OLD.id);"
        for name in _SOURCE_NAMES[:-1]
    )
    account_deletes += " DELETE FROM analysis_source_execution WHERE account_id = OLD.id;"
    connection.exec_driver_sql(
        "CREATE TRIGGER IF NOT EXISTS analysis_structured_account_delete AFTER DELETE ON account "
        f"BEGIN {account_deletes} END"
    )


def _validate(table: sa.Table, row: dict, kinds: dict) -> dict:
    """Reject lossy SQLite coercions rather than advertise incompatible snapshots."""
    result = dict(row)
    for column in table.c:
        name = column.name
        value = row.get(name)
        kind = kinds.get(name)
        if value is None:
            if not column.nullable:
                raise ValueError(f"Missing required structured value: {table.name}.{name}")
            continue
        if isinstance(column.type, sa.Boolean):
            if kind not in (None, "true", "false") or value not in (0, 1):
                raise ValueError(f"Invalid boolean: {table.name}.{name}")
            result[name] = bool(value)
        elif isinstance(column.type, sa.Integer):
            if kind not in (None, "integer") or not isinstance(value, int):
                raise ValueError(f"Invalid integer: {table.name}.{name}")
        elif isinstance(column.type, sa.Float):
            if kind not in (None, "integer", "real") or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"Invalid number: {table.name}.{name}")
        elif isinstance(column.type, sa.Text) and (kind not in (None, "text") or not isinstance(value, str)):
            raise ValueError(f"Invalid text: {table.name}.{name}")
    return result


def _object(connection, snapshot_id: str, path: str, fields: dict[str, str]) -> tuple[dict, dict]:
    """Read fixed scalar fields without returning their containing JSON object."""
    selections = ["json_type(ranges, :path) AS object_type"]
    params = {"snapshot_id": snapshot_id, "path": path}
    for index, (name, key) in enumerate(fields.items()):
        params[f"path{index}"] = path + "." + key
        selections.extend(
            (f'json_extract(ranges, :path{index}) AS "{name}"', f'json_type(ranges, :path{index}) AS "type_{name}"')
        )
    row = (
        connection.execute(
            sa.text("SELECT " + ",".join(selections) + " FROM performance_snapshot WHERE id=:snapshot_id"), params
        )
        .mappings()
        .one()
    )
    if row["object_type"] != "object":
        raise ValueError(f"Expected an object at {path}")
    return {name: row[name] for name in fields}, {name: row["type_" + name] for name in fields}


def _objects(connection, snapshot_id: str, path: str, fields: dict[str, str]) -> Iterator[tuple[object, dict, dict]]:
    """Stream one leaf object's scalar columns at a time with bounded DBAPI batches."""
    selections = ["item.key AS item_key", "item.type AS object_type"]
    for name, key in fields.items():
        selections.extend(
            (
                f"json_extract(item.value, '$.{key}') AS \"{name}\"",
                f"json_type(item.value, '$.{key}') AS \"type_{name}\"",
            )
        )
    rows = connection.execute(
        sa.text(
            "SELECT "
            + ",".join(selections)
            + " FROM performance_snapshot AS snapshot, json_each(snapshot.ranges, :path) AS item"
            + " WHERE snapshot.id=:snapshot_id ORDER BY item.key"
        ),
        {"snapshot_id": snapshot_id, "path": path},
    ).mappings()
    for row in rows:
        if row["object_type"] != "object":
            raise ValueError(f"Expected leaf object at {path}")
        yield row["item_key"], {name: row[name] for name in fields}, {name: row["type_" + name] for name in fields}


def _copy_array(connection, metadata, snapshot_id, range_key, table_name, path, renames=None, constants=None):
    table = metadata.tables[table_name]
    constants = constants or {}
    renames = renames or {}
    fields = {
        column.name: renames.get(column.name, column.name)
        for column in table.c
        if column.name not in {"snapshot_id", "range_key", "idx", *constants}
    }
    batch = []
    for index, values, kinds in _objects(connection, snapshot_id, path, fields):
        row = {"snapshot_id": snapshot_id, "range_key": range_key, "idx": index, **constants, **values}
        batch.append(_validate(table, row, kinds))
        if len(batch) == _BATCH_SIZE:
            connection.execute(table.insert(), batch)
            batch = []
    if batch:
        connection.execute(table.insert(), batch)


def _copy_skips(connection, metadata, snapshot_id, range_key, base):
    table = metadata.tables["performance_result_skip"]
    fields = {column.name: column.name for column in table.c if column.name not in {"snapshot_id", "range_key", "kind"}}
    for kind in ("skips", "target_skips"):
        path = base + ".performance." + kind
        present = connection.execute(
            sa.text("SELECT json_type(ranges,:path) FROM performance_snapshot WHERE id=:id"),
            {"id": snapshot_id, "path": path},
        ).scalar_one()
        if present in (None, "null"):
            continue
        values, kinds = _object(connection, snapshot_id, path, fields)
        row = {
            "snapshot_id": snapshot_id,
            "range_key": range_key,
            "kind": {"skips": "portfolio", "target_skips": "target"}[kind],
            **values,
        }
        connection.execute(table.insert(), _validate(table, row, kinds))


def _copy_daily_costs(connection, metadata, snapshot_id, range_key, base):
    table = metadata.tables["performance_result_daily_cost"]
    mapping = {
        "value": "value",
        "cost": "cost",
        "loss_bp": "lossBp",
        "coverage": "coverage",
        "covered": "covered",
        "count": "count",
        "amount_complete": "amountComplete",
        "fee_covered": "feeCovered",
        "covered_value": "coveredValue",
        "estimated": "estimated",
    }
    path = base + ".daily_costs"
    for day, values, kinds in _objects(connection, snapshot_id, path, mapping):
        present = sum(1 << _DAILY_FIELDS.index(mapping[name]) for name, kind in kinds.items() if kind is not None)
        row = {"snapshot_id": snapshot_id, "range_key": range_key, "day": day, "present": present, **values}
        connection.execute(table.insert(), _validate(table, row, kinds))
        # Day keys are ISO dates produced by performance_costs; JSON quoting also
        # keeps unsupported imported keys from changing the extraction path.
        fee_path = path + "." + json.dumps(day, ensure_ascii=False) + ".fees"
        fees = connection.execute(
            sa.text(
                "SELECT item.key AS currency,item.value,item.type FROM performance_snapshot AS snapshot, "
                "json_each(snapshot.ranges,:path) AS item WHERE snapshot.id=:id"
            ),
            {"id": snapshot_id, "path": fee_path},
        ).mappings()
        fee_table = metadata.tables["performance_result_daily_fee"]
        for fee in fees:
            row = {
                "snapshot_id": snapshot_id,
                "range_key": range_key,
                "day": day,
                "currency": fee["currency"],
                "value": fee["value"],
            }
            connection.execute(fee_table.insert(), _validate(fee_table, row, {"value": fee["type"]}))


def _check_shapes(connection, snapshot_id, range_key):
    """Only complete canonical containers may become a structured snapshot."""
    base = "$." + range_key
    expected = {
        "": "object",
        ".performance": "object",
        ".performance.settings": "object",
        ".performance.calendar": "object",
        ".performance.points": "array",
        ".performance.bindings": "array",
        ".performance.sizing_fallback_ranges": "array",
        ".performance.calendar.closed_ranges": "array",
        ".performance.calendar.unavailable_ranges": "array",
        ".daily_costs": "object",
        ".events": "array",
    }
    expressions = [f"json_type(ranges,:path{i}) AS shape{i}" for i in range(len(expected))]
    params = {"id": snapshot_id, **{f"path{i}": base + path for i, path in enumerate(expected)}}
    expressions.extend(
        (
            "json_extract(ranges,:range_path) AS range_key",
            "json_extract(ranges,:weight_path) AS range_weight",
            "json_extract(ranges,:fee_path) AS range_fee",
            "json_type(ranges,:fee_path) AS range_fee_type",
            "json_extract(ranges,:status_path) AS calendar_status",
            "json_extract(settings,'$.backtest_weight_type') AS weight",
            "json_extract(settings,'$.backtest_fee_rate') AS fee",
        )
    )
    params.update(
        range_path=base + ".performance.range",
        weight_path=base + ".performance.settings.backtest_weight_type",
        fee_path=base + ".performance.settings.backtest_fee_rate",
        status_path=base + ".performance.calendar.status",
    )
    row = (
        connection.execute(
            sa.text("SELECT " + ",".join(expressions) + " FROM performance_snapshot WHERE id=:id"), params
        )
        .mappings()
        .one()
    )
    if any(row[f"shape{i}"] != shape for i, shape in enumerate(expected.values())):
        raise ValueError("Unsupported legacy result container shape")
    if (
        row["range_key"] != range_key
        or row["range_weight"] != row["weight"]
        or row["range_fee"] != row["fee"]
        or row["range_fee_type"] not in ("integer", "real")
    ):
        raise ValueError("Legacy range metadata does not match its immutable snapshot")
    if row["calendar_status"] not in ("available", "partial", "unavailable", "not_required"):
        raise ValueError("Unsupported legacy calendar status")


def _backfill_range(connection, metadata, snapshot_id, range_key):
    _check_shapes(connection, snapshot_id, range_key)
    base = "$." + range_key
    table = metadata.tables["performance_result_range"]
    fields = {
        column.name: "performance." + column.name
        for column in table.c
        if column.name
        not in {"snapshot_id", "range_key", "calendar_status", "calendar_id", "calendar_label", "event_count"}
    }
    fields.update(
        calendar_status="performance.calendar.status",
        calendar_id="performance.calendar.calendar_id",
        calendar_label="performance.calendar.label",
        event_count="event_count",
    )
    values, kinds = _object(connection, snapshot_id, base, fields)
    row = {"snapshot_id": snapshot_id, "range_key": range_key, **values}
    connection.execute(table.insert(), _validate(table, row, kinds))
    _copy_skips(connection, metadata, snapshot_id, range_key, base)
    for name, suffix, renames in (
        ("point", ".performance.points", {}),
        ("fallback", ".performance.sizing_fallback_ranges", {}),
        ("binding", ".performance.bindings", {}),
        ("event", ".events", {"execution_id": "executionId"}),
    ):
        _copy_array(connection, metadata, snapshot_id, range_key, "performance_result_" + name, base + suffix, renames)
    for kind in ("closed_ranges", "unavailable_ranges"):
        _copy_array(
            connection,
            metadata,
            snapshot_id,
            range_key,
            "performance_result_calendar_range",
            base + ".performance.calendar." + kind,
            constants={"kind": {"closed_ranges": "closed", "unavailable_ranges": "unavailable"}[kind]},
        )
    _copy_daily_costs(connection, metadata, snapshot_id, range_key, base)


def _backfill(connection, metadata) -> None:
    """Promote complete compatible snapshots; savepoints retain unsupported legacy data."""
    previous = ""
    while True:
        ids = list(
            connection.execute(
                sa.text("SELECT id FROM performance_snapshot WHERE id > :previous ORDER BY id LIMIT :batch"),
                {"previous": previous, "batch": _BATCH_SIZE},
            ).scalars()
        )
        if not ids:
            return
        previous = ids[-1]
        for snapshot_id in ids:
            try:
                with connection.begin_nested():
                    row = (
                        connection.execute(
                            sa.text(
                                "SELECT cost_schema_version,json_valid(ranges) AS valid_ranges,json_valid(settings) AS valid_settings,"
                                "CASE WHEN json_valid(settings) THEN json_extract(settings,'$.backtest_weight_type') END AS weight,"
                                "CASE WHEN json_valid(settings) THEN json_extract(settings,'$.backtest_fee_rate') END AS fee,"
                                "CASE WHEN json_valid(settings) THEN json_type(settings,'$.backtest_fee_rate') END AS fee_type "
                                "FROM performance_snapshot WHERE id=:id"
                            ),
                            {"id": snapshot_id},
                        )
                        .mappings()
                        .one()
                    )
                    if not row["valid_ranges"] or not row["valid_settings"] or row["cost_schema_version"] != 1:
                        continue
                    if (
                        row["fee_type"] not in ("integer", "real")
                        or row["weight"] not in ("cs", "ts")
                        or not isinstance(row["fee"], (float, int))
                        or not 0 <= row["fee"] < 1
                    ):
                        continue
                    for range_key in _RANGE_KEYS:
                        _backfill_range(connection, metadata, snapshot_id, range_key)
                    connection.execute(
                        sa.text(
                            "UPDATE performance_snapshot SET result_schema_version=2,backtest_weight_type=:weight,"
                            "backtest_fee_rate=:fee WHERE id=:id"
                        ),
                        {"id": snapshot_id, "weight": row["weight"], "fee": row["fee"]},
                    )
            except (ValueError, TypeError, KeyError, sa.exc.IntegrityError, sa.exc.OperationalError):
                # SQLite json_each reports malformed imported leaf JSON as OperationalError;
                # only this snapshot is rejected and its raw historical data is untouched.
                continue


def upgrade() -> None:
    """Add typed inputs/results without changing the Python-owned write boundary."""
    metadata = _schema()
    for column in _SNAPSHOT_COLUMNS:
        op.add_column(
            "performance_snapshot",
            sa.Column(column.name, column.type, nullable=column.nullable, server_default=column.server_default),
        )
    connection = op.get_bind()
    for name in (*reversed(_SOURCE_NAMES), *reversed(_RESULT_NAMES), "analysis_calendar_day"):
        metadata.tables[name].create(connection)
    _backfill(connection, metadata)
    install_triggers(connection)


def _json_fields(alias, fields, *, booleans=(), numbers=(), renames=None):
    """Build a frozen SQLite JSON object without allocating a Python object tree."""
    renames = renames or {}
    items = []
    for field in fields:
        value = f'{alias}."{field}"'
        if field in booleans:
            value = f"json(CASE WHEN {value} THEN 'true' ELSE 'false' END)"
        elif field in numbers:
            value = f"json(_analysis_json_number({value}))"
        items.extend(("'" + renames.get(field, field) + "'", value))
    return "json_object(" + ",".join(items) + ")"


def _array_expr(table, fields, *, booleans=(), numbers=(), renames=None, extra="", order="idx"):
    value = _json_fields("child", fields, booleans=booleans, numbers=numbers, renames=renames)
    return (
        "json((SELECT json_group_array(json(value)) FROM (SELECT "
        + value
        + " AS value FROM "
        + table
        + " AS child WHERE child.snapshot_id=r.snapshot_id AND child.range_key=r.range_key "
        + extra
        + " ORDER BY child."
        + order
        + ")))"
    )


def _range_json() -> str:
    """Reconstitute legacy ranges inside SQLite, including growing arrays."""
    point_fields = (
        "date",
        "observed_at",
        "record_id",
        "execution_id",
        "account_return",
        "account_equity",
        "portfolio_return",
        "account_daily_return",
        "portfolio_daily_return",
        "target_portfolio_return",
        "target_portfolio_daily_return",
        "difference",
        "sizing_fallback",
    )
    points = _array_expr(
        "performance_result_point", point_fields, booleans=("sizing_fallback",), numbers=point_fields[4:-1]
    )
    fallbacks = _array_expr("performance_result_fallback", ("start", "end", "count"))
    bindings = _array_expr("performance_result_binding", ("time", "portfolio_id"))
    events = _array_expr(
        "performance_result_event", ("time", "tag", "text", "execution_id"), renames={"execution_id": "executionId"}
    )
    skip_fields = ("count", "missing_target", "missing_ticks", "first_time", "last_time")
    skips = {
        kind: "json((SELECT "
        + _json_fields("child", skip_fields)
        + " FROM performance_result_skip AS child WHERE child.snapshot_id=r.snapshot_id "
        + f"AND child.range_key=r.range_key AND child.kind='{ {'skips': 'portfolio', 'target_skips': 'target'}[kind] }'))"
        for kind in ("skips", "target_skips")
    }
    calendars = {
        kind: _array_expr(
            "performance_result_calendar_range",
            ("start", "end"),
            extra=f"AND child.kind='{ {'closed_ranges': 'closed', 'unavailable_ranges': 'unavailable'}[kind] }'",
        )
        for kind in ("closed_ranges", "unavailable_ranges")
    }
    settings = "json_object('backtest_weight_type',s.backtest_weight_type,'backtest_fee_rate',json(_analysis_json_number(s.backtest_fee_rate)))"
    calendar = (
        "json_object('status',r.calendar_status,'calendar_id',r.calendar_id,'label',r.calendar_label,"
        + "'closed_ranges',"
        + calendars["closed_ranges"]
        + ",'unavailable_ranges',"
        + calendars["unavailable_ranges"]
        + ")"
    )
    fields = (
        "backtest_included",
        "engine_version",
        "baseline",
        "end",
        "record_count",
        "observation_count",
        "used_record_count",
        "target_used_record_count",
        "invalid_asset_count",
        "sizing_fallback_count",
    )
    header = _json_fields("r", fields, booleans=("backtest_included",))
    # json_patch removes null members, so json_set preserves explicit null fields.
    performance = (
        "json_set("
        + header
        + ",'$.settings',"
        + settings
        + ",'$.range',r.range_key,'$.points',"
        + points
        + ",'$.skips',"
        + skips["skips"]
        + ",'$.target_skips',"
        + skips["target_skips"]
        + ",'$.sizing_fallback_ranges',"
        + fallbacks
        + ",'$.bindings',"
        + bindings
        + ",'$.calendar',"
        + calendar
        + ")"
    )
    executions = (
        "json((SELECT json_group_array(json(payload)) FROM (SELECT e.payload FROM performance_cost_execution AS e "
        "WHERE e.snapshot_id=r.snapshot_id AND e.time >= COALESCE((SELECT baseline FROM performance_cost_range "
        "WHERE snapshot_id=r.snapshot_id AND range_key=r.range_key),0) AND e.time <= COALESCE((SELECT end "
        "FROM performance_cost_range WHERE snapshot_id=r.snapshot_id AND range_key=r.range_key),-1) "
        "ORDER BY e.time,e.record_id)))"
    )
    performance = (
        "CASE WHEN r.range_key='all' THEN json_set("
        + performance
        + ",'$.executions',"
        + executions
        + ") ELSE "
        + performance
        + " END"
    )
    daily_mapping = {
        "value": "value",
        "cost": "cost",
        "lossBp": "loss_bp",
        "coverage": "coverage",
        "covered": "covered",
        "count": "count",
        "amountComplete": "amount_complete",
        "feeCovered": "fee_covered",
        "coveredValue": "covered_value",
        "estimated": "estimated",
    }
    daily_members = []
    for index, public in enumerate(_DAILY_FIELDS):
        value = "_analysis_json_number(d." + daily_mapping[public] + ")"
        if public == "amountComplete":
            value = "CASE WHEN d.amount_complete THEN 'true' ELSE 'false' END"
        daily_members.append(f"SELECT '{public}' AS key,{value} AS value WHERE d.present & {1 << index}")
    fees = (
        "(SELECT json_group_object(currency,json(_analysis_json_number(value))) FROM (SELECT currency,value FROM performance_result_daily_fee "
        "AS fee WHERE fee.snapshot_id=d.snapshot_id AND fee.range_key=d.range_key AND fee.day=d.day ORDER BY currency))"
    )
    daily_members.append("SELECT 'fees' AS key," + fees + " AS value")
    daily = "json((SELECT json_group_object(key,json(value)) FROM (" + " UNION ALL ".join(daily_members) + ")))"
    daily_costs = (
        "json((SELECT json_group_object(day,json(value)) FROM (SELECT d.day,"
        + daily
        + " AS value FROM performance_result_daily_cost AS d WHERE d.snapshot_id=r.snapshot_id "
        "AND d.range_key=r.range_key ORDER BY d.day)))"
    )
    return (
        "json_object('performance',json("
        + performance
        + "),'daily_costs',"
        + daily_costs
        + ",'events',"
        + events
        + ",'event_count',r.event_count)"
    )


def _restore_results(connection) -> None:
    """Rebuild sentinel snapshots without materializing history in Python memory.

    SQLite's native JSON number formatter keeps only 15 significant digits.
    Encoding one scalar through Python preserves every stored float64 bit while
    SQLite still assembles growing arrays and objects entirely in the database.
    """
    expression = _range_json()
    dbapi = connection.connection.dbapi_connection
    dbapi.create_function(
        "_analysis_json_number", 1, lambda value: json.dumps(value, allow_nan=False), deterministic=True
    )
    try:
        connection.exec_driver_sql(
            "UPDATE performance_snapshot AS s SET ranges=(SELECT json_group_object(range_key,json(value)) "
            "FROM (SELECT r.range_key," + expression + " AS value FROM performance_result_range AS r "
            "WHERE r.snapshot_id=s.id ORDER BY r.range_key)), "
            "settings=json_object('backtest_weight_type',s.backtest_weight_type,"
            "'backtest_fee_rate',json(_analysis_json_number(s.backtest_fee_rate))) "
            "WHERE s.result_schema_version=2 AND (s.ranges='{}' OR s.settings='{}')"
        )
    finally:
        dbapi.create_function("_analysis_json_number", 1, None)


def downgrade() -> None:
    """Restore legacy payloads before removing the typed result/source representation."""
    from axile.server.alembic.analysis0035_cost_restore import restore_cost_payloads

    connection = op.get_bind()
    restore_cost_payloads(connection)
    _restore_results(connection)
    for name in _TRIGGERS:
        connection.exec_driver_sql(f"DROP TRIGGER IF EXISTS {name}")
    for name in (*_RESULT_NAMES, *_SOURCE_NAMES, "analysis_calendar_day"):
        op.drop_table(name)
    for column in reversed(_SNAPSHOT_COLUMNS):
        op.drop_column("performance_snapshot", column.name)
