//! Frozen execution facts. No model defaults or legacy payload fallback.

use rusqlite::{types::ValueRef, Connection, Row};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

// Python's notification hashes include insertion order and its JSON float spelling.
fn wire(value: &serde_json::Value) -> Result<String, String> {
    use serde_json::Value;
    Ok(match value {
        Value::Null => "null".into(),
        Value::Bool(b) => b.to_string(),
        Value::String(s) => serde_json::to_string(s).map_err(invalid)?,
        Value::Number(n) if n.is_f64() => {
            let f = n.as_f64().ok_or_else(|| invalid("invalid float"))?;
            let raw = n.to_string();
            let negative = raw.starts_with('-');
            let unsigned = raw.trim_start_matches('-');
            let (mantissa, explicit) = if let Some((a, b)) = unsigned.split_once(['e', 'E']) {
                (a, b.parse::<i32>().map_err(invalid)?)
            } else {
                (unsigned, 0)
            };
            let decimal = mantissa.find('.').unwrap_or(mantissa.len()) as i32;
            let digits = mantissa.replace('.', "");
            let first = digits.find(|c| c != '0');
            if let Some(first) = first {
                let exponent = explicit + decimal - first as i32 - 1;
                let significant = digits[first..].trim_end_matches('0');
                let sign = if negative { "-" } else { "" };
                if !(-4..16).contains(&exponent) {
                    let mut m = significant[..1].to_string();
                    if significant.len() > 1 {
                        m.push('.');
                        m.push_str(&significant[1..]);
                    }
                    format!(
                        "{sign}{m}e{}{:02}",
                        if exponent < 0 { "-" } else { "+" },
                        exponent.abs()
                    )
                } else {
                    let point = exponent + 1;
                    let mut fixed = if point <= 0 {
                        format!("0.{}{}", "0".repeat((-point) as usize), significant)
                    } else if point as usize >= significant.len() {
                        format!(
                            "{}{}.0",
                            significant,
                            "0".repeat(point as usize - significant.len())
                        )
                    } else {
                        format!(
                            "{}.{}",
                            &significant[..point as usize],
                            &significant[point as usize..]
                        )
                    };
                    fixed.insert_str(0, sign);
                    fixed
                }
            } else if f.is_sign_negative() {
                "-0.0".into()
            } else {
                "0.0".into()
            }
        }
        Value::Number(n) => n.to_string(),
        Value::Array(values) => format!(
            "[{}]",
            values
                .iter()
                .map(wire)
                .collect::<Result<Vec<_>, _>>()?
                .join(",")
        ),
        Value::Object(map) => format!(
            "{{{}}}",
            map.iter()
                .map(|(key, value)| Ok(format!(
                    "{}:{}",
                    serde_json::to_string(key).map_err(invalid)?,
                    wire(value)?
                )))
                .collect::<Result<Vec<_>, String>>()?
                .join(",")
        ),
    })
}
fn digest(value: &Value) -> Result<String, String> {
    Ok(format!("{:x}", Sha256::digest(wire(value)?.as_bytes())))
}
const EXEC_METADATA: &[&str] = &[
    "id",
    "kind",
    "trigger_source",
    "notified_at",
    "execution_time",
    "outcome_reason",
    "channel_type",
    "status",
    "success",
    "outcome",
    "reason_code",
    "error",
    "is_test",
    "supplements",
];
fn scalar(value: &Value) -> bool {
    !value.is_object() && !value.is_array()
}
fn reorder(values: Object, order: &Value) -> Result<Object, String> {
    let order = order
        .as_array()
        .ok_or_else(|| invalid("invalid notification field order"))?;
    if order.len() != values.len() {
        return Err(invalid("incomplete notification field order"));
    }
    let mut result = Object::new();
    for key in order {
        let key = key
            .as_str()
            .ok_or_else(|| invalid("invalid notification order key"))?;
        let value = values
            .get(key)
            .ok_or_else(|| invalid("unknown notification order key"))?;
        if result.insert(key.into(), value.clone()).is_some() {
            return Err(invalid("duplicate notification order key"));
        }
    }
    Ok(result)
}
fn hydrate(conn: &Connection, record: &Object, result: &mut Object) -> Result<(), String> {
    let Some(extra) = result.get_mut("extra").and_then(Value::as_object_mut) else {
        return Ok(());
    };
    let Some(reference) = extra
        .get("server_notification_context")
        .and_then(Value::as_object)
    else {
        return Ok(());
    };
    if !reference.contains_key("$axile_notification_context") {
        return Ok(());
    }
    let expected = [
        "$axile_notification_context",
        "event_id",
        "event_sha256",
        "capture_sha256",
        "root_order",
        "root_metadata",
        "execution_state",
        "execution_order",
        "execution_metadata",
    ];
    if reference.len() != expected.len()
        || expected.iter().any(|k| !reference.contains_key(*k))
        || get(reference, "$axile_notification_context")?.as_i64() != Some(1)
    {
        return Err(invalid("invalid notification reference"));
    }
    let event_id = format!("execution:{}", text(record, "execution_id")?);
    let events = load(conn, "notification_event", "id", &json!(event_id))?;
    let event = one(&events)?;
    if get(event, "account_id")? != get(record, "account_id")?
        || text(event, "event_type")? != "execution.finished"
        || text(reference, "event_id")? != event_id
        || text(reference, "event_sha256")? != digest(get(event, "context")?)?
    {
        return Err(invalid("notification identity or digest mismatch"));
    }
    let root_metadata = object(get(reference, "root_metadata")?)?;
    let execution_metadata = object(get(reference, "execution_metadata")?)?;
    if root_metadata
        .iter()
        .any(|(k, v)| !["event_id", "event_type"].contains(&k.as_str()) || !scalar(v))
    {
        return Err(invalid("notification root metadata override"));
    }
    for (key, value) in execution_metadata {
        if !EXEC_METADATA.contains(&key.as_str()) {
            return Err(invalid("notification business override"));
        }
        if key == "supplements" {
            let supplements = value
                .as_array()
                .ok_or_else(|| invalid("invalid notification supplements"))?;
            for item in supplements {
                if object(item)?.iter().any(|(k, v)| {
                    ![
                        "group_id",
                        "base_scheduled_at",
                        "scheduled_at",
                        "index",
                        "configured_count",
                        "effective_count",
                        "is_last",
                        "group_status",
                        "end_reason",
                    ]
                    .contains(&k.as_str())
                        || !scalar(v)
                }) {
                    return Err(invalid("notification supplement override"));
                }
            }
        } else if !scalar(value) {
            return Err(invalid("notification execution metadata override"));
        }
    }
    let mut context = object(get(event, "context")?)?.clone();
    for key in ["event_id", "event_type"] {
        context.shift_remove(key);
    }
    context.extend(root_metadata.clone());
    match text(reference, "execution_state")? {
        "object" => {
            let mut execution = object(
                context
                    .get("execution")
                    .ok_or_else(|| invalid("missing notification execution"))?,
            )?
            .clone();
            for key in EXEC_METADATA {
                execution.shift_remove(*key);
            }
            execution.extend(execution_metadata.clone());
            context.insert(
                "execution".into(),
                Value::Object(reorder(execution, get(reference, "execution_order")?)?),
            );
        }
        "null" => {
            context.insert("execution".into(), Value::Null);
        }
        "absent" => {
            context.shift_remove("execution");
        }
        _ => return Err(invalid("invalid notification execution state")),
    }
    let restored = Value::Object(reorder(context, get(reference, "root_order")?)?);
    if text(reference, "capture_sha256")? != digest(&restored)? {
        return Err(invalid("notification capture digest mismatch"));
    }
    extra.insert("server_notification_context".into(), restored);
    Ok(())
}

/// Read one authorized record inside the caller's Store snapshot.
pub fn read(conn: &Connection, account_id: i64, record_id: i64) -> Result<Value, String> {
    let id = json!(record_id);
    let heads = load(conn, "executerecord", "id", &id)?;
    if heads.is_empty() {
        return Err("EXECUTION_NOT_FOUND: execution does not exist".into());
    }
    let head = one(&heads)?;
    if number(head, "account_id")? != account_id {
        return Err("EXECUTION_NOT_FOUND: execution does not exist".into());
    }
    if get(head, "facts_complete")? != &json!(true) {
        return Err(invalid("execution facts are not complete"));
    }
    let primary = if get(head, "input_id")?.is_null() {
        Value::Null
    } else {
        input(conn, get(head, "input_id")?, head)?
    };
    let extensions = load(conn, "execution_extension", "record_id", &id)?;
    let extension = one(&extensions)?;
    let kind = text(extension, "root_kind")?;
    // Resolve references even for malformed result roots, as the Python repository does.
    let result_input = match text(head, "result_input_kind")? {
        "value" => Some(input(conn, get(head, "result_input_id")?, head)?),
        "null" => Some(Value::Null),
        "invalid" => Some(get(head, "result_input_value")?.clone()),
        "absent" => None,
        _ => return Err(invalid("unknown result input state")),
    };
    // Asset owners are checked before root dispatch, including scalar/null result roots.
    let mut asset_fragment = Object::new();
    assets(conn, &id, head, &mut asset_fragment)?;
    let result = if kind != "object" {
        object(get(extension, "audit_evidence")?)?;
        let value = get(extension, "root_value")?;
        let valid = match kind {
            "null" => value.is_null(),
            "empty_list" => value.as_array().is_some_and(Vec::is_empty),
            "scalar" => {
                value.is_string()
                    || value.is_boolean()
                    || value.is_number()
                    || value
                        .as_object()
                        .is_some_and(|m| m.len() == 1 && m.contains_key("invalid_number"))
            }
            _ => false,
        };
        if !valid {
            return Err(invalid("result root state disagrees with value"));
        }
        value.clone()
    } else {
        let mut result = fields(head, HEAD)?;
        match text(head, "termination_kind")? {
            "absent" => {}
            "null" => {
                result.insert("termination".into(), Value::Null);
            }
            "invalid" => {
                result.insert(
                    "termination".into(),
                    get(object(get(head, "termination_evidence")?)?, "value")?.clone(),
                );
            }
            "value" => {
                let mut typed = Object::new();
                for &key in TERM {
                    typed.insert(
                        key.into(),
                        get(head, &format!("termination_{key}"))?.clone(),
                    );
                }
                let evidence = get(head, "termination_evidence")?;
                let mut term = columns(&typed, TERM, evidence)?;
                match text(head, "termination_cancel_ids_kind")? {
                    "absent" => {}
                    "null" => {
                        term.insert("cancel_failed_order_ids".into(), Value::Null);
                    }
                    "invalid" => {
                        term.insert(
                            "cancel_failed_order_ids".into(),
                            get(object(evidence)?, "cancel_ids_value")?.clone(),
                        );
                    }
                    "value" => {
                        let rows = ordered(
                            load(
                                conn,
                                "execution_termination_cancel_failure",
                                "record_id",
                                &id,
                            )?,
                            get(head, "termination_cancel_count")?,
                        )?;
                        let mut values = Vec::new();
                        let mut invalid_ids = Object::new();
                        for row in rows {
                            values.push(get(&row, "order_id")?.clone());
                            if let Some(value) = object(get(&row, "audit_evidence")?)?
                                .get("invalid_fields")
                                .and_then(Value::as_object)
                                .and_then(|m| m.get("order_id"))
                            {
                                invalid_ids
                                    .insert(number(&row, "ordinal")?.to_string(), value.clone());
                            }
                        }
                        term.insert("cancel_failed_order_ids".into(), Value::Array(values));
                        if !invalid_ids.is_empty() {
                            let audit = term.entry("audit_evidence").or_insert_with(|| json!({}));
                            audit
                                .as_object_mut()
                                .ok_or_else(|| invalid("invalid termination evidence"))?
                                .insert(
                                    "invalid_cancel_order_ids".into(),
                                    Value::Object(invalid_ids),
                                );
                        }
                    }
                    _ => return Err(invalid("unknown termination cancel state")),
                }
                result.insert("termination".into(), Value::Object(term));
            }
            _ => return Err(invalid("unknown termination state")),
        }
        symbols(conn, &id, &mut result)?;
        collections(conn, &id, &mut result, false)?;
        collections(conn, &id, &mut result, true)?;
        merge(&mut result, asset_fragment)?;
        merge(
            &mut result,
            fields(extension, &["memory", "extra", "reconciliation"])?,
        )?;
        if let Some(value) = result_input {
            result.insert("inputs".into(), value);
        }
        hydrate(conn, head, &mut result)?;
        Value::Object(result)
    };
    Ok(json!({"input":primary,"result":result}))
}

type Object = Map<String, Value>;
type Rows = Vec<Object>;
fn invalid(message: impl std::fmt::Display) -> String {
    format!("INVALID_DATA: {message}")
}
fn db(error: rusqlite::Error) -> String {
    format!("EXECUTION_READ_ERROR: {error}")
}
fn get<'a>(row: &'a Object, key: &str) -> Result<&'a Value, String> {
    row.get(key)
        .ok_or_else(|| invalid(format!("missing column {key}")))
}
fn text<'a>(row: &'a Object, key: &str) -> Result<&'a str, String> {
    get(row, key)?
        .as_str()
        .ok_or_else(|| invalid(format!("invalid text {key}")))
}
fn number(row: &Object, key: &str) -> Result<i64, String> {
    get(row, key)?
        .as_i64()
        .ok_or_else(|| invalid(format!("invalid integer {key}")))
}
fn object(value: &Value) -> Result<&Object, String> {
    value.as_object().ok_or_else(|| invalid("expected object"))
}
fn one(rows: &Rows) -> Result<&Object, String> {
    if rows.len() != 1 {
        return Err(invalid("expected exactly one fact"));
    }
    Ok(&rows[0])
}
pub(crate) fn load(
    conn: &Connection,
    table: &str,
    column: &str,
    value: &Value,
) -> Result<Rows, String> {
    let mut statement = conn
        .prepare(&format!("SELECT * FROM {table} WHERE {column}=?1"))
        .map_err(db)?;
    let parameter = match value {
        Value::String(s) => rusqlite::types::Value::Text(s.clone()),
        Value::Number(n) => rusqlite::types::Value::Integer(
            n.as_i64().ok_or_else(|| invalid("invalid identifier"))?,
        ),
        _ => return Err(invalid("invalid identifier")),
    };
    let mut cursor = statement.query([parameter]).map_err(db)?;
    let mut result = Vec::new();
    while let Some(row) = cursor.next().map_err(db)? {
        result.push(decode(row, table)?);
    }
    Ok(result)
}
pub(crate) fn decode(row: &Row<'_>, table: &str) -> Result<Object, String> {
    let mut item = Object::new();
    for (index, name) in row.as_ref().column_names().iter().enumerate() {
        // Frozen business0037 schema; algorithm is JSON only on execution_input.
        let json_column = matches!(
            (table, *name),
            (_, "audit_evidence")
                | ("notification_event", "context")
                | (
                    "executerecord",
                    "result_input_value" | "termination_evidence"
                )
                | (
                    "execution_input",
                    "root_value"
                        | "algorithm"
                        | "symbol_algorithms"
                        | "trade_rules"
                        | "futures_close_policy"
                        | "feishu_key"
                        | "feishu_account"
                        | "extra"
                )
                | ("execution_termination_cancel_failure", "order_id")
                | (
                    "execution_symbol_collection"
                        | "execution_symbol_result"
                        | "execution_symbol_sizing"
                        | "execution_symbol_conversion"
                        | "execution_symbol_tick"
                        | "execution_symbol_target",
                    "invalid_value"
                )
                | ("execution_symbol_result", "memory")
                | (
                    "execution_symbol_conversion" | "business_trade_conversion",
                    "reference"
                )
                | ("execution_symbol_tick", "extra")
                | (
                    "business_asset_snapshot",
                    "payload_value" | "positions_value" | "extra"
                )
                | ("business_asset_position", "payload_value" | "extra")
                | ("execution_order_collection", "invalid_value")
                | (
                    "execution_order",
                    "invalid_value"
                        | "order_id"
                        | "extra"
                        | "conversion_invalid_value"
                        | "conversion_reference"
                        | "conversion_evidence"
                )
                | (
                    "business_trade_collection" | "business_trade" | "business_trade_conversion",
                    "malformed_value"
                )
                | ("business_trade", "trade_id" | "order_id" | "extra")
                | (
                    "execution_extension",
                    "memory" | "extra" | "reconciliation" | "root_value"
                )
        );
        let bool_column = matches!(
            (table, *name),
            (
                "executerecord",
                "success"
                    | "facts_complete"
                    | "termination_cancel_attempted"
                    | "termination_forced"
                    | "termination_cancel_unconfirmed"
            ) | (
                "execution_symbol_result",
                "success" | "sizing_recorded" | "tick_recorded"
            ) | (
                "execution_symbol_sizing" | "business_trade",
                "conversion_recorded"
            ) | ("execution_symbol_tick", "book_valid")
                | ("business_asset_snapshot", "observation_valid")
        );
        let value = match row.get_ref(index).map_err(db)? {
            ValueRef::Null => Value::Null,
            ValueRef::Integer(n) if bool_column => json!(n != 0),
            ValueRef::Integer(n) => json!(n),
            ValueRef::Real(n) => Value::Number(
                serde_json::Number::from_f64(n).ok_or_else(|| invalid("nonfinite column"))?,
            ),
            ValueRef::Text(bytes) => {
                let s = std::str::from_utf8(bytes).map_err(invalid)?;
                if json_column {
                    serde_json::from_str(s).map_err(invalid)?
                } else {
                    json!(s)
                }
            }
            ValueRef::Blob(_) => return Err(invalid("unexpected fact blob")),
        };
        item.insert((*name).into(), value);
    }
    Ok(item)
}
fn columns(row: &Object, fields: &[&str], evidence: &Value) -> Result<Object, String> {
    let evidence = object(evidence)?;
    let mut result = Object::new();
    for &key in fields {
        let value = get(row, key)?;
        if !value.is_null() || fields.len() == 1 {
            result.insert(key.into(), value.clone());
        }
    }
    if !evidence.is_empty() {
        result.insert("audit_evidence".into(), Value::Object(evidence.clone()));
    }
    Ok(result)
}
fn fields(row: &Object, names: &[&str]) -> Result<Object, String> {
    columns(row, names, get(row, "audit_evidence")?)
}
fn ordered(mut rows: Rows, count: &Value) -> Result<Rows, String> {
    rows.sort_by_key(|r| r.get("ordinal").and_then(Value::as_i64).unwrap_or(-1));
    if count.as_i64() != Some(rows.len() as i64) {
        return Err(invalid("incomplete collection facts"));
    }
    for (index, row) in rows.iter().enumerate() {
        if number(row, "ordinal")? != index as i64 {
            return Err(invalid("invalid occurrence sequence"));
        }
    }
    Ok(rows)
}
fn pointer(path: &str, key: &str) -> String {
    format!("{path}/{}", key.replace('~', "~0").replace('/', "~1"))
}
fn merge(target: &mut Object, fragment: Object) -> Result<(), String> {
    for (key, value) in fragment {
        if let Some(existing) = target.get_mut(&key) {
            match (existing, value) {
                (Value::Object(a), Value::Object(b)) => merge(a, b)?,
                (existing, value) if *existing == value => {}
                _ => return Err(invalid(format!("conflicting fact field {key}"))),
            }
        } else {
            target.insert(key, value);
        }
    }
    Ok(())
}
fn put(result: &mut Object, path: &str, value: Value) -> Result<(), String> {
    let parts = path
        .strip_prefix('/')
        .ok_or_else(|| invalid("invalid fact path"))?
        .split('/')
        .map(|s| s.replace("~1", "/").replace("~0", "~"))
        .collect::<Vec<_>>();
    if !(parts.len() == 1 || parts.len() == 3 && parts[0] == "symbol_results") {
        return Err(invalid("unsupported fact path"));
    }
    let mut fragment = value;
    for part in parts.iter().rev() {
        let mut map = Object::new();
        map.insert(part.clone(), fragment);
        fragment = Value::Object(map);
    }
    let Value::Object(fragment) = fragment else {
        return Err(invalid("expected object"));
    };
    merge(result, fragment)
}
fn assets(
    conn: &Connection,
    id: &Value,
    record: &Object,
    result: &mut Object,
) -> Result<(), String> {
    let mut bindings = load(conn, "business_asset_binding", "record_id", id)?;
    bindings.sort_by_key(|r| r.get("ordinal").and_then(Value::as_i64).unwrap_or(-1));
    for binding in bindings {
        let mut statement = conn
            .prepare("SELECT * FROM business_asset_snapshot WHERE asset_id=?1")
            .map_err(db)?;
        let mut root_cursor = statement.query([text(&binding, "asset_id")?]).map_err(db)?;
        let root_row = root_cursor
            .next()
            .map_err(db)?
            .ok_or_else(|| invalid("expected exactly one fact"))?;
        let decoded = decode(root_row, "business_asset_snapshot")?;
        let root = &decoded;
        if get(root, "record_id")?.is_null() {
            let input_id = get(root, "input_id")?;
            if input_id.is_null()
                || !(input_id == get(record, "input_id")?
                    || input_id == get(record, "result_input_id")?)
            {
                return Err(invalid("asset input does not belong to execution"));
            }
            let owners = load(conn, "execution_input", "id", input_id)?;
            let owner = one(&owners)?;
            let execution = if get(owner, "owner_execution_id")?.is_null() {
                get(owner, "execution_id")?
            } else {
                get(owner, "owner_execution_id")?
            };
            if get(owner, "account_id")? != get(record, "account_id")?
                || execution != get(record, "execution_id")?
                || !get(owner, "execution_id")?.is_null()
                    && get(owner, "execution_id")? != get(record, "execution_id")?
            {
                return Err(invalid("asset owner mismatch"));
            }
            if text(root, "path")? != "/after_account_assets"
                || text(root, "scope")? != "after_pending"
                || text(&binding, "path")? != "/account_assets"
                || text(&binding, "scope")? != "after"
            {
                return Err(invalid("invalid indirect asset binding"));
            }
        } else {
            if get(root, "record_id")? != id {
                return Err(invalid("asset belongs to another execution"));
            }
            for key in ["path", "scope", "ordinal", "map_key"] {
                if get(root, key)? != get(&binding, key)? {
                    return Err(invalid("asset occurrence mismatch"));
                }
            }
        }
        let asset_id = text(root, "asset_id")?;
        let mut statement = conn
            .prepare("SELECT * FROM business_asset_position WHERE asset_id=?1 ORDER BY ordinal")
            .map_err(db)?;
        let mut cursor = statement.query([asset_id]).map_err(db)?;
        let mut positions = Vec::new();
        while let Some(row) = cursor.next().map_err(db)? {
            positions.push((
                row.get("ordinal").map_err(db)?,
                crate::assets::position(row)?,
            ));
        }
        let value = if text(root, "payload_kind")? == "object" {
            crate::assets::observation(root_row, &positions)?
        } else {
            get(root, "payload_value")?.clone()
        };
        if root_cursor.next().map_err(db)?.is_some() {
            return Err(invalid("expected exactly one fact"));
        }
        let scope = text(&binding, "scope")?;
        if scope == "symbol" {
            put(
                result,
                &pointer(
                    &pointer("/symbol_results", text(&binding, "map_key")?),
                    "account_assets",
                ),
                value,
            )?;
        } else {
            let key = match scope {
                "before" => "before_account_assets",
                "after_pending" => "after_account_assets",
                _ => "account_assets",
            };
            result.insert(key.into(), value);
        }
    }
    Ok(())
}
const INPUT: &[&str] = &[
    "channel_type",
    "execution_timeout",
    "algorithm",
    "symbol_algorithms",
    "trade_rules",
    "futures_close_policy",
    "feishu_key",
    "feishu_account",
    "extra",
];
fn input(conn: &Connection, id: &Value, record: &Object) -> Result<Value, String> {
    let rows = load(conn, "execution_input", "id", id)?;
    let head = one(&rows)?;
    let owner = if get(head, "owner_execution_id")?.is_null() {
        get(head, "execution_id")?
    } else {
        get(head, "owner_execution_id")?
    };
    if get(head, "account_id")? != get(record, "account_id")?
        || owner != get(record, "execution_id")?
    {
        return Err(invalid("input belongs to another account or execution"));
    }
    if get(record, "execution_id")?.is_null() {
        let shared:bool=conn.query_row("SELECT EXISTS(SELECT 1 FROM executerecord WHERE id!=?1 AND (input_id=?2 OR result_input_id=?2))",rusqlite::params![number(record,"id")?,id.as_str().ok_or_else(||invalid("invalid input id"))?],|r|r.get(0)).map_err(db)?;
        if shared {
            return Err(invalid("anonymous execution input is shared"));
        }
    }
    if text(head, "root_kind")? != "object" {
        return Ok(get(head, "root_value")?.clone());
    }
    let targets = load(conn, "execution_input_target", "input_id", id)?;
    let symbols = load(conn, "execution_input_symbol", "input_id", id)?;
    let plans = load(conn, "execution_input_plan", "input_id", id)?;
    decode_input(head, &targets, &symbols, &plans)
}

/// 冻结输入的读端口径:事实行到业务 DTO,供详情与重放比较共用。
pub(crate) fn decode_input(
    head: &Object,
    targets: &Rows,
    symbols: &Rows,
    plans: &Rows,
) -> Result<Value, String> {
    let mut result = fields(head, INPUT)?;
    for role in [
        "curr_target",
        "last_target",
        "strategy_target",
        "forbidden_symbols",
        "risk_symbols",
        "sizing_context",
    ] {
        let state = text(head, &format!("{role}_state"))?;
        let value = match state {
            "absent" => continue,
            "null" | "invalid" => Value::Null,
            "value" if role == "sizing_context" => Value::Object(columns(
                head,
                &["long_leverage", "short_leverage", "weight_precision"],
                object(get(head, "audit_evidence")?)?
                    .get("sizing")
                    .unwrap_or(&json!({})),
            )?),
            "value" if role.ends_with("target") => {
                let members = targets
                    .iter()
                    .filter(|r| r.get("role") == Some(&json!(role)))
                    .collect::<Vec<_>>();
                if get(head, &format!("{role}_count"))?.as_i64() != Some(members.len() as i64) {
                    return Err(invalid("incomplete frozen targets"));
                }
                let mut map = Object::new();
                for row in members {
                    map.insert(text(row, "map_key")?.into(), get(row, "value")?.clone());
                }
                Value::Object(map)
            }
            "value" => Value::Array(
                ordered(
                    symbols
                        .iter()
                        .filter(|r| r.get("role") == Some(&json!(role)))
                        .cloned()
                        .collect(),
                    get(head, &format!("{role}_count"))?,
                )?
                .iter()
                .map(|r| get(r, "value").cloned())
                .collect::<Result<_, _>>()?,
            ),
            _ => return Err(invalid("unknown input collection state")),
        };
        result.insert(role.into(), value);
    }
    let mut plan = Object::new();
    for (role, key, out) in [
        ("quantities", "quantity", "quantities"),
        ("target_units", "units", "units"),
    ] {
        match text(head, &format!("{role}_state"))? {
            "absent" => {}
            "null" | "invalid" => {
                plan.insert(out.into(), Value::Null);
            }
            "value" => {
                let members = plans
                    .iter()
                    .filter(|r| r.get(&format!("{key}_state")) != Some(&json!("absent")))
                    .collect::<Vec<_>>();
                if get(head, &format!("{role}_count"))?.as_i64() != Some(members.len() as i64) {
                    return Err(invalid("incomplete frozen quantity plan"));
                }
                let mut map = Object::new();
                for row in members {
                    map.insert(text(row, "symbol")?.into(), get(row, key)?.clone());
                }
                plan.insert(out.into(), Value::Object(map));
            }
            _ => return Err(invalid("unknown plan state")),
        }
    }
    if !plans.is_empty() {
        let mut states = Object::new();
        for row in plans.iter() {
            states.insert(
                text(row, "symbol")?.into(),
                json!({"quantity":get(row,"quantity_state")?,"units":get(row,"units_state")?}),
            );
        }
        plan.insert("availability".into(), Value::Object(states));
    }
    if !plan.is_empty() {
        result.insert("plan".into(), Value::Object(plan));
    }
    Ok(Value::Object(result))
}
const HEAD: &[&str] = &[
    "symbol",
    "technical_detail",
    "status",
    "task_status",
    "error",
    "outcome",
    "outcome_reason",
    "reason_code",
    "msg",
    "execution_kind",
    "channel_type",
    "execution_time",
    "success",
];
const TERM: &[&str] = &[
    "reason",
    "mode",
    "trigger",
    "requested_at",
    "acked_at",
    "finished_at",
    "cancel_attempted",
    "forced",
    "cancel_unconfirmed",
];
const SYMBOL: &[&str] = &[
    "symbol",
    "algorithm",
    "status",
    "error",
    "technical_detail",
    "reason_code",
    "outcome",
    "outcome_reason",
    "target_volume",
    "final_volume",
    "success",
    "memory",
];
const SIZING: &[&str] = &[
    "symbol",
    "sizing_mode",
    "status",
    "reason_code",
    "account_weight",
    "equity",
    "reference_price",
    "unit_multiplier",
    "unit_notional",
    "target_notional",
    "raw_quantity",
    "target_quantity",
    "current_quantity",
    "quantity_step",
    "min_quantity",
    "min_notional",
];
const CONVERSION: &[&str] = &["price_currency", "value_currency", "rate", "reference"];
const ORDER: &[&str] = &[
    "order_id",
    "symbol",
    "direction",
    "order_type",
    "volume",
    "price",
    "status",
    "filled_volume",
    "avg_price",
    "create_time",
    "update_time",
    "update_timestamp",
    "remaining_volume",
    "filled_ratio",
    "extra",
];
const TRADE: &[&str] = &[
    "trade_id",
    "order_id",
    "symbol",
    "trade_volume",
    "trade_price",
    "trade_value",
    "trade_time",
    "extra",
];
fn availability(view: &mut Object, row: &Object, keys: &[&str]) -> Result<(), String> {
    let mut states = Object::new();
    for &key in keys {
        let state = text(row, &format!("{key}_state"))?;
        if !["absent", "null", "invalid", "value"].contains(&state) {
            return Err(invalid("unknown availability"));
        }
        states.insert(key.into(), json!(state));
        if state == "null" {
            view.insert(key.into(), Value::Null);
        }
    }
    view.insert("availability".into(), Value::Object(states));
    Ok(())
}
fn node(row: &Object, specs: &[&str]) -> Result<Value, String> {
    object(get(row, "audit_evidence")?)?;
    match text(row, "node_kind")? {
        "value" => Ok(get(row, "invalid_value")?.clone()),
        "object" => Ok(Value::Object(fields(row, specs)?)),
        _ => Err(invalid("invalid node kind")),
    }
}
fn find<'a>(rows: &'a Rows, key: &str, value: &str) -> Result<Option<&'a Object>, String> {
    let mut matches = rows
        .iter()
        .filter(|r| r.get(key).and_then(Value::as_str) == Some(value));
    let found = matches.next();
    if matches.next().is_some() {
        return Err(invalid("duplicate fact path"));
    }
    Ok(found)
}
fn symbols(conn: &Connection, id: &Value, result: &mut Object) -> Result<(), String> {
    let collections = load(conn, "execution_symbol_collection", "record_id", id)?;
    let results = load(conn, "execution_symbol_result", "record_id", id)?;
    let sizings = load(conn, "execution_symbol_sizing", "record_id", id)?;
    let conversions = load(conn, "execution_symbol_conversion", "record_id", id)?;
    let ticks = load(conn, "execution_symbol_tick", "record_id", id)?;
    let targets = load(conn, "execution_symbol_target", "record_id", id)?;
    for rows in [
        &collections,
        &results,
        &sizings,
        &conversions,
        &ticks,
        &targets,
    ] {
        let mut paths = std::collections::HashSet::new();
        for row in rows {
            if !paths.insert(text(row, "path")?) {
                return Err(invalid("duplicate symbol fact path"));
            }
        }
    }
    let mut tick_names = vec!["symbol".to_string(), "last_price".into()];
    for side in ["bid", "ask"] {
        for field in ["price", "volume"] {
            for suffix in ["", "_2", "_3", "_4", "_5"] {
                tick_names.push(format!("{side}_{field}{suffix}"));
            }
        }
    }
    tick_names.extend(
        [
            "volume",
            "turnover",
            "timestamp",
            "update_time",
            "timestamp_datetime",
            "book_valid",
            "extra",
        ]
        .iter()
        .map(|s| s.to_string()),
    );
    let tick_refs = tick_names.iter().map(String::as_str).collect::<Vec<_>>();
    for name in ["symbol_results", "first_ticks", "target_volume"] {
        let path = format!("/{name}");
        let Some(root) = find(&collections, "path", &path)? else {
            continue;
        };
        let mut value = node(root, &[])?;
        if let Some(map) = value.as_object_mut() {
            let source = if name == "symbol_results" {
                &results
            } else if name == "first_ticks" {
                &ticks
            } else {
                &targets
            };
            let members = ordered(
                source
                    .iter()
                    .filter(|r| {
                        r.get("path")
                            .and_then(Value::as_str)
                            .is_some_and(|p| p.starts_with(&format!("{path}/")))
                    })
                    .cloned()
                    .collect(),
                get(root, "member_count")?,
            )?;
            for row in members {
                let key = text(&row, "map_key")?;
                let child = text(&row, "path")?;
                if child != pointer(&path, key) {
                    return Err(invalid("invalid symbol occurrence"));
                }
                let mut item = node(
                    &row,
                    if name == "symbol_results" {
                        SYMBOL
                    } else if name == "first_ticks" {
                        &tick_refs
                    } else {
                        &["value"]
                    },
                )?;
                if name == "symbol_results" {
                    if let Some(view) = item.as_object_mut() {
                        availability(view, &row, &["target_volume", "final_volume"])?;
                        for (field, source, specs, flag) in [
                            ("sizing", &sizings, SIZING, "sizing_recorded"),
                            ("first_tick", &ticks, tick_refs.as_slice(), "tick_recorded"),
                        ] {
                            let nested = pointer(child, field);
                            if let Some(saved) = find(source, "path", &nested)? {
                                let mut decoded = node(saved, specs)?;
                                if field == "sizing" {
                                    if let Some(sizing) = decoded.as_object_mut() {
                                        let conversion_path = pointer(&nested, "value_conversion");
                                        if let Some(conversion) =
                                            find(&conversions, "path", &conversion_path)?
                                        {
                                            sizing.insert(
                                                "value_conversion".into(),
                                                node(conversion, CONVERSION)?,
                                            );
                                        } else if get(saved, "conversion_recorded")? == &json!(true)
                                        {
                                            return Err(invalid("missing sizing conversion"));
                                        }
                                    }
                                }
                                view.insert(field.into(), decoded);
                            } else if get(&row, flag)? == &json!(true) {
                                return Err(invalid("missing nested symbol fact"));
                            }
                        }
                    }
                } else if name == "target_volume" {
                    if let Some(target) = item.as_object() {
                        if let Some(evidence) = target.get("audit_evidence") {
                            let fragment = json!({"audit_evidence":{"invalid_target_values":{"target_volume":{key:evidence}}}});
                            merge(result, object(&fragment)?.clone())?;
                        }
                        item = get(target, "value")?.clone();
                    }
                }
                map.insert(key.into(), item);
            }
        }
        result.insert(name.into(), value);
    }
    Ok(())
}
fn collections(
    conn: &Connection,
    id: &Value,
    result: &mut Object,
    trade: bool,
) -> Result<(), String> {
    let (collection_table, item_table) = if trade {
        ("business_trade_collection", "business_trade")
    } else {
        ("execution_order_collection", "execution_order")
    };
    let state_field = if trade { "state" } else { "kind" };
    let path_field = if trade { "collection_path" } else { "path" };
    let count_field = if trade { "member_count" } else { "item_count" };
    let invalid_field = if trade {
        "malformed_value"
    } else {
        "invalid_value"
    };
    let specs = if trade { TRADE } else { ORDER };
    let ids: &[&str] = if trade {
        &["trade_id", "order_id"]
    } else {
        &["order_id"]
    };
    let invalid_states: &[&str] = if trade {
        &["malformed"]
    } else {
        &["null", "invalid"]
    };
    let roots = load(conn, collection_table, "record_id", id)?;
    let items = load(conn, item_table, "record_id", id)?;
    let conversions = if trade {
        load(conn, "business_trade_conversion", "record_id", id)?
    } else {
        Vec::new()
    };
    let mut consumed = 0;
    for root in roots {
        let path = text(&root, "path")?;
        let state = text(&root, state_field)?;
        let value = if state == "array" {
            let members = ordered(
                items
                    .iter()
                    .filter(|r| r.get(path_field).and_then(Value::as_str) == Some(path))
                    .cloned()
                    .collect(),
                get(&root, count_field)?,
            )?;
            consumed += members.len();
            let mut values = Vec::new();
            for row in members {
                let state = text(&row, state_field)?;
                if state != "object" {
                    if !invalid_states.contains(&state) {
                        return Err(invalid("unknown item state"));
                    }
                    values.push(get(&row, invalid_field)?.clone());
                    continue;
                }
                let mut view = fields(&row, specs)?;
                availability(&mut view, &row, ids)?;
                if trade {
                    let conversion = find(&conversions, "trade_path", text(&row, "path")?)?;
                    if let Some(saved) = conversion {
                        view.insert(
                            "value_conversion".into(),
                            if text(saved, "state")? == "object" {
                                Value::Object(fields(saved, CONVERSION)?)
                            } else {
                                get(saved, "malformed_value")?.clone()
                            },
                        );
                    } else if get(&row, "conversion_recorded")? == &json!(true) {
                        return Err(invalid("missing trade conversion"));
                    }
                } else {
                    match get(&row, "conversion_kind")? {
                        Value::Null => {}
                        Value::String(kind) if kind == "object" => {
                            let mut typed = Object::new();
                            for &field in CONVERSION {
                                typed.insert(
                                    field.into(),
                                    get(&row, &format!("conversion_{field}"))?.clone(),
                                );
                            }
                            view.insert(
                                "value_conversion".into(),
                                Value::Object(columns(
                                    &typed,
                                    CONVERSION,
                                    get(&row, "conversion_evidence")?,
                                )?),
                            );
                        }
                        Value::String(kind) if ["null", "invalid"].contains(&kind.as_str()) => {
                            view.insert(
                                "value_conversion".into(),
                                get(&row, "conversion_invalid_value")?.clone(),
                            );
                        }
                        _ => return Err(invalid("unknown order conversion kind")),
                    }
                }
                values.push(Value::Object(view));
            }
            Value::Array(values)
        } else if invalid_states.contains(&state) {
            get(&root, invalid_field)?.clone()
        } else {
            return Err(invalid("unknown collection kind"));
        };
        if trade {
            let target = match get(&root, "map_key")? {
                Value::Null => "/trades".into(),
                Value::String(key) => pointer(&pointer("/symbol_results", key), "trades"),
                _ => return Err(invalid("invalid trade map key")),
            };
            put(result, &target, value)?;
        } else {
            let parts = path.split('/').collect::<Vec<_>>();
            if !(path == "/orders"
                || parts.len() == 4 && parts[1] == "symbol_results" && parts[3] == "orders")
            {
                return Err(invalid("unsupported order collection path"));
            }
            put(result, path, value)?;
        }
    }
    if !trade && consumed != items.len() {
        return Err(invalid("orphan order facts"));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn find_preserves_missing_unique_and_duplicate_paths() {
        let mut rows: Rows = vec![
            json!({"path":null}),
            json!({"path":7}),
            json!({}),
            json!({"path":"/a"}),
        ]
        .into_iter()
        .map(|value| value.as_object().unwrap().clone())
        .collect();
        assert!(find(&rows, "path", "/missing").unwrap().is_none());
        assert_eq!(find(&rows, "path", "/a").unwrap(), rows.last());
        rows.push(rows.last().unwrap().clone());
        assert_eq!(
            find(&rows, "path", "/a").unwrap_err(),
            invalid("duplicate fact path")
        );
    }

    #[test]
    fn known_schema_preserves_text_json_and_booleans() {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE execution_input (id TEXT, algorithm JSON, audit_evidence JSON);
             INSERT INTO execution_input VALUES ('input', '{\"name\":true}', '{}');
             CREATE TABLE execution_symbol_result (
                 record_id INTEGER, algorithm TEXT, memory JSON, success BOOLEAN,
                 sizing_recorded BOOLEAN, tick_recorded BOOLEAN
             );
             INSERT INTO execution_symbol_result VALUES (1, '{\"name\":true}', '[false]', 0, 1, -2);
             CREATE TABLE execution_input_symbol (input_id TEXT, value TEXT);
             INSERT INTO execution_input_symbol VALUES ('input', '[false]');",
        )
        .unwrap();
        let inputs = load(&conn, "execution_input", "id", &json!("input")).unwrap();
        assert_eq!(one(&inputs).unwrap()["algorithm"], json!({"name":true}));
        let symbols = load(&conn, "execution_symbol_result", "record_id", &json!(1)).unwrap();
        let symbol = one(&symbols).unwrap();
        assert_eq!(symbol["algorithm"], json!("{\"name\":true}"));
        assert_eq!(symbol["memory"], json!([false]));
        assert_eq!(symbol["success"], false);
        assert_eq!(symbol["sizing_recorded"], true);
        assert_eq!(symbol["tick_recorded"], true);
        let names = load(&conn, "execution_input_symbol", "input_id", &json!("input")).unwrap();
        assert_eq!(one(&names).unwrap()["value"], json!("[false]"));
        conn.execute("UPDATE execution_input SET algorithm='not JSON'", [])
            .unwrap();
        assert!(load(&conn, "execution_input", "id", &json!("input"))
            .unwrap_err()
            .starts_with("INVALID_DATA:"));
    }
    #[test]
    fn python_float_wire_and_unicode() {
        for (source, expected) in [
            ("1e-5", "1e-05"),
            ("0.0001", "0.0001"),
            ("1e16", "1e+16"),
            ("1e15", "1000000000000000.0"),
            ("1.0", "1.0"),
            ("-0.0", "-0.0"),
            ("1.234e-7", "1.234e-07"),
        ] {
            let value: Value = serde_json::from_str(source).unwrap();
            assert_eq!(wire(&value).unwrap(), expected);
        }
        assert_eq!(wire(&json!({"汉字":"好"})).unwrap(), "{\"汉字\":\"好\"}");
    }
    #[test]
    fn sparse_columns_and_collection_integrity() {
        let row = object(&json!({"a":null,"b":0,"audit_evidence":{"invalid_fields":{"a":"bad"}}}))
            .unwrap()
            .clone();
        let view = fields(&row, &["a", "b"]).unwrap();
        assert!(!view.contains_key("a"));
        assert_eq!(view["b"], 0);
        assert!(view.contains_key("audit_evidence"));
        assert_eq!(fields(&row, &["a"]).unwrap()["a"], Value::Null);
        assert!(ordered(
            vec![object(&json!({"ordinal":1})).unwrap().clone()],
            &json!(1)
        )
        .is_err());
        assert!(reorder(
            object(&json!({"a":1,"b":2})).unwrap().clone(),
            &json!(["a", "a"])
        )
        .is_err());
    }
    #[test]
    fn merge_rejects_conflicts_and_pointer_escapes() {
        let mut result = Object::new();
        put(&mut result, "/symbol_results/a~1b~0c/orders", json!([])).unwrap();
        assert_eq!(result["symbol_results"]["a/b~c"]["orders"], json!([]));
        assert!(put(&mut result, "/symbol_results/a~1b~0c/orders", Value::Null).is_err());
    }
}
