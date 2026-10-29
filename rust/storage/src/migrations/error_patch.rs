//! 私有错误码修补与字段恢复；调用方提供码表和事务。

use std::collections::BTreeSet;

use rusqlite::{params, types::Value as SqlValue, Connection, OptionalExtension, Row};
use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};

use crate::error::AnalysisError;

type Result<T> = std::result::Result<T, AnalysisError>;

fn error(message: impl Into<String>) -> AnalysisError {
    AnalysisError::new("MIGRATION_ERROR", message)
}

fn db(error: rusqlite::Error) -> AnalysisError {
    self::error(error.to_string())
}

fn sql(connection: &Connection, statement: &str) -> Result<()> {
    connection.execute_batch(statement).map_err(db)
}

fn query_rows<T>(
    connection: &Connection,
    statement: &str,
    parameters: impl rusqlite::Params,
    map: impl FnMut(&Row<'_>) -> rusqlite::Result<T>,
) -> Result<Vec<T>> {
    let mut query = connection.prepare(statement).map_err(db)?;
    let rows = query.query_map(parameters, map).map_err(db)?;
    rows.collect::<std::result::Result<_, _>>().map_err(db)
}

fn identifier(value: &str) -> Result<String> {
    if value.is_empty()
        || !value
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || c == b'_')
    {
        return Err(error("invalid migration identifier"));
    }
    Ok(format!("\"{value}\""))
}

fn table_exists(connection: &Connection, name: &str) -> Result<bool> {
    connection
        .query_row(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?1",
            [name],
            |_| Ok(()),
        )
        .optional()
        .map(|row| row.is_some())
        .map_err(db)
}

fn value(row: &Row<'_>, column: &str) -> rusqlite::Result<Value> {
    Ok(match row.get::<_, SqlValue>(column)? {
        SqlValue::Null => Value::Null,
        SqlValue::Integer(value) => value.into(),
        SqlValue::Real(value) => serde_json::json!(value),
        SqlValue::Text(value) => Value::String(value),
        SqlValue::Blob(_) => Value::Null,
    })
}

fn decode(value: &Value) -> Result<Value> {
    match value {
        Value::String(value) => crate::json_codec::parse(value).map_err(error),
        _ => Ok(value.clone()),
    }
}

fn document(value: &Value) -> Option<Value> {
    decode(value).ok().filter(Value::is_object)
}

fn encoded(value: &Value) -> Result<String> {
    serde_json::to_string(value).map_err(|err| error(err.to_string()))
}

fn same_json(left: &Value, right: &Value) -> bool {
    if left == right {
        return true;
    }
    match (left, right) {
        (Value::Number(left), Value::Number(right)) => {
            if left.is_f64() || right.is_f64() {
                let (float, integer) = if left.is_f64() {
                    (left, right)
                } else {
                    (right, left)
                };
                let float = float.as_f64().unwrap();
                if let Some(integer) = integer.as_i64() {
                    float >= i64::MIN as f64
                        && float < -(i64::MIN as f64)
                        && float.fract() == 0.0
                        && float as i64 == integer
                } else if let Some(integer) = integer.as_u64() {
                    float >= 0.0
                        && float < 18_446_744_073_709_551_616.0
                        && float.fract() == 0.0
                        && float as u64 == integer
                } else {
                    left.as_f64() == right.as_f64()
                }
            } else {
                false
            }
        }
        (Value::Bool(left), Value::Number(right)) | (Value::Number(right), Value::Bool(left)) => {
            right.as_f64() == Some(if *left { 1.0 } else { 0.0 })
        }
        (Value::Array(left), Value::Array(right)) => {
            left.len() == right.len()
                && left
                    .iter()
                    .zip(right)
                    .all(|(left, right)| same_json(left, right))
        }
        (Value::Object(left), Value::Object(right)) => {
            left.len() == right.len()
                && left
                    .iter()
                    .all(|(key, left)| right.get(key).is_some_and(|right| same_json(left, right)))
        }
        _ => false,
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub(crate) struct Change {
    pub path: Vec<Value>,
    pub before_present: bool,
    pub before: Value,
    pub after_present: bool,
    pub after: Value,
    pub rule: String,
}

fn text(value: Option<&Value>) -> Option<&str> {
    value
        .and_then(Value::as_str)
        .filter(|value| !value.trim().is_empty())
}

fn technical(value: Option<&Value>) -> bool {
    let Some(value) = text(value) else {
        return false;
    };
    let number = value.strip_prefix('-').unwrap_or(value);
    (!number.is_empty() && number.chars().all(|c| c.is_ascii_digit()))
        || value.contains("Traceback")
        || value.contains("ErrorID=")
        || value.contains("return_code=")
        || value == "gm_authentication_error"
        || (value.starts_with(|c: char| c.is_ascii_uppercase())
            && value
                .bytes()
                .all(|c| c.is_ascii_uppercase() || c.is_ascii_digit() || c == b'_' || c == b'.'))
        || ["Error", "Exception"].iter().any(|suffix| {
            value.match_indices(suffix).any(|(offset, _)| {
                let is_word = |c: char| c.is_alphanumeric() || c == '_';
                if value[offset + suffix.len()..]
                    .chars()
                    .next()
                    .is_some_and(is_word)
                {
                    return false;
                }
                value[..offset]
                    .chars()
                    .rev()
                    .take_while(|c| is_word(*c) || *c == '.')
                    .any(is_word)
            })
        })
}

fn channel<'a>(documents: impl Iterator<Item = &'a Map<String, Value>>) -> Option<String> {
    let mut found = BTreeSet::new();
    for document in documents {
        for value in [
            document.get("channel_type"),
            document.get("channel"),
            document
                .get("extra")
                .and_then(|extra| extra.get("channel_type")),
        ] {
            if let Some(value) = text(value) {
                found.insert(value.trim().to_uppercase());
            }
        }
    }
    if found.len() == 1 {
        found.pop_first()
    } else {
        None
    }
}

fn code(document: &Map<String, Value>, codes: &Value) -> Option<String> {
    let mut found = BTreeSet::new();
    if let Some(error) = text(document.get("error")) {
        if codes
            .as_object()
            .is_some_and(|codes| codes.values().any(|items| items.get(error).is_some()))
        {
            found.insert(error.to_owned());
        }
    }
    for key in [
        "reason_code",
        "native_code",
        "error_code",
        "ErrorID",
        "status_code",
        "code",
    ] {
        match document.get(key) {
            Some(Value::String(value)) => {
                found.insert(value.clone());
            }
            Some(Value::Number(value)) if value.is_i64() || value.is_u64() => {
                found.insert(value.to_string());
            }
            _ => {}
        }
    }
    if document.get("error_type") == Some(&json!("gm_authentication_error")) {
        found.insert("gm_authentication_error".into());
    }
    if found.len() == 1 {
        found.pop_first()
    } else {
        None
    }
}

fn message(
    document: &Map<String, Value>,
    evidence: &[&Value],
    codes: &Value,
) -> Option<(String, String)> {
    let original = text(document.get("error"));
    if document
        .get("error")
        .is_some_and(|error| !error.is_null() && !error.is_string())
    {
        return None;
    }
    if original.is_some() && !technical(document.get("error")) {
        return None;
    }
    if let Some(reason) = text(document.get("outcome_reason")) {
        if !technical(document.get("outcome_reason")) && Some(reason) != original {
            return None;
        }
    }
    let channel = channel(
        evidence
            .iter()
            .filter_map(|value| value.as_object())
            .chain(std::iter::once(document)),
    )?;
    let code = code(document, codes)?;
    let message = codes.get(&channel)?.get(&code)?.as_str()?;
    if channel == "GM"
        && code == "1000"
        && !matches!(
            document.get("operation").and_then(Value::as_str),
            Some("authenticate" | "authentication" | "login")
        )
    {
        return None;
    }
    if channel == "CTP"
        && matches!(code.as_str(), "3" | "31")
        && !matches!(
            document.get("error_type").and_then(Value::as_str),
            Some("CtpRequestError" | "ctp_request_error")
        )
    {
        return None;
    }
    if channel == "TQ"
        && !matches!(
            document.get("operation").and_then(Value::as_str),
            Some("trading_time" | "trading_time_check" | "session_check")
        )
    {
        return None;
    }
    Some((message.into(), format!("{channel}:{code}")))
}

fn visit(
    value: &mut Value,
    path: &[Value],
    evidence: &[&Value],
    codes: &Value,
    changes: &mut Vec<Change>,
) {
    let Some(item) = value.as_object_mut() else {
        return;
    };
    if let Some((message, rule)) = message(item, evidence, codes) {
        let detail = text(item.get("error"))
            .filter(|_| !item.contains_key("technical_detail"))
            .map(|original| {
                (
                    "technical_detail",
                    json!(original),
                    "technical_detail".into(),
                )
            });
        for (key, after, rule) in
            detail
                .into_iter()
                .chain(std::iter::once(("error", json!(message), rule)))
        {
            let before = item.insert(key.into(), after.clone());
            let mut path = path.to_vec();
            path.push(json!(key));
            changes.push(Change {
                path,
                before_present: before.is_some(),
                before: before.unwrap_or_default(),
                after_present: true,
                after,
                rule,
            });
        }
    }
    if let Some(symbols) = item
        .get_mut("symbol_results")
        .and_then(Value::as_object_mut)
    {
        for (symbol, child) in symbols {
            let mut child_path = path.to_vec();
            child_path.extend([json!("symbol_results"), json!(symbol)]);
            visit(child, &child_path, evidence, codes, changes);
        }
    }
}

pub(crate) fn patch_document(
    document: &Value,
    evidence: &[Value],
    codes: &Value,
) -> Result<(Value, Vec<Change>)> {
    let mut copy = document.clone();
    let mut changes = Vec::new();
    if !copy.is_object() {
        return Ok((copy, changes));
    }
    let evidence: Vec<_> = evidence.iter().chain(std::iter::once(document)).collect();
    visit(&mut copy, &[], &evidence, codes, &mut changes);
    if let Some(rows) = copy
        .get_mut("reconciliation")
        .and_then(|value| value.get_mut("symbols"))
        .and_then(Value::as_array_mut)
    {
        for change in changes.clone() {
            if change.path.len() != 3
                || change.path[0] != "symbol_results"
                || change.path[2] != "error"
            {
                continue;
            }
            for (index, row) in rows.iter_mut().enumerate() {
                let Some(row) = row.as_object_mut() else {
                    continue;
                };
                if row.get("symbol") == Some(&change.path[1])
                    && change.before_present
                    && row.get("error") == Some(&change.before)
                {
                    changes.push(Change {
                        path: vec![
                            json!("reconciliation"),
                            json!("symbols"),
                            json!(index),
                            json!("error"),
                        ],
                        ..change.clone()
                    });
                    row.insert("error".into(), change.after.clone());
                }
            }
        }
    }
    Ok((copy, changes))
}

pub(crate) fn path_get(document: &Value, path: &[Value]) -> (bool, Value) {
    let mut value = document;
    for part in path {
        let child = if let Some(index) = part.as_u64() {
            value.as_array().and_then(|rows| rows.get(index as usize))
        } else {
            part.as_str().and_then(|key| value.get(key))
        };
        let Some(child) = child else {
            return (false, Value::Null);
        };
        value = child;
    }
    (true, value.clone())
}

pub(crate) fn path_set(
    document: &mut Value,
    path: &[Value],
    present: bool,
    replacement: &Value,
) -> Result<()> {
    let Some((last, parents)) = path.split_last() else {
        return Err(error("invalid migration JSON path"));
    };
    let mut value = document;
    for part in parents {
        value = if let Some(index) = part.as_u64() {
            value
                .as_array_mut()
                .and_then(|rows| rows.get_mut(index as usize))
        } else {
            part.as_str().and_then(|key| value.get_mut(key))
        }
        .ok_or_else(|| error("migration restore conflict: invalid JSON path"))?;
    }
    if let Some(key) = last.as_str() {
        let object = value
            .as_object_mut()
            .ok_or_else(|| error("migration restore conflict: invalid JSON path"))?;
        if present {
            object.insert(key.into(), replacement.clone());
        } else {
            object.remove(key);
        }
    } else if let Some(index) = last.as_u64() {
        let target = value
            .as_array_mut()
            .and_then(|rows| rows.get_mut(index as usize))
            .ok_or_else(|| error("migration restore conflict: invalid JSON path"))?;
        if !present {
            return Err(error(
                "migration restore conflict: cannot remove JSON array entry",
            ));
        }
        *target = replacement.clone();
    } else {
        return Err(error("invalid migration JSON path"));
    }
    Ok(())
}

pub(crate) fn invalidate(connection: &Connection, execution_id: Option<&str>) -> Result<()> {
    connection.execute("INSERT INTO account_analysis(account_id,source_version) SELECT DISTINCT account_id,1 FROM execution_event WHERE execution_id=?1 AND account_id IN(SELECT id FROM account) UNION SELECT account_id,1 FROM executerecord WHERE execution_id=?1 AND account_id IN(SELECT id FROM account) ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,requested=1,failures=0,retry_at=0,error=NULL",[execution_id]).map_err(db)?;
    Ok(())
}

fn identity(row: &Row<'_>) -> rusqlite::Result<Value> {
    let mut identity = Map::new();
    for key in ["execution_id", "created_at", "account_id", "artifact_type"] {
        if row.as_ref().column_index(key).is_ok() {
            identity.insert(key.into(), value(row, key)?);
        }
    }
    Ok(Value::Object(identity))
}

fn evidence(connection: &Connection, execution_id: Option<&str>) -> Result<Vec<Value>> {
    let Some(execution) = execution_id else {
        return Ok(Vec::new());
    };
    let mut output = Vec::new();
    for query in ["SELECT raw_input FROM executerecord WHERE execution_id=?1","SELECT raw_result FROM executerecord WHERE execution_id=?1","SELECT content FROM execution_artifact WHERE execution_id=?1 AND artifact_type IN('STANDARD_INPUT','EXECUTION_SUMMARY')"] {
        for row in query_rows(connection,query,[execution],|row|row.get::<_,Option<String>>(0))? {
            output.push(row.and_then(|value|crate::json_codec::parse(&value).ok()).unwrap_or_default());
        }
    }
    for channel in query_rows(
        connection,
        "SELECT DISTINCT channel FROM execution_event WHERE execution_id=?1",
        [execution],
        |row| row.get::<_, String>(0),
    )? {
        output.push(json!({"channel":channel}));
    }
    Ok(output)
}

pub(crate) fn patch_errors(
    connection: &Connection,
    backup_table: &str,
    codes: &Value,
    dry_run: bool,
) -> Result<Value> {
    let backup = identifier(backup_table)?;
    if !dry_run {
        sql(connection,&format!("CREATE TABLE {backup}(id INTEGER NOT NULL PRIMARY KEY,table_name TEXT NOT NULL,row_id TEXT NOT NULL,json_column TEXT NOT NULL,path JSON NOT NULL,before_present BOOLEAN NOT NULL,before_value JSON,after_present BOOLEAN NOT NULL,after_value JSON,rule TEXT NOT NULL,identity JSON NOT NULL)"))?;
    }
    let mut fields = 0_u64;
    let mut scan = Map::new();
    for (table, column, predicate) in [
        ("executerecord", "raw_result", "1"),
        (
            "execution_artifact",
            "content",
            "artifact_type='EXECUTION_SUMMARY'",
        ),
    ] {
        let mut last_id = -1_i64;
        loop {
            let rows = query_rows(
                connection,
                &format!("SELECT * FROM {table} WHERE {predicate} AND id>?1 ORDER BY id LIMIT 500"),
                [last_id],
                |row| {
                    Ok((
                        row.get::<_, i64>("id")?,
                        row.get::<_, Option<String>>("execution_id")?,
                        value(row, column)?,
                        row.as_ref()
                            .column_index("raw_input")
                            .ok()
                            .map(|_| value(row, "raw_input"))
                            .transpose()?,
                        identity(row)?,
                    ))
                },
            )?;
            if rows.is_empty() {
                break;
            }
            for (id, execution, raw, input, identity) in rows {
                last_id = id;
                let original = decode(&raw).unwrap_or_default();
                let scanned = scan
                    .get("scanned_rows")
                    .and_then(Value::as_u64)
                    .unwrap_or(0)
                    + 1;
                scan.insert("scanned_rows".into(), json!(scanned));
                let mut channels = vec![input
                    .and_then(|value| decode(&value).ok())
                    .unwrap_or_default()];
                channels.extend(evidence(connection, execution.as_deref())?);
                let (patched, changes) = patch_document(&original, &channels, codes)?;
                if changes.is_empty() {
                    let key = if original.is_object() {
                        "unchanged_rows"
                    } else {
                        "invalid_shape_rows"
                    };
                    let count = scan.get(key).and_then(Value::as_u64).unwrap_or(0) + 1;
                    scan.insert(key.into(), json!(count));
                    continue;
                }
                fields += changes.len() as u64;
                if dry_run {
                    continue;
                }
                for change in changes {
                    connection.execute(&format!("INSERT INTO {backup}(table_name,row_id,json_column,path,before_present,before_value,after_present,after_value,rule,identity) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10)"),params![table,id.to_string(),column,encoded(&json!(change.path))?,change.before_present,encoded(&change.before)?,change.after_present,encoded(&change.after)?,change.rule,encoded(&identity)?]).map_err(db)?;
                }
                connection
                    .execute(
                        &format!("UPDATE {table} SET {column}=?1 WHERE id=?2"),
                        params![encoded(&patched)?, id],
                    )
                    .map_err(db)?;
                if table == "execution_artifact" {
                    invalidate(connection, execution.as_deref())?;
                }
            }
        }
    }
    Ok(json!({"fields":fields,"scan":scan}))
}

struct Archived {
    table: String,
    row_id: String,
    column: String,
    path: Vec<Value>,
    before_present: bool,
    before: Value,
    after_present: bool,
    after: Value,
    identity: Value,
}

pub(crate) fn restore_errors(
    connection: &Connection,
    backup_table: &str,
    dry_run: bool,
) -> Result<()> {
    if !table_exists(connection, backup_table)? {
        return Err(error(format!("migration backup missing: {backup_table}")));
    }
    let backup = identifier(backup_table)?;
    let rows = query_rows(
        connection,
        &format!("SELECT * FROM {backup} ORDER BY table_name,row_id,json_column,id"),
        [],
        |row| {
            Ok(Archived {
                table: row.get("table_name")?,
                row_id: row.get("row_id")?,
                column: row.get("json_column")?,
                path: serde_json::from_str(&row.get::<_, String>("path")?).map_err(|error| {
                    rusqlite::Error::FromSqlConversionFailure(
                        0,
                        rusqlite::types::Type::Text,
                        Box::new(error),
                    )
                })?,
                before_present: row.get("before_present")?,
                before: value(row, "before_value")?,
                after_present: row.get("after_present")?,
                after: value(row, "after_value")?,
                identity: value(row, "identity")?,
            })
        },
    )?;
    let mut remaining = rows.as_slice();
    while let Some(first) = remaining.first() {
        let end = remaining.partition_point(|row| {
            row.table == first.table && row.row_id == first.row_id && row.column == first.column
        });
        let (entries, rest) = remaining.split_at(end);
        remaining = rest;
        let table = identifier(&first.table)?;
        let column = identifier(&first.column)?;
        let current = connection
            .query_row(
                &format!("SELECT * FROM {table} WHERE id=?1"),
                [&first.row_id],
                |row| {
                    Ok((
                        value(row, &first.column)?,
                        identity(row)?,
                        row.get::<_, Option<String>>("execution_id")?,
                    ))
                },
            )
            .optional()
            .map_err(db)?;
        let conflict = || {
            error(format!(
                "migration restore conflict: {}:{}",
                first.table, first.row_id
            ))
        };
        let Some((current, current_identity, execution)) = current else {
            return Err(conflict());
        };
        let expected_identity = decode(&first.identity)?;
        if expected_identity.as_object().map_or(true, |expected| {
            expected.iter().any(|(key, value)| {
                !current_identity
                    .get(key)
                    .is_some_and(|current| same_json(current, value))
            })
        }) {
            return Err(conflict());
        }
        let mut document = document(&current).ok_or_else(conflict)?;
        for entry in entries {
            let after = decode(&entry.after)?;
            let (present, current) = path_get(&document, &entry.path);
            if present != entry.after_present || !same_json(&current, &after) {
                return Err(error(format!(
                    "migration restore conflict: {}:{}:{}",
                    entry.table,
                    entry.row_id,
                    encoded(&json!(entry.path))?
                )));
            }
        }
        if !dry_run {
            for entry in entries.iter().rev() {
                path_set(
                    &mut document,
                    &entry.path,
                    entry.before_present,
                    &decode(&entry.before)?,
                )?;
            }
            connection
                .execute(
                    &format!("UPDATE {table} SET {column}=?1 WHERE id=?2"),
                    params![encoded(&document)?, first.row_id],
                )
                .map_err(db)?;
            if first.table == "execution_artifact" {
                invalidate(connection, execution.as_deref())?;
            }
        }
    }
    if !dry_run {
        sql(connection, &format!("DROP TABLE {backup}"))?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn raw_patch_and_restore_preserve_historical_numbers_and_channel_evidence() {
        let connection = Connection::open_in_memory().unwrap();
        connection.execute_batch(
            "CREATE TABLE executerecord(id INTEGER PRIMARY KEY,execution_id TEXT,account_id INTEGER,created_at TEXT,raw_input JSON,raw_result JSON);
             CREATE TABLE execution_artifact(id INTEGER PRIMARY KEY,execution_id TEXT,artifact_type TEXT,content JSON,created_at TEXT);
             CREATE TABLE execution_event(execution_id TEXT,channel TEXT);"
        ).unwrap();
        let source = r#"{"error":"-2019","metrics":[NaN,Infinity,-Infinity,1e400],"huge":123456789012345678901234567890,"literal":{"invalid_number":"nan"},"text":"NaN Infinity"}"#;
        let evidence = r#"{"channel_type":"EXAMPLE_CHANNEL","metrics":[NaN,Infinity]}"#;
        connection
            .execute(
                "INSERT INTO executerecord VALUES(1,'frozen',1,'date','{}',?1)",
                [source],
            )
            .unwrap();
        connection
            .execute(
                "INSERT INTO execution_artifact VALUES(1,'frozen','STANDARD_INPUT',?1,'date')",
                [evidence],
            )
            .unwrap();
        let read = || {
            let text: String = connection
                .query_row(
                    "SELECT raw_result FROM executerecord WHERE id=1",
                    [],
                    |row| row.get(0),
                )
                .unwrap();
            crate::json_codec::parse(&text).unwrap()
        };
        let original = read();
        let codes = json!({"EXAMPLE_CHANNEL":{"-2019":"保证金不足"}});
        assert_eq!(
            patch_errors(&connection, "private_error_backup", &codes, false).unwrap()["fields"],
            2
        );
        let patched = read();
        assert_eq!(patched["error"], "保证金不足");
        assert_eq!(patched["technical_detail"], "-2019");
        for key in ["metrics", "huge", "literal", "text"] {
            assert_eq!(patched[key], original[key]);
        }
        restore_errors(&connection, "private_error_backup", true).unwrap();
        assert_eq!(read(), patched);
        restore_errors(&connection, "private_error_backup", false).unwrap();
        assert_eq!(read(), original);
        assert!(connection
            .prepare("SELECT * FROM private_error_backup")
            .is_err());
    }

    #[test]
    fn invalidation_uses_fields_shared_by_0033_and_incremental_analysis() {
        let connection = Connection::open_in_memory().unwrap();
        connection.execute_batch(
            "CREATE TABLE account(id INTEGER PRIMARY KEY);
             CREATE TABLE account_analysis(account_id INTEGER PRIMARY KEY,source_version INTEGER,requested INTEGER,failures INTEGER,retry_at REAL,error TEXT);
             CREATE TABLE execution_event(execution_id TEXT,account_id INTEGER);
             CREATE TABLE executerecord(execution_id TEXT,account_id INTEGER);
             INSERT INTO account VALUES(1);
             INSERT INTO account_analysis VALUES(1,4,0,2,8,'failure');
             INSERT INTO executerecord VALUES('patched',1);"
        ).unwrap();
        invalidate(&connection, Some("patched")).unwrap();
        let state = connection.query_row(
            "SELECT source_version,requested,failures,retry_at,error FROM account_analysis WHERE account_id=1",
            [],
            |row| Ok((row.get::<_, i64>(0)?,row.get::<_, i64>(1)?,row.get::<_, i64>(2)?,row.get::<_, f64>(3)?,row.get::<_, Option<String>>(4)?)),
        ).unwrap();
        assert_eq!(state, (5, 1, 0, 0.0, None));
    }

    #[test]
    fn private_patch_requires_channel_agreement_and_updates_mirror() {
        let codes = json!({"EXAMPLE_CHANNEL":{"-2019":"保证金不足"}});
        let raw = json!({"channel":"EXAMPLE_CHANNEL","symbol_results":{"ag":{"error":"-2019"}},"reconciliation":{"symbols":[{"symbol":"ag","error":"-2019"}]}});
        let (after, changes) = patch_document(&raw, &[], &codes).unwrap();
        assert_eq!(after["symbol_results"]["ag"]["error"], "保证金不足");
        assert_eq!(after["reconciliation"]["symbols"][0]["error"], "保证金不足");
        assert_eq!(changes.len(), 3);
        let (unchanged, changes) =
            patch_document(&raw, &[json!({"channel":"TQ"})], &codes).unwrap();
        assert_eq!(unchanged, raw);
        assert!(changes.is_empty());
    }

    #[test]
    fn patch_restore_roundtrip_preserves_absence_null_and_business_reason() {
        let codes = json!({"EXAMPLE_CHANNEL":{"-2019":"保证金不足"}});
        let raw = json!({"channel":"EXAMPLE_CHANNEL","reason_code":"-2019","error":null});
        let (mut after, changes) = patch_document(&raw, &[], &codes).unwrap();
        for change in changes.iter().rev() {
            assert_eq!(
                path_get(&after, &change.path),
                (change.after_present, change.after.clone())
            );
            path_set(
                &mut after,
                &change.path,
                change.before_present,
                &change.before,
            )
            .unwrap();
        }
        assert_eq!(after, raw);
        let raw = json!({"channel":"EXAMPLE_CHANNEL","error":"-2019","outcome_reason":"保留原业务说明"});
        assert!(patch_document(&raw, &[], &codes).unwrap().1.is_empty());
    }
}
