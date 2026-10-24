use super::*;
use crate::legacy_execution::compact_execution;
use schema::{COST_CHILDREN, RAW_FIELDS, RESULT_NAMES, SOURCE_NAMES, SYMBOL_FIELDS};

fn object(value: &Value) -> Object {
    value.as_object().cloned().unwrap_or_default()
}
fn present(row: &Object, names: &[&str]) -> Value {
    Value::from(
        names
            .iter()
            .enumerate()
            .filter(|(_, name)| row.contains_key(**name))
            .map(|(index, _)| 1_i64 << index)
            .sum::<i64>(),
    )
}
fn renamed(source: &Object, names: &[(&str, &str)]) -> Object {
    names
        .iter()
        .map(|(column, key)| ((*column).to_owned(), value(source, key).clone()))
        .collect()
}

pub(super) fn upgrade34(connection: &Connection) -> Result<()> {
    execute(connection,"ALTER TABLE performance_snapshot ADD COLUMN cost_schema_version INTEGER DEFAULT '0' NOT NULL")?;
    schema::add_columns(
        connection,
        "performance_cost_execution",
        &schema::execution_columns(),
    )?;
    schema::add_columns(
        connection,
        "performance_cost_trade",
        &schema::trade_columns(),
    )?;
    schema::create34(connection)?;
    let mut after = None;
    loop {
        let ids = snapshot_ids(connection, after.as_deref())?;
        if ids.is_empty() {
            break;
        }
        after = ids.last().cloned();
        for id in ids {
            if let Err(failure) = savepoint(connection, || backfill34(connection, &id)) {
                if failure.code != "MIGRATION_VALUE" {
                    return Err(failure);
                }
            }
        }
    }
    guards::install34(connection)
}
fn invalid(message: impl Into<String>) -> AnalysisError {
    AnalysisError::new("MIGRATION_VALUE", message)
}

fn validate(
    connection: &Connection,
    table: &str,
    row: &Object,
    kinds: Option<&Object>,
) -> Result<()> {
    for column in table_info(connection, table)? {
        let name = text(&column, "name")?;
        let item = value(row, name);
        if item.is_null() {
            if integer(&column, "notnull")? != 0 {
                return Err(invalid(format!("Missing legacy scalar in {table}.{name}")));
            }
            continue;
        }
        let kind = kinds
            .and_then(|kinds| kinds.get(name))
            .and_then(Value::as_str);
        let valid = match text(&column, "type")?.to_uppercase().as_str() {
            "TEXT" => item.is_string() && matches!(kind, None | Some("text")),
            "FLOAT" | "REAL" => {
                item.is_number()
                    && (kinds.is_none() || item.as_f64().is_some_and(f64::is_finite))
                    && matches!(kind, None | Some("integer" | "real"))
            }
            "BOOLEAN" => {
                let truth = item.is_boolean() || matches!(item.as_i64(), Some(0 | 1));
                truth && matches!(kind, None | Some("true" | "false"))
            }
            "INTEGER" => {
                if kinds.is_none()
                    && item.is_number()
                    && item.as_i64().is_none()
                    && item
                        .to_string()
                        .bytes()
                        .all(|byte| byte.is_ascii_digit() || byte == b'-')
                {
                    return Err(error("Python int too large to convert to SQLite INTEGER"));
                }
                (item.as_i64().is_some() || (kinds.is_none() && item.is_boolean()))
                    && matches!(kind, None | Some("integer"))
            }
            _ => true,
        };
        if !valid {
            return Err(invalid(format!("Invalid legacy scalar in {table}.{name}")));
        }
    }
    Ok(())
}
fn put_validated(
    connection: &Connection,
    table: &str,
    row: &Object,
    kinds: Option<&Object>,
) -> Result<()> {
    validate(connection, table, row, kinds)?;
    insert(connection, table, row)
}

fn backfill34(connection: &Connection, id: &str) -> Result<()> {
    let mut after = None;
    loop {
        let batch=rows(connection,"SELECT snapshot_id,record_id,time,success,noop,payload FROM performance_cost_execution WHERE snapshot_id=?1 AND (?2 IS NULL OR record_id>?2) ORDER BY record_id LIMIT 100",rusqlite::params![id,after])?;
        if batch.is_empty() {
            break;
        }
        after = Some(integer(batch.last().unwrap(), "record_id")?);
        for mut row in batch {
            let source = json_value(value(&row, "payload")).map_err(|e| invalid(e.error))?;
            let payload = source
                .as_object()
                .ok_or_else(|| invalid("Legacy execution projection must be an object"))?;
            let payload = compact_execution(payload)?;
            let record = object(value(&payload, "record"));
            let raw = object(value(&record, "raw_result"));
            let mut headers = renamed(
                &payload,
                &[
                    ("key", "key"),
                    ("duration_sec", "durationSec"),
                    ("trade_count", "tradeCount"),
                    ("symbol_count", "symbolCount"),
                    ("position_count", "positionCount"),
                    ("reason", "reason"),
                ],
            );
            headers.extend(renamed(
                &record,
                &[
                    ("execution_id", "execution_id"),
                    ("created_at", "created_at"),
                    ("is_success", "is_success"),
                ],
            ));
            headers.insert(
                "positions_present".into(),
                Value::Bool(value(&payload, "positions").is_array()),
            );
            headers.insert("raw_present".into(), present(&raw, RAW_FIELDS));
            headers.insert(
                "has_symbol_results".into(),
                Value::Bool(value(&raw, "symbol_results").is_object()),
            );
            for name in RAW_FIELDS {
                headers.insert(format!("raw_{name}"), value(&raw, name).clone());
            }
            row.extend(headers.clone());
            validate(connection, "performance_cost_execution", &row, None)?;
            update(
                connection,
                "performance_cost_execution",
                &headers,
                "snapshot_id=? AND record_id=?",
                &[Value::from(id), value(&row, "record_id").clone()],
            )?;
            cost_children(connection, id, integer(&row, "record_id")?, &payload)?;
        }
    }
    let mut after = None;
    loop {
        let batch=rows(connection,"SELECT snapshot_id,id,record_id,symbol,time,day,value,cost,loss_bp,fee,fee_currency,estimated,payload FROM performance_cost_trade WHERE snapshot_id=?1 AND (?2 IS NULL OR id>?2) ORDER BY id LIMIT 100",rusqlite::params![id,after])?;
        if batch.is_empty() {
            break;
        }
        after = Some(integer(batch.last().unwrap(), "id")?);
        for mut row in batch {
            let source = json_value(value(&row, "payload")).map_err(|e| invalid(e.error))?;
            let payload = source
                .as_object()
                .ok_or_else(|| invalid("Legacy trade projection must be an object"))?;
            let side = payload
                .get("side")
                .cloned()
                .unwrap_or_else(|| Value::from("none"));
            if !matches!(side.as_str(), Some("buy" | "sell" | "none")) {
                return Err(invalid("Cost fact side must be buy, sell or none"));
            }
            let mut headers = renamed(
                payload,
                &[
                    ("quantity", "quantity"),
                    ("price", "price"),
                    ("reference", "reference"),
                    ("reference_source", "referenceSource"),
                ],
            );
            headers.insert("side".into(), side);
            row.extend(headers.clone());
            validate(connection, "performance_cost_trade", &row, None)?;
            update(
                connection,
                "performance_cost_trade",
                &headers,
                "snapshot_id=? AND id=?",
                &[Value::from(id), value(&row, "id").clone()],
            )?;
        }
    }
    let ranges=rows(connection,"SELECT item.key AS range_key,json_extract(item.value,'$.performance.baseline') AS baseline,json_extract(item.value,'$.performance.end') AS range_end FROM performance_snapshot AS snapshot,json_each(snapshot.ranges) AS item WHERE snapshot.id=?",[id])?;
    for range in ranges {
        let mut fact=serde_json::json!({"snapshot_id":id,"range_key":value(&range,"range_key"),"baseline":null,"end":null}).as_object().unwrap().clone();
        for (column, key) in [("baseline", "baseline"), ("end", "range_end")] {
            let bound = value(&range, key);
            if crate::business_facts_ops::encode::is_truthy(bound) {
                let source = bound
                    .as_str()
                    .ok_or_else(|| invalid("Invalid cost range timestamp"))?;
                fact.insert(
                    column.into(),
                    Value::from(millis(source, true).map_err(|e| invalid(e.error))?),
                );
            }
        }
        insert(connection, "performance_cost_range", &fact)?;
    }
    connection
        .execute(
            "UPDATE performance_snapshot SET cost_schema_version=1 WHERE id=?",
            [id],
        )
        .map_err(sql)?;
    Ok(())
}
fn cost_children(
    connection: &Connection,
    id: &str,
    record_id: i64,
    payload: &Object,
) -> Result<()> {
    let base = serde_json::json!({"snapshot_id":id,"record_id":record_id})
        .as_object()
        .unwrap()
        .clone();
    let raw = object(value(&object(value(payload, "record")), "raw_result"));
    if let Some(symbols) = value(&raw, "symbol_results").as_object() {
        for (symbol, item) in symbols {
            let item = object(item);
            let mut row = base.clone();
            row.insert("symbol".into(), Value::from(symbol.as_str()));
            row.insert("present".into(), present(&item, SYMBOL_FIELDS));
            for name in SYMBOL_FIELDS {
                row.insert((*name).into(), value(&item, name).clone());
            }
            put_validated(connection, "performance_cost_execution_symbol", &row, None)?;
        }
    }
    for (field, table, names) in [
        (
            "positions",
            "performance_cost_position",
            &["symbol", "quantity", "direction"][..],
        ),
        (
            "attempts",
            "performance_cost_attempt",
            &["symbol", "planned", "filled", "reason"][..],
        ),
    ] {
        let source = value(payload, field);
        if !crate::business_facts_ops::encode::is_truthy(source) {
            continue;
        }
        let items = source
            .as_array()
            .ok_or_else(|| invalid("Invalid execution projection array"))?;
        for (index, item) in items.iter().enumerate() {
            let item = item
                .as_object()
                .ok_or_else(|| invalid("Invalid execution projection member"))?;
            let mut row = base.clone();
            row.insert("idx".into(), Value::from(index));
            for name in names {
                row.insert((*name).into(), value(item, name).clone());
            }
            put_validated(connection, table, &row, None)?;
        }
    }
    let transactions = value(payload, "transactions");
    if !crate::business_facts_ops::encode::is_truthy(transactions) {
        return Ok(());
    }
    let transactions = transactions
        .as_array()
        .ok_or_else(|| invalid("Invalid execution transactions"))?;
    for (index, item) in transactions.iter().enumerate() {
        let item = item
            .as_object()
            .ok_or_else(|| invalid("Invalid execution transaction"))?;
        let summary = value(item, "summary")
            .as_object()
            .ok_or_else(|| invalid("Missing transaction summary"))?;
        let mut row = base.clone();
        row.insert("idx".into(), Value::from(index));
        row.extend(renamed(item, TRANSACTION_FIELDS));
        row.insert(
            "time_estimated".into(),
            item.get("timeEstimated")
                .cloned()
                .unwrap_or(Value::Bool(false)),
        );
        row.extend(renamed(summary, &DAILY_FIELDS[..8]));
        put_validated(connection, "performance_cost_transaction", &row, None)?;
        let fees = value(summary, "fees")
            .as_object()
            .ok_or_else(|| invalid("Missing transaction fees"))?;
        for (currency, fee) in fees {
            let mut row = base.clone();
            row.insert("transaction_idx".into(), Value::from(index));
            row.insert("currency".into(), Value::from(currency.as_str()));
            row.insert("value".into(), fee.clone());
            put_validated(connection, "performance_cost_transaction_fee", &row, None)?;
        }
    }
    Ok(())
}

pub(super) fn downgrade34(connection: &Connection) -> Result<()> {
    guards::drop34(connection)?;
    for name in ["replay_observation_symbol", "replay_observation"]
        .into_iter()
        .chain(COST_CHILDREN.iter().copied())
    {
        execute(connection, &format!("DROP TABLE {}", quote(name)))?;
    }
    schema::drop_columns(
        connection,
        "performance_cost_trade",
        &schema::trade_columns(),
    )?;
    schema::drop_columns(
        connection,
        "performance_cost_execution",
        &schema::execution_columns(),
    )?;
    execute(
        connection,
        "ALTER TABLE performance_snapshot DROP COLUMN cost_schema_version",
    )
}

pub(super) const TRANSACTION_FIELDS: &[(&str, &str)] = &[
    ("symbol", "symbol"),
    ("side", "side"),
    ("quantity", "quantity"),
    ("price", "price"),
    ("reference", "reference"),
    ("reference_source", "referenceSource"),
    ("time", "time"),
    ("end_time", "endTime"),
    ("time_estimated", "timeEstimated"),
    ("before", "before"),
    ("target", "target"),
];
pub(super) const DAILY_FIELDS: &[(&str, &str)] = &[
    ("value", "value"),
    ("cost", "cost"),
    ("loss_bp", "lossBp"),
    ("coverage", "coverage"),
    ("covered", "covered"),
    ("count", "count"),
    ("amount_complete", "amountComplete"),
    ("fee_covered", "feeCovered"),
    ("covered_value", "coveredValue"),
    ("estimated", "estimated"),
];
fn table_info(connection: &Connection, table: &str) -> Result<Vec<Object>> {
    rows(
        connection,
        &format!("PRAGMA table_info({})", quote(table)),
        [],
    )
}
pub(super) fn table_fields(
    connection: &Connection,
    table: &str,
    skip: &[&str],
) -> Result<Vec<String>> {
    table_info(connection, table)?
        .iter()
        .map(|row| text(row, "name").map(str::to_owned))
        .filter(|item| {
            item.as_ref()
                .map_or(true, |name| !skip.contains(&name.as_str()))
        })
        .collect()
}
fn each_extracted(
    connection: &Connection,
    id: &str,
    path: &str,
    fields: &[(String, String)],
    multiple: bool,
    mut visitor: impl FnMut(Value, Object, Object) -> Result<()>,
) -> Result<()> {
    let (source, from) = if multiple {
        (
            "item.value",
            "performance_snapshot AS snapshot,json_each(snapshot.ranges,?2) AS item",
        )
    } else {
        ("ranges", "performance_snapshot")
    };
    let mut selections = vec![if multiple {
        "item.key AS item_key,item.type AS object_type".to_owned()
    } else {
        "NULL AS item_key,json_type(ranges,?2) AS object_type".to_owned()
    }];
    let mut parameters = vec![SqlValue::Text(id.into()), SqlValue::Text(path.into())];
    for (name, key) in fields {
        parameters.push(SqlValue::Text(if multiple {
            format!("$.{key}")
        } else {
            format!("{path}.{key}")
        }));
        let index = parameters.len();
        selections.push(format!(
            "json_extract({source},?{index}) AS {},json_type({source},?{index}) AS {}",
            quote(name),
            quote(&format!("type_{name}"))
        ));
    }
    let query = format!(
        "SELECT {} FROM {from} WHERE {}.id=?1{}",
        selections.join(","),
        if multiple {
            "snapshot"
        } else {
            "performance_snapshot"
        },
        if multiple { " ORDER BY item.key" } else { "" }
    );
    let mut statement = connection.prepare(&query).map_err(sql)?;
    let mut cursor = statement.query(params_from_iter(parameters)).map_err(sql)?;
    while let Some(row) = cursor.next().map_err(sql)? {
        if row.get::<_, String>("object_type").map_err(sql)? != "object" {
            return Err(invalid(format!("Expected an object at {path}")));
        }
        let (mut values, mut kinds) = (Object::new(), Object::new());
        for (name, _) in fields {
            values.insert(
                name.clone(),
                from_sql(row.get(name.as_str()).map_err(sql)?)?,
            );
            kinds.insert(
                name.clone(),
                from_sql(row.get(format!("type_{name}").as_str()).map_err(sql)?)?,
            );
        }
        visitor(from_sql(row.get("item_key").map_err(sql)?)?, values, kinds)?;
    }
    Ok(())
}
fn json_type(connection: &Connection, id: &str, path: &str) -> Result<Value> {
    Ok(value(
        &one(
            connection,
            "SELECT json_type(ranges,?2) AS kind FROM performance_snapshot WHERE id=?1",
            rusqlite::params![id, path],
        )?,
        "kind",
    )
    .clone())
}
fn check_shapes(connection: &Connection, id: &str, key: &str) -> Result<()> {
    let base = format!("$.{key}");
    for (suffix, expected) in [
        ("", "object"),
        (".performance", "object"),
        (".performance.settings", "object"),
        (".performance.calendar", "object"),
        (".performance.points", "array"),
        (".performance.bindings", "array"),
        (".performance.sizing_fallback_ranges", "array"),
        (".performance.calendar.closed_ranges", "array"),
        (".performance.calendar.unavailable_ranges", "array"),
        (".daily_costs", "object"),
        (".events", "array"),
    ] {
        if json_type(connection, id, &format!("{base}{suffix}"))? != Value::from(expected) {
            return Err(invalid("Unsupported legacy result container shape"));
        }
    }
    let row=one(connection,"SELECT json_extract(ranges,?2) AS range_key,json_extract(ranges,?3) AS range_weight,json_extract(ranges,?4) AS range_fee,json_type(ranges,?4) AS range_fee_type,json_extract(ranges,?5) AS calendar_status,json_extract(settings,'$.backtest_weight_type') AS weight,json_extract(settings,'$.backtest_fee_rate') AS fee FROM performance_snapshot WHERE id=?1",rusqlite::params![id,format!("{base}.performance.range"),format!("{base}.performance.settings.backtest_weight_type"),format!("{base}.performance.settings.backtest_fee_rate"),format!("{base}.performance.calendar.status")])?;
    if value(&row, "range_key") != &Value::from(key)
        || value(&row, "range_weight") != value(&row, "weight")
        || !numeric_equal(value(&row, "range_fee"), value(&row, "fee"))
        || !matches!(
            value(&row, "range_fee_type").as_str(),
            Some("integer" | "real")
        )
    {
        return Err(invalid(
            "Legacy range metadata does not match its immutable snapshot",
        ));
    }
    if !matches!(
        value(&row, "calendar_status").as_str(),
        Some("available" | "partial" | "unavailable" | "not_required")
    ) {
        return Err(invalid("Unsupported legacy calendar status"));
    }
    Ok(())
}
fn numeric_equal(left: &Value, right: &Value) -> bool {
    left == right || (left.is_number() && right.is_number() && left.as_f64() == right.as_f64())
}
fn range_base(id: &str, key: &str) -> Object {
    serde_json::json!({"snapshot_id":id,"range_key":key})
        .as_object()
        .unwrap()
        .clone()
}
fn copy_array(
    connection: &Connection,
    id: &str,
    key: &str,
    table: &str,
    path: &str,
    renames: &[(&str, &str)],
    constants: &[(&str, &str)],
) -> Result<()> {
    let mut skip = vec!["snapshot_id", "range_key", "idx"];
    skip.extend(constants.iter().map(|(name, _)| *name));
    let fields = table_fields(connection, table, &skip)?
        .into_iter()
        .map(|name| {
            let key = renames
                .iter()
                .find(|(column, _)| *column == name)
                .map_or(name.clone(), |(_, key)| (*key).into());
            (name, key)
        })
        .collect::<Vec<_>>();
    each_extracted(
        connection,
        id,
        path,
        &fields,
        true,
        |index, values, kinds| {
            let mut row = range_base(id, key);
            row.insert("idx".into(), index);
            row.extend(values);
            for (name, item) in constants {
                row.insert((*name).into(), Value::from(*item));
            }
            put_validated(connection, table, &row, Some(&kinds))
        },
    )
}
fn backfill_range(connection: &Connection, id: &str, key: &str) -> Result<()> {
    check_shapes(connection, id, key)?;
    let base = format!("$.{key}");
    let table = "performance_result_range";
    let fields = table_fields(connection, table, &["snapshot_id", "range_key"])?
        .into_iter()
        .map(|name| {
            let path = match name.as_str() {
                "calendar_status" => "performance.calendar.status".into(),
                "calendar_id" => "performance.calendar.calendar_id".into(),
                "calendar_label" => "performance.calendar.label".into(),
                "event_count" => "event_count".into(),
                _ => format!("performance.{name}"),
            };
            (name, path)
        })
        .collect::<Vec<_>>();
    each_extracted(connection, id, &base, &fields, false, |_, values, kinds| {
        let mut row = range_base(id, key);
        row.extend(values);
        put_validated(connection, table, &row, Some(&kinds))
    })?;
    for (kind, role) in [("skips", "portfolio"), ("target_skips", "target")] {
        let path = format!("{base}.performance.{kind}");
        if matches!(
            json_type(connection, id, &path)?.as_str(),
            None | Some("null")
        ) {
            continue;
        }
        let fields = table_fields(
            connection,
            "performance_result_skip",
            &["snapshot_id", "range_key", "kind"],
        )?
        .into_iter()
        .map(|name| (name.clone(), name))
        .collect::<Vec<_>>();
        each_extracted(connection, id, &path, &fields, false, |_, values, kinds| {
            let mut row = range_base(id, key);
            row.insert("kind".into(), Value::from(role));
            row.extend(values);
            put_validated(connection, "performance_result_skip", &row, Some(&kinds))
        })?;
    }
    for (name, suffix) in [
        ("point", ".performance.points"),
        ("fallback", ".performance.sizing_fallback_ranges"),
        ("binding", ".performance.bindings"),
        ("event", ".events"),
    ] {
        copy_array(
            connection,
            id,
            key,
            &format!("performance_result_{name}"),
            &format!("{base}{suffix}"),
            if name == "event" {
                &[("execution_id", "executionId")]
            } else {
                &[]
            },
            &[],
        )?;
    }
    for (kind, role) in [
        ("closed_ranges", "closed"),
        ("unavailable_ranges", "unavailable"),
    ] {
        copy_array(
            connection,
            id,
            key,
            "performance_result_calendar_range",
            &format!("{base}.performance.calendar.{kind}"),
            &[],
            &[("kind", role)],
        )?;
    }
    let fields = DAILY_FIELDS
        .iter()
        .map(|(column, key)| ((*column).into(), (*key).into()))
        .collect::<Vec<_>>();
    each_extracted(
        connection,
        id,
        &format!("{base}.daily_costs"),
        &fields,
        true,
        |day, values, kinds| {
            let mut row = range_base(id, key);
            row.insert("day".into(), day.clone());
            row.insert(
                "present".into(),
                Value::from(
                    DAILY_FIELDS
                        .iter()
                        .enumerate()
                        .filter(|(_, (name, _))| !value(&kinds, name).is_null())
                        .map(|(index, _)| 1_i64 << index)
                        .sum::<i64>(),
                ),
            );
            row.extend(values);
            put_validated(
                connection,
                "performance_result_daily_cost",
                &row,
                Some(&kinds),
            )?;
            let path = format!(
                "{base}.daily_costs.{}.fees",
                serde_json::to_string(&day).map_err(|e| invalid(e.to_string()))?
            );
            for fee in rows(connection,"SELECT item.key AS currency,item.value,item.type FROM performance_snapshot AS snapshot,json_each(snapshot.ranges,?2) AS item WHERE snapshot.id=?1",rusqlite::params![id,path])? {
            let mut row=range_base(id,key);row.insert("day".into(),day.clone());row.insert("currency".into(),value(&fee,"currency").clone());row.insert("value".into(),value(&fee,"value").clone());
            let kinds=serde_json::json!({"value":value(&fee,"type")}).as_object().unwrap().clone();
            put_validated(connection,"performance_result_daily_fee",&row,Some(&kinds))?;
        }
            Ok(())
        },
    )
}
pub(super) fn upgrade35(connection: &Connection) -> Result<()> {
    execute(connection,"ALTER TABLE performance_snapshot ADD COLUMN result_schema_version INTEGER DEFAULT '0' NOT NULL; ALTER TABLE performance_snapshot ADD COLUMN backtest_weight_type TEXT; ALTER TABLE performance_snapshot ADD COLUMN backtest_fee_rate FLOAT;")?;
    schema::create35(connection)?;
    let mut after = Some(String::new());
    loop {
        let ids = snapshot_ids(connection, after.as_deref())?;
        if ids.is_empty() {
            break;
        }
        after = ids.last().cloned();
        for id in ids {
            let result = savepoint(connection, || {
                let row=one(connection,"SELECT cost_schema_version,json_valid(ranges) AS valid_ranges,json_valid(settings) AS valid_settings,CASE WHEN json_valid(settings) THEN json_extract(settings,'$.backtest_weight_type') END AS weight,CASE WHEN json_valid(settings) THEN json_extract(settings,'$.backtest_fee_rate') END AS fee,CASE WHEN json_valid(settings) THEN json_type(settings,'$.backtest_fee_rate') END AS fee_type FROM performance_snapshot WHERE id=?",[&id])?;
                if value(&row, "valid_ranges") != &Value::from(1)
                    || value(&row, "valid_settings") != &Value::from(1)
                    || value(&row, "cost_schema_version") != &Value::from(1)
                {
                    return Ok(());
                }
                if !matches!(value(&row, "fee_type").as_str(), Some("integer" | "real"))
                    || !matches!(value(&row, "weight").as_str(), Some("cs" | "ts"))
                    || !value(&row, "fee")
                        .as_f64()
                        .is_some_and(|fee| (0.0..1.0).contains(&fee))
                {
                    return Ok(());
                }
                for key in ["30", "90", "all"] {
                    backfill_range(connection, &id, key)?;
                }
                let values=serde_json::json!({"result_schema_version":2,"backtest_weight_type":value(&row,"weight"),"backtest_fee_rate":value(&row,"fee")}).as_object().unwrap().clone();
                update(
                    connection,
                    "performance_snapshot",
                    &values,
                    "id=?",
                    &[Value::from(id.as_str())],
                )
            });
            if let Err(failure) = result {
                if !matches!(failure.code.as_str(), "MIGRATION_VALUE" | "MIGRATION_SQL") {
                    return Err(failure);
                }
            }
        }
    }
    guards::install35(connection)
}
pub(super) fn downgrade35(connection: &Connection) -> Result<()> {
    restore::cost_payloads(connection)?;
    restore::results(connection)?;
    guards::drop35(connection)?;
    for table in RESULT_NAMES
        .iter()
        .chain(SOURCE_NAMES)
        .copied()
        .chain(["analysis_calendar_day"])
    {
        execute(connection, &format!("DROP TABLE {}", quote(table)))?;
    }
    execute(connection,"ALTER TABLE performance_snapshot DROP COLUMN backtest_fee_rate; ALTER TABLE performance_snapshot DROP COLUMN backtest_weight_type; ALTER TABLE performance_snapshot DROP COLUMN result_schema_version;")
}
pub(super) fn upgrade36(connection: &Connection) -> Result<()> {
    execute(connection,"ALTER TABLE account_analysis ADD COLUMN running_token TEXT; ALTER TABLE performance_snapshot ADD COLUMN read_view_schema_version INTEGER DEFAULT '0' NOT NULL")?;
    schema::create36(connection)?;
    execute(connection, include_str!("summary36.sql"))?;
    guards::install36(connection)
}
pub(super) fn downgrade36(connection: &Connection) -> Result<()> {
    execute(connection,"DROP TRIGGER IF EXISTS analysis_read_views_snapshot_delete; DROP TABLE performance_execution_fee; DROP TABLE performance_execution_summary; ALTER TABLE performance_snapshot DROP COLUMN read_view_schema_version; ALTER TABLE account_analysis DROP COLUMN running_token")
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn projection_normalizes_legacy_outcome_and_keeps_presence() {
        let result=compact_execution(json!({"record":{"raw_result":{"outcome":"error","msg":"RuntimeError: failed","symbol_results":{"x":{"status":"BLOCKED","error":"当前不在交易时段"}}}}}).as_object().unwrap()).unwrap();
        assert_eq!(result["record"]["raw_result"]["status"], "FAILED");
        assert_eq!(
            result["record"]["raw_result"]["error"],
            "执行失败，具体原因见执行证据"
        );
        assert_eq!(
            result["record"]["raw_result"]["reason_code"],
            "COMMON.SESSION.CLOSED"
        );
        assert!(!result["record"]["raw_result"]
            .as_object()
            .unwrap()
            .contains_key("technical_detail"));
    }

    fn baseline() -> Connection {
        let connection = Connection::open_in_memory().unwrap();
        connection.execute_batch("PRAGMA foreign_keys=OFF;
            CREATE TABLE account(id INTEGER PRIMARY KEY,trade_channel TEXT);
            CREATE TABLE executerecord(id INTEGER PRIMARY KEY,account_id INTEGER,execution_id TEXT,created_at TEXT,raw_input JSON,raw_result JSON,is_success INTEGER);
            CREATE TABLE target_weight_snapshot(id INTEGER PRIMARY KEY,account_id INTEGER,execution_id TEXT);
            CREATE TABLE performance_snapshot(id TEXT PRIMARY KEY,ranges JSON NOT NULL,settings JSON NOT NULL);
            CREATE TABLE performance_cost_execution(snapshot_id TEXT NOT NULL,record_id INTEGER NOT NULL,time FLOAT NOT NULL,success BOOLEAN NOT NULL,noop BOOLEAN NOT NULL,payload JSON NOT NULL,PRIMARY KEY(snapshot_id,record_id));
            CREATE TABLE performance_cost_trade(snapshot_id TEXT NOT NULL,id INTEGER NOT NULL,record_id INTEGER NOT NULL,symbol TEXT NOT NULL,time FLOAT NOT NULL,day TEXT NOT NULL,value FLOAT,cost FLOAT,loss_bp FLOAT,fee FLOAT,fee_currency TEXT,estimated BOOLEAN NOT NULL,payload JSON NOT NULL,PRIMARY KEY(snapshot_id,id));
            CREATE TABLE account_analysis(account_id INTEGER PRIMARY KEY,source_version INTEGER DEFAULT 0,failures INTEGER DEFAULT 0,current_snapshot TEXT,running_version INTEGER,requested BOOLEAN DEFAULT 0,retry_at FLOAT,error TEXT);
        ").unwrap();
        connection
    }

    #[test]
    fn incompatible_cost_snapshot_rolls_back_its_complete_graph() {
        let connection = baseline();
        connection.execute_batch("INSERT INTO performance_snapshot VALUES('bad','{}','{}');
            INSERT INTO performance_cost_execution VALUES('bad',1,1000,1,0,'{\"key\":\"kept\",\"positions\":[{\"symbol\":\"A\",\"quantity\":1}],\"attempts\":[{\"symbol\":\"A\",\"planned\":\"not-number\"}]}');
            INSERT INTO performance_snapshot VALUES('good','{}','{}');
            INSERT INTO performance_cost_execution VALUES('good',2,1000,1,0,'{\"record\":{\"raw_result\":{}},\"positions\":[]}');
        ").unwrap();
        upgrade34(&connection).unwrap();
        assert_eq!(
            connection
                .query_row(
                    "SELECT cost_schema_version FROM performance_snapshot WHERE id='bad'",
                    [],
                    |row| row.get::<_, i64>(0)
                )
                .unwrap(),
            0
        );
        assert_eq!(
            connection
                .query_row(
                    "SELECT key FROM performance_cost_execution WHERE snapshot_id='bad'",
                    [],
                    |row| row.get::<_, Option<String>>(0)
                )
                .unwrap(),
            None
        );
        assert_eq!(
            connection
                .query_row(
                    "SELECT COUNT(*) FROM performance_cost_position WHERE snapshot_id='bad'",
                    [],
                    |row| row.get::<_, i64>(0)
                )
                .unwrap(),
            0
        );
        assert_eq!(
            connection
                .query_row(
                    "SELECT cost_schema_version FROM performance_snapshot WHERE id='good'",
                    [],
                    |row| row.get::<_, i64>(0)
                )
                .unwrap(),
            1
        );
        downgrade34(&connection).unwrap();
        assert_eq!(connection.query_row("SELECT json_extract(payload,'$.key') FROM performance_cost_execution WHERE snapshot_id='bad'",[],|row|row.get::<_,String>(0)).unwrap(),"kept");
    }

    #[test]
    fn empty_legacy_results_keep_payloads_through_structured_roundtrip() {
        let connection = baseline();
        connection
            .execute_batch(
                "INSERT INTO performance_snapshot VALUES('legacy','{\"unrecognized\":[1,2]}','{}')",
            )
            .unwrap();
        upgrade34(&connection).unwrap();
        upgrade35(&connection).unwrap();
        upgrade36(&connection).unwrap();
        assert_eq!(
            connection
                .query_row(
                    "SELECT result_schema_version FROM performance_snapshot",
                    [],
                    |row| row.get::<_, i64>(0)
                )
                .unwrap(),
            0
        );
        downgrade36(&connection).unwrap();
        downgrade35(&connection).unwrap();
        downgrade34(&connection).unwrap();
        assert_eq!(
            connection
                .query_row("SELECT ranges FROM performance_snapshot", [], |row| row
                    .get::<_, String>(
                    0
                ))
                .unwrap(),
            "{\"unrecognized\":[1,2]}"
        );
    }
}
