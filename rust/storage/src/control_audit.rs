//! 账户控制审计查询；保留数据库枚举和公开响应的不同表示。

use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

use rusqlite::{params, Connection, Row};
use serde_json::{json, Value};

use crate::error::AnalysisError;

fn database(error: rusqlite::Error) -> AnalysisError {
    AnalysisError::new("DATABASE_READ", error.to_string())
}

fn required<'a>(request: &'a Value, key: &str) -> Result<&'a str, AnalysisError> {
    request[key]
        .as_str()
        .ok_or_else(|| AnalysisError::new("INVALID_REQUEST", format!("{key} 必须是字符串")))
}

fn pagination(request: &Value, default: i64) -> Result<(i64, i64), AnalysisError> {
    let skip = request.get("skip").map_or(Some(0), Value::as_i64);
    let limit = request.get("limit").map_or(Some(default), Value::as_i64);
    match (skip, limit) {
        (Some(skip), Some(limit)) if skip >= 0 && limit <= 500 => {
            Ok((skip, if limit <= 0 { default } else { limit }))
        }
        _ => Err(AnalysisError::new(
            "INVALID_REQUEST",
            "skip 必须大于等于 0，limit 不能超过 500",
        )),
    }
}

fn account_id(request: &Value) -> Result<i64, AnalysisError> {
    request["account_id"]
        .as_i64()
        .ok_or_else(|| AnalysisError::new("INVALID_REQUEST", "account_id 必须是整数"))
}

fn optional<'a>(request: &'a Value, key: &str) -> Result<Option<&'a str>, AnalysisError> {
    if request[key].is_null() {
        return Ok(None);
    }
    required(request, key).map(Some)
}

fn require_account(connection: &Connection, account_id: i64) -> Result<(), AnalysisError> {
    let exists: bool = connection
        .query_row(
            "SELECT EXISTS(SELECT 1 FROM account WHERE id=?1)",
            [account_id],
            |row| row.get(0),
        )
        .map_err(database)?;
    if exists {
        Ok(())
    } else {
        Err(AnalysisError::new("ACCOUNT_NOT_FOUND", "Account not found"))
    }
}

fn decision(value: &str) -> Result<&'static str, AnalysisError> {
    match value {
        "ALLOWED" => Ok("allowed"),
        "BLOCKED" => Ok("blocked"),
        _ => Err(AnalysisError::new(
            "INVALID_DATA",
            format!("无效账户控制 decision: {value}"),
        )),
    }
}

pub(crate) fn legacy_source(context_id: &str) -> Option<&'static str> {
    let (prefix, tail) = context_id.split_once('-')?;
    if tail.len() != 32
        || !tail
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    {
        return None;
    }
    match prefix {
        "orders" => Some("current_orders"),
        "catalog" => Some("instrument_catalog"),
        _ => None,
    }
}

pub(crate) fn context_kind(value: &str, context_id: &str) -> &'static str {
    match value {
        "execution" => "execution",
        "account_operation" => "account_operation",
        "unknown" if legacy_source(context_id).is_some() => "account_operation",
        _ => "unknown",
    }
}

pub fn control_audit_summary(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let connection = crate::storage_connection::open_reader(Path::new(path))?;
    connection
        .execute_batch("BEGIN DEFERRED")
        .map_err(database)?;
    let account_id = account_id(request)?;
    let day = required(request, "control_date")?;
    require_account(&connection, account_id)?;
    let mut statement = connection
        .prepare(
            "SELECT operation,decision,outcome,count(*) FROM account_control_event
             WHERE account_id=?1 AND control_date=?2
             GROUP BY operation,decision,outcome ORDER BY operation,decision,outcome",
        )
        .map_err(database)?;
    let mut rows = statement
        .query(params![account_id, day])
        .map_err(database)?;
    let mut result = Vec::new();
    while let Some(row) = rows.next().map_err(database)? {
        result.push(json!({
            "operation":row.get::<_,String>(0).map_err(database)?,
            "decision":decision(&row.get::<_,String>(1).map_err(database)?)?,
            "outcome":row.get::<_,String>(2).map_err(database)?,
            "count":row.get::<_,i64>(3).map_err(database)?
        }));
    }
    Ok(json!(result))
}

pub fn control_audit_executions(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let (skip, limit) = pagination(request, 100)?;
    let account_id = account_id(request)?;
    let day = required(request, "control_date")?;
    let connection = crate::storage_connection::open_reader(Path::new(path))?;
    connection
        .execute_batch("BEGIN DEFERRED")
        .map_err(database)?;
    require_account(&connection, account_id)?;
    let count: i64 = connection.query_row(
        "SELECT count(DISTINCT execution_id) FROM account_control_event WHERE account_id=?1 AND control_date=?2",
        params![account_id, day], |row| row.get(0),
    ).map_err(database)?;
    let mut statement = connection.prepare(
        "WITH page AS (
            SELECT execution_id,count(*) AS event_count,min(created_at_us) AS first_at,max(created_at_us) AS last_at
            FROM account_control_event WHERE account_id=?1 AND control_date=?2 GROUP BY execution_id
            ORDER BY last_at DESC,execution_id DESC LIMIT ?3 OFFSET ?4)
         SELECT DISTINCT p.execution_id,p.event_count,p.first_at,p.last_at,e.context_kind,e.source
         FROM page p JOIN account_control_event e ON e.execution_id=p.execution_id
            AND e.account_id=?1 AND e.control_date=?2
         ORDER BY p.last_at DESC,p.execution_id DESC"
    ).map_err(database)?;
    let mut rows = statement
        .query(params![account_id, day, limit, skip])
        .map_err(database)?;
    let mut data = Vec::<Value>::new();
    let mut descriptions = BTreeMap::<String, (BTreeSet<String>, BTreeSet<String>)>::new();
    while let Some(row) = rows.next().map_err(database)? {
        let id: String = row.get(0).map_err(database)?;
        let kind: String = row.get(4).map_err(database)?;
        let source: String = row.get(5).map_err(database)?;
        if !descriptions.contains_key(&id) {
            data.push(json!({
                "execution_id":id,"event_count":row.get::<_,i64>(1).map_err(database)?,
                "first_event_at":crate::time_us::read(row, 2).map_err(database)?,
                "last_event_at":crate::time_us::read(row, 3).map_err(database)?
            }));
        }
        let (kinds, sources) = descriptions.entry(id.clone()).or_default();
        kinds.insert(context_kind(&kind, &id).to_owned());
        sources.insert(source);
    }
    for entry in &mut data {
        let (kinds, sources) = &descriptions[entry["execution_id"].as_str().unwrap()];
        entry["context_kind"] = json!(if kinds.len() == 1 {
            kinds.first().unwrap().as_str()
        } else {
            "unknown"
        });
        entry["sources"] = json!(sources);
    }
    Ok(json!({"data":data,"count":count}))
}

pub(crate) const EVENT_COLUMNS: &str = "id,account_id,control_date,execution_id,context_kind,source,seq,channel,operation,symbol,metadata,decision,counted,outcome,event_uid,created_at_us,occurred_at_us";

pub(crate) fn event(row: &Row<'_>) -> Result<Value, AnalysisError> {
    let metadata: String = row.get(10).map_err(database)?;
    let metadata: Value = serde_json::from_str(&metadata)
        .map_err(|error| AnalysisError::new("INVALID_DATA", error.to_string()))?;
    if !metadata.is_object() {
        return Err(AnalysisError::new(
            "INVALID_DATA",
            "控制事件 metadata 必须是对象",
        ));
    }
    Ok(json!({
        "id":row.get::<_,i64>(0).map_err(database)?,
        "account_id":row.get::<_,i64>(1).map_err(database)?,
        "control_date":row.get::<_,String>(2).map_err(database)?,
        "execution_id":row.get::<_,String>(3).map_err(database)?,
        "context_kind":row.get::<_,String>(4).map_err(database)?,
        "source":row.get::<_,String>(5).map_err(database)?,
        "seq":row.get::<_,i64>(6).map_err(database)?,
        "channel":row.get::<_,String>(7).map_err(database)?,
        "operation":row.get::<_,String>(8).map_err(database)?,
        "symbol":row.get::<_,Option<String>>(9).map_err(database)?,
        "metadata":metadata,"decision":decision(&row.get::<_,String>(11).map_err(database)?)?,
        "counted":row.get::<_,bool>(12).map_err(database)?,
        "outcome":row.get::<_,String>(13).map_err(database)?,
        "event_uid":row.get::<_,String>(14).map_err(database)?,
        "created_at":crate::time_us::read(row, 15).map_err(database)?,
        "occurred_at_ms":row.get::<_,i64>(16).map_err(database)?.div_euclid(1_000)
    }))
}

pub fn control_audit_events(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let (skip, limit) = pagination(request, 200)?;
    let execution_id = required(request, "execution_id")?;
    let symbol = optional(request, "symbol")?;
    let operation = optional(request, "operation")?;
    let connection = crate::storage_connection::open_reader(Path::new(path))?;
    connection
        .execute_batch("BEGIN DEFERRED")
        .map_err(database)?;
    let predicate =
        "execution_id=?1 AND (?2 IS NULL OR symbol=?2) AND (?3 IS NULL OR operation=?3)";
    let count: i64 = connection
        .query_row(
            &format!("SELECT count(*) FROM account_control_event WHERE {predicate}"),
            params![execution_id, symbol, operation],
            |row| row.get(0),
        )
        .map_err(database)?;
    let mut statement = connection
        .prepare(&format!(
            "SELECT {EVENT_COLUMNS} FROM account_control_event WHERE {predicate} ORDER BY seq,id LIMIT ?4 OFFSET ?5"
        ))
        .map_err(database)?;
    let mut rows = statement
        .query(params![execution_id, symbol, operation, limit, skip])
        .map_err(database)?;
    let mut data = Vec::new();
    while let Some(row) = rows.next().map_err(database)? {
        data.push(event(row)?);
    }
    Ok(json!({"data":data,"count":count}))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn legacy_context_only_accepts_exact_reserved_id() {
        for (id, source) in [
            (format!("orders-{}", "a".repeat(32)), Some("current_orders")),
            (
                format!("catalog-{}", "0".repeat(32)),
                Some("instrument_catalog"),
            ),
            (format!("catalog-{}", "a".repeat(33)), None),
            (format!("orders-{}", "A".repeat(32)), None),
            (format!("orders-{}", "a".repeat(31)), None),
            (format!("orders-{}\n", "a".repeat(32)), None),
            (format!("plugin-{}", "a".repeat(32)), None),
            ("orders-deadbeef".into(), None),
        ] {
            assert_eq!(legacy_source(&id), source, "{id:?}");
            assert_eq!(
                context_kind("unknown", &id),
                if source.is_some() {
                    "account_operation"
                } else {
                    "unknown"
                },
                "{id:?}"
            );
            assert_eq!(context_kind("execution", &id), "execution");
            assert_eq!(context_kind("account_operation", &id), "account_operation");
            assert_eq!(context_kind("invalid", &id), "unknown");
        }
    }

    #[test]
    fn pagination_preserves_defaults_and_enforces_limits() {
        assert_eq!(pagination(&json!({"limit":0}), 100).unwrap(), (0, 100));
        assert!(pagination(&json!({"skip":-1}), 100).is_err());
        assert!(pagination(&json!({"limit":501}), 100).is_err());
        assert_eq!(decision("BLOCKED").unwrap(), "blocked");
        assert!(decision("blocked").is_err());
    }
}
