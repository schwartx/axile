//! 监控的持久化输入、迟到结果、异常会话及额度预警。

use std::collections::BTreeSet;
use std::path::Path;

use chrono::{DateTime, Datelike, SecondsFormat, Utc};
use chrono_tz::Tz;
use rusqlite::{params, Connection, OptionalExtension};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use crate::control_audit::{event, EVENT_COLUMNS};
use crate::error::AnalysisError;

const WARNING_EVENT: &str = "account_control.warning";

fn database(error: rusqlite::Error) -> AnalysisError {
    AnalysisError::new("CONTROL_MONITOR", error.to_string())
}

fn invalid(message: impl Into<String>) -> AnalysisError {
    AnalysisError::new("INVALID_REQUEST", message)
}

fn integer(value: &Value, key: &str) -> Result<i64, AnalysisError> {
    value[key]
        .as_i64()
        .ok_or_else(|| invalid(format!("{key} 必须是整数")))
}

fn text<'a>(value: &'a Value, key: &str) -> Result<&'a str, AnalysisError> {
    value[key]
        .as_str()
        .ok_or_else(|| invalid(format!("{key} 必须是字符串")))
}

fn sessions_on(
    connection: &Connection,
    account_id: Option<i64>,
) -> Result<Vec<Value>, AnalysisError> {
    let mut statement = connection.prepare(
        "SELECT id,instance_id,account_id,execution_id,context_kind,source,timezone,started_at_ms,finished_at_ms,status
         FROM account_control_session WHERE (?1 IS NULL OR account_id=?1) ORDER BY id"
    ).map_err(database)?;
    let rows = statement
        .query_map([account_id], |row| {
            let execution_id: String = row.get(3)?;
            let kind: String = row.get(4)?;
            Ok(json!({
                "id":row.get::<_,i64>(0)?,"instance_id":row.get::<_,String>(1)?,
                "account_id":row.get::<_,i64>(2)?,"execution_id":execution_id,
                "context_kind":kind,"source":row.get::<_,String>(5)?,
                "effective_context_kind":crate::control_audit::context_kind(&kind,&execution_id),
                "timezone":row.get::<_,String>(6)?,"started_at_ms":row.get::<_,i64>(7)?,
                "finished_at_ms":row.get::<_,Option<i64>>(8)?,"status":row.get::<_,String>(9)?
            }))
        })
        .map_err(database)?;
    rows.collect::<Result<Vec<_>, _>>().map_err(database)
}

pub fn control_monitor_inputs(path: &str, _request: &Value) -> Result<Value, AnalysisError> {
    let connection = crate::storage_connection::open_reader(Path::new(path))?;
    connection
        .execute_batch("BEGIN DEFERRED")
        .map_err(database)?;
    let mut statement = connection
        .prepare(
            "SELECT a.id,a.trade_channel,
            coalesce(json_extract(s.execution,'$.account_control_preset'),'default'),
            json_extract(s.execution,'$.account_control_override')
         FROM account a LEFT JOIN account_settings s ON s.account_id=a.id ORDER BY a.id",
        )
        .map_err(database)?;
    let mut rows = statement.query([]).map_err(database)?;
    let mut accounts = Vec::new();
    while let Some(row) = rows.next().map_err(database)? {
        let raw: Option<String> = row.get(3).map_err(database)?;
        let override_value: Value = raw
            .map(|raw| serde_json::from_str(&raw))
            .transpose()
            .map_err(|error| AnalysisError::new("INVALID_DATA", error.to_string()))?
            .unwrap_or(Value::Null);
        accounts.push(json!({
            "id":row.get::<_,i64>(0).map_err(database)?,
            "trade_channel":row.get::<_,String>(1).map_err(database)?,
            "account_control_preset":row.get::<_,String>(2).map_err(database)?,
            "account_control_override":override_value
        }));
    }
    Ok(json!({"accounts":accounts,"sessions":sessions_on(&connection,None)?}))
}

pub fn control_monitor_recover(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let now = integer(request, "now_ms")?;
    let connection = crate::writer_scope::open_writer(Path::new(path))?;
    crate::writer_scope::immediate(&connection, || {
        let updated = connection.execute(
            "UPDATE account_control_session SET status='incomplete',finished_at_ms=?1 WHERE status='active'",
            [now],
        ).map_err(database)?;
        Ok(json!({"updated":updated}))
    })
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct QuotaWarning {
    account_id: i64,
    execution_id: String,
    operation: String,
    control_date: String,
    timezone: String,
    occurred_at_ms: i64,
    used: i64,
    limit: i64,
    warning_percent: i64,
    threshold_count: i64,
    remaining: i64,
    #[serde(default = "warning_source")]
    source: String,
}

fn warning_source() -> String {
    "axile_local_allowed".to_owned()
}

impl QuotaWarning {
    fn context(&self, name: &str) -> Result<Value, AnalysisError> {
        if !matches!(self.operation.as_str(), "place_order" | "cancel_order")
            || self.used < 0
            || self.limit <= 0
            || !(1..=99).contains(&self.warning_percent)
            || self.threshold_count <= 0
            || self.remaining < 0
            || self.source != "axile_local_allowed"
        {
            return Err(invalid("无效额度预警"));
        }
        let timezone: Tz = self.timezone.parse().map_err(|_| invalid("无效预警时区"))?;
        let at = DateTime::<Utc>::from_timestamp_millis(self.occurred_at_ms)
            .ok_or_else(|| invalid("无效预警发生时间"))?
            .with_timezone(&timezone);
        if !(1..=9999).contains(&at.year()) {
            return Err(invalid("无效预警发生时间"));
        }
        let precision = if at.timestamp_subsec_micros() == 0 {
            SecondsFormat::Secs
        } else {
            SecondsFormat::Micros
        };
        let mut quota = serde_json::to_value(self).map_err(|error| invalid(error.to_string()))?;
        let quota = quota.as_object_mut().unwrap();
        for key in ["account_id", "execution_id", "occurred_at_ms"] {
            quota.remove(key);
        }
        Ok(json!({
            "event_id":self.event_id(),"event_type":WARNING_EVENT,"execution_id":self.execution_id,
            "occurred_at":at.to_rfc3339_opts(precision,false),
            "account":{"id":self.account_id,"name":name},"error":null,"quota":quota,"is_test":false
        }))
    }

    fn event_id(&self) -> String {
        format!(
            "account-control.warning:{}:{}:{}:{}",
            self.account_id, self.timezone, self.control_date, self.operation
        )
    }
}

pub(crate) fn enqueue_quota_warnings_on(
    connection: &Connection,
    warnings: &Value,
    created_at: &str,
) -> Result<Value, AnalysisError> {
    let warnings: Vec<QuotaWarning> =
        serde_json::from_value(warnings.clone()).map_err(|error| invalid(error.to_string()))?;
    let contexts = warnings
        .iter()
        .map(|warning| warning.context(""))
        .collect::<Result<Vec<_>, _>>()?;
    let mut queued = BTreeSet::new();
    for (warning, mut context) in warnings.iter().zip(contexts) {
        let name: Option<String> = connection
            .query_row(
                "SELECT name FROM account WHERE id=?1",
                [warning.account_id],
                |row| row.get(0),
            )
            .optional()
            .map_err(database)?;
        let Some(name) = name else { continue };
        context["account"]["name"] = json!(name);
        let inserted = connection
            .execute(
                "INSERT INTO notification_event(id,account_id,created_at,event_type,context,status)
             VALUES (?1,?2,?3,?4,?5,'pending') ON CONFLICT(id) DO NOTHING",
                params![
                    warning.event_id(),
                    warning.account_id,
                    created_at,
                    WARNING_EVENT,
                    context.to_string()
                ],
            )
            .map_err(database)?;
        if inserted != 0 {
            queued.insert(warning.account_id);
        }
    }
    Ok(json!({"queued_accounts":queued}))
}

fn pending_on(connection: &Connection) -> Result<Vec<i64>, AnalysisError> {
    let mut statement = connection.prepare(
        "SELECT DISTINCT account_id FROM notification_event WHERE event_type=?1 AND status='pending' ORDER BY account_id"
    ).map_err(database)?;
    let rows = statement
        .query_map([WARNING_EVENT], |row| row.get(0))
        .map_err(database)?;
    rows.collect::<Result<Vec<_>, _>>().map_err(database)
}

pub fn control_quota_pending(path: &str, _request: &Value) -> Result<Value, AnalysisError> {
    let connection = crate::storage_connection::open_reader(Path::new(path))?;
    Ok(json!({"account_ids":pending_on(&connection)?}))
}

fn history_on(connection: &Connection, request: &Value) -> Result<Value, AnalysisError> {
    let mut history = crate::control_store::load_baseline_on(connection, request)?;
    let mut statement = connection
        .prepare(&format!(
            "SELECT {EVENT_COLUMNS} FROM account_control_event WHERE account_id=?1 ORDER BY occurred_at_ms DESC,id DESC LIMIT 100"
        ))
        .map_err(database)?;
    let mut rows = statement
        .query([integer(request, "account_id")?])
        .map_err(database)?;
    let mut records = Vec::new();
    while let Some(row) = rows.next().map_err(database)? {
        let mut value = event(row)?;
        value.as_object_mut().unwrap().retain(|key, _| {
            matches!(
                key.as_str(),
                "event_uid"
                    | "occurred_at_ms"
                    | "operation"
                    | "symbol"
                    | "decision"
                    | "outcome"
                    | "metadata"
            )
        });
        records.push(value);
    }
    history["records"] = json!(records);
    Ok(history)
}

fn refresh_on(connection: &Connection, requests: &[Value]) -> Result<Value, AnalysisError> {
    let mut data = Vec::new();
    let mut updated_uids = Vec::new();
    let mut warnings = Vec::new();
    let mut warning_error: Option<&str> = None;
    for request in requests {
        let account_id = integer(request, "account_id")?;
        let now = integer(request, "now_ms")?;
        if let Some(instance_id) = request["mark_incomplete"].as_str() {
            connection
                .execute(
                    "UPDATE account_control_session SET status='incomplete',finished_at_ms=?1
                 WHERE account_id=?2 AND instance_id=?3 AND status='active'",
                    params![now, account_id, instance_id],
                )
                .map_err(database)?;
        }
        let sessions = sessions_on(connection, Some(account_id))?;
        let outcomes: Vec<Value> = request["outcomes"]
            .as_array()
            .ok_or_else(|| invalid("outcomes 必须是数组"))?
            .iter()
            .filter(|event| {
                event["event_uid"].as_str().is_some_and(|uid| {
                    sessions.iter().any(|session| {
                        session["status"] != "active"
                            && uid.contains(&format!(
                                ":{}:",
                                session["instance_id"].as_str().unwrap()
                            ))
                    })
                })
            })
            .map(|event| {
                let mut event = event.clone();
                event["account_id"] = json!(account_id);
                event
            })
            .collect();
        let updated = if outcomes.is_empty() {
            json!({"updated":0,"updated_uids":[]})
        } else {
            crate::control_store::update_event_outcomes_on(connection, &json!(outcomes))?
        };
        updated_uids.extend(updated["updated_uids"].as_array().unwrap().iter().cloned());
        let signature: Vec<Value> = sessions
            .iter()
            .map(|session| json!([session["instance_id"], session["status"]]))
            .collect();
        let history = if request["read_history"] == true
            || updated["updated"].as_u64().unwrap_or(0) != 0
            || request["history_sessions"] != json!(signature)
        {
            history_on(connection, request)?
        } else {
            Value::Null
        };
        let mut state = json!({
            "account_id":account_id,"sessions":sessions,"history":history,
            "incomplete":crate::control_store::incomplete_history_on(connection,request)?
        });
        if request["trade_channel"] == "ctp" {
            let start = integer(request, "start_ms")?;
            let end = integer(request, "end_ms")?;
            let day = text(request, "control_date")?;
            let timezone = text(request, "timezone")?;
            state["order_submissions"] =
                crate::submissions::summary(connection, account_id, start, end)
                    .map_err(AnalysisError::from)?;
            state["submission_monitor"] =
                crate::submissions::monitor(connection, account_id, day, timezone, start, end, now)
                    .map_err(AnalysisError::from)?;
        }
        for source in request["warning_sources"].as_array().into_iter().flatten() {
            if !sessions
                .iter()
                .any(|session| session["instance_id"] == source["instance_id"])
            {
                continue;
            }
            for warning in source["warnings"].as_array().into_iter().flatten() {
                if warning["account_id"].as_i64() != Some(account_id) {
                    warning_error = Some("额度预警与账户不匹配");
                }
                warnings.push(warning.clone());
            }
        }
        data.push(state);
    }
    Ok(
        json!({"data":data,"updated_uids":updated_uids,"warnings":warnings,"warning_error":warning_error}),
    )
}

pub fn control_monitor_refresh(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let requests = request["accounts"]
        .as_array()
        .ok_or_else(|| invalid("accounts 必须是数组"))?;
    let created_at = text(request, "created_at")?;
    let has_updates = requests.iter().any(|request| {
        request["mark_incomplete"].is_string()
            || request["outcomes"]
                .as_array()
                .is_some_and(|rows| !rows.is_empty())
    });
    let has_warnings = requests.iter().any(|request| {
        request["warning_sources"]
            .as_array()
            .is_some_and(|rows| !rows.is_empty())
    });
    let connection = if has_updates || has_warnings {
        crate::writer_scope::open_writer(Path::new(path))?
    } else {
        crate::storage_connection::open_reader(Path::new(path))?
    };
    let mut result = if has_updates {
        crate::writer_scope::immediate(&connection, || refresh_on(&connection, requests))?
    } else {
        connection
            .execute_batch("BEGIN DEFERRED")
            .map_err(database)?;
        let result = refresh_on(&connection, requests)?;
        connection.execute_batch("COMMIT").map_err(database)?;
        result
    };
    let warnings = result.as_object_mut().unwrap().remove("warnings").unwrap();
    result["warnings_committed"] = json!(result["warning_error"].is_null());
    if result["warnings_committed"] == true && !warnings.as_array().unwrap().is_empty() {
        if let Err(error) = crate::writer_scope::immediate(&connection, || {
            enqueue_quota_warnings_on(&connection, &warnings, created_at)
        }) {
            result["warnings_committed"] = json!(false);
            result["warning_error"] = json!(error.to_string());
        }
    }
    result["pending_accounts"] = json!(pending_on(&connection)?);
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn warning() -> Value {
        json!({"account_id":1,"execution_id":"exec","operation":"place_order",
            "control_date":"2026-10-23","timezone":"Asia/Shanghai","occurred_at_ms":1792713600123_i64,
            "used":4,"limit":5,"warning_percent":80,"threshold_count":4,"remaining":1})
    }

    fn connection() -> Connection {
        let connection = Connection::open_in_memory().unwrap();
        connection.execute_batch(
            "CREATE TABLE account(id INTEGER PRIMARY KEY,name TEXT);
             INSERT INTO account VALUES(1,'primary');
             CREATE TABLE notification_event(sequence INTEGER PRIMARY KEY,id TEXT UNIQUE,account_id INTEGER,
                created_at TEXT,event_type TEXT,context TEXT,status TEXT);"
        ).unwrap();
        connection
    }

    #[test]
    fn quota_preserves_frozen_context_deduplicates_and_skips_deleted_accounts() {
        let connection = connection();
        let first = warning();
        let mut changed = first.clone();
        changed["warning_percent"] = json!(99);
        changed["used"] = json!(5);
        let mut deleted = first.clone();
        deleted["account_id"] = json!(2);
        enqueue_quota_warnings_on(&connection, &json!([first, changed, deleted]), "created")
            .unwrap();
        let context: String = connection
            .query_row("SELECT context FROM notification_event", [], |row| {
                row.get(0)
            })
            .unwrap();
        let context: Value = serde_json::from_str(&context).unwrap();
        assert_eq!(context["account"], json!({"id":1,"name":"primary"}));
        assert_eq!(context["quota"]["warning_percent"], 80);
        assert_eq!(context["quota"]["source"], "axile_local_allowed");
        assert!(context["occurred_at"]
            .as_str()
            .unwrap()
            .ends_with(".123000+08:00"));
        assert_eq!(pending_on(&connection).unwrap(), vec![1]);
        let count: i64 = connection
            .query_row("SELECT count(*) FROM notification_event", [], |row| {
                row.get(0)
            })
            .unwrap();
        assert_eq!(count, 1);
    }

    #[test]
    fn quota_validates_entire_batch_before_writing() {
        let connection = connection();
        let mut invalid = warning();
        invalid["used"] = json!(true);
        assert!(
            enqueue_quota_warnings_on(&connection, &json!([warning(), invalid]), "created")
                .is_err()
        );
        assert!(pending_on(&connection).unwrap().is_empty());
        let mut invalid = warning();
        invalid["warning_percent"] = json!(100);
        assert!(
            enqueue_quota_warnings_on(&connection, &json!([warning(), invalid]), "created")
                .is_err()
        );
        assert!(pending_on(&connection).unwrap().is_empty());
    }
}
