//! 账户控制基线、会话和事实的持久化边界。

use std::path::Path;

use rusqlite::{params, Connection};
use serde_json::{json, Value};

use crate::control_audit::context_kind;
use crate::error::AnalysisError;
use crate::{storage_connection, writer_scope};

fn invalid(field: &str) -> AnalysisError {
    AnalysisError::new("ACCOUNT_CONTROL", format!("账户控制字段无效: {field}"))
}

fn sql(error: rusqlite::Error) -> AnalysisError {
    AnalysisError::new("ACCOUNT_CONTROL_SQL", error.to_string())
}

fn text<'a>(row: &'a Value, field: &str) -> Result<&'a str, AnalysisError> {
    row.get(field)
        .and_then(Value::as_str)
        .ok_or_else(|| invalid(field))
}

fn number(row: &Value, field: &str) -> Result<i64, AnalysisError> {
    row.get(field)
        .and_then(Value::as_i64)
        .ok_or_else(|| invalid(field))
}

fn optional_text<'a>(row: &'a Value, field: &str) -> Result<Option<&'a str>, AnalysisError> {
    match row.get(field) {
        None | Some(Value::Null) => Ok(None),
        Some(value) => value.as_str().map(Some).ok_or_else(|| invalid(field)),
    }
}

fn array<'a>(row: &'a Value, field: &str) -> Result<&'a [Value], AnalysisError> {
    row.get(field)
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .ok_or_else(|| invalid(field))
}

fn stored_enum<'a>(
    row: &Value,
    field: &str,
    choices: &[(&str, &'a str)],
) -> Result<&'a str, AnalysisError> {
    let value = text(row, field)?;
    choices
        .iter()
        .find_map(|(public, stored)| (*public == value).then_some(*stored))
        .ok_or_else(|| invalid(field))
}

fn metadata(row: &Value) -> Result<String, AnalysisError> {
    row.get("metadata")
        .filter(|value| value.is_object())
        .map(Value::to_string)
        .ok_or_else(|| invalid("metadata"))
}

/// 在同一只读快照内加载本次 guard 的全部历史输入。
pub fn load_baseline(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let connection = storage_connection::open_reader(Path::new(path))?;
    connection.execute_batch("BEGIN DEFERRED").map_err(sql)?;
    let result = load_baseline_on(&connection, request)?;
    connection.execute_batch("COMMIT").map_err(sql)?;
    Ok(result)
}

/// 复用调用方连接；不打开连接或持有独立事务。
pub(crate) fn load_baseline_on(
    connection: &Connection,
    request: &Value,
) -> Result<Value, AnalysisError> {
    let account_id = number(request, "account_id")?;
    let control_date = text(request, "control_date")?;
    let mut statement = connection
        .prepare(
            "SELECT bucket_type,bucket_start,operation,
                    CASE WHEN scope_type='SYMBOL' THEN symbol ELSE NULL END AS scope_symbol,
                    SUM(delta_count)
             FROM account_control_counter_delta
             WHERE account_id=?1 AND control_date=?2
             GROUP BY bucket_type,bucket_start,operation,scope_symbol",
        )
        .map_err(sql)?;
    let counters = statement
        .query_map(params![account_id, control_date], |row| {
            Ok(json!({
                "bucket_type": row.get::<_, String>(0)?.to_ascii_lowercase(),
                "bucket_start": row.get::<_, String>(1)?,
                "operation": row.get::<_, String>(2)?,
                "symbol": row.get::<_, Option<String>>(3)?,
                "delta_count": row.get::<_, i64>(4)?,
            }))
        })
        .map_err(sql)?
        .collect::<Result<Vec<_>, _>>()
        .map_err(sql)?;
    let mut statement = connection
        .prepare(
            "SELECT operation,NULL,MAX(occurred_at_ms) FROM account_control_event
             WHERE account_id=?1 AND decision='ALLOWED' AND counted=1 GROUP BY operation
             UNION ALL
             SELECT operation,symbol,MAX(occurred_at_ms) FROM account_control_event
             WHERE account_id=?1 AND decision='ALLOWED' AND counted=1 AND symbol IS NOT NULL
             GROUP BY symbol,operation",
        )
        .map_err(sql)?;
    let recent_allowed = statement
        .query_map([account_id], |row| {
            Ok(json!({
                "operation": row.get::<_, String>(0)?,
                "symbol": row.get::<_, Option<String>>(1)?,
                "occurred_at_ms": row.get::<_, i64>(2)?,
            }))
        })
        .map_err(sql)?
        .collect::<Result<Vec<_>, _>>()
        .map_err(sql)?;
    Ok(json!({
        "counters": counters,
        "recent_allowed": recent_allowed,
        "incomplete": incomplete_history_on(connection, request)?,
    }))
}

pub(crate) fn incomplete_history_on(
    connection: &Connection,
    request: &Value,
) -> Result<bool, AnalysisError> {
    let active_instance = optional_text(request, "active_instance_id")?;
    let mut statement = connection
        .prepare(
            "SELECT instance_id,context_kind,execution_id,source,status
             FROM account_control_session
             WHERE account_id=?1 AND status IN ('active','incomplete') AND started_at_ms<?2
               AND (finished_at_ms IS NULL OR finished_at_ms>=?3)",
        )
        .map_err(sql)?;
    let mut rows = statement
        .query(params![
            number(request, "account_id")?,
            number(request, "end_ms")?,
            number(request, "start_ms")?
        ])
        .map_err(sql)?;
    while let Some(row) = rows.next().map_err(sql)? {
        let instance: String = row.get(0).map_err(sql)?;
        let status: String = row.get(4).map_err(sql)?;
        if status == "active" && Some(instance.as_str()) == active_instance {
            continue;
        }
        let context: String = row.get(1).map_err(sql)?;
        let execution: String = row.get(2).map_err(sql)?;
        let source: String = row.get(3).map_err(sql)?;
        if context_kind(&context, &execution) == "account_operation"
            && matches!(
                source.as_str(),
                "account_assets" | "connection_prepare" | "instrument_catalog"
            )
        {
            continue;
        }
        return Ok(true);
    }
    Ok(false)
}

/// 登记 guard；此事务结束后才允许 Python 调用渠道 SDK。
pub fn register_session(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let connection = writer_scope::open_writer(Path::new(path))?;
    writer_scope::immediate(&connection, || {
        connection
            .execute(
                "INSERT INTO account_control_session
                 (instance_id,account_id,execution_id,context_kind,source,timezone,started_at_ms,status)
                 VALUES (?1,?2,?3,?4,?5,?6,?7,'active')",
                params![
                    text(request, "instance_id")?, number(request, "account_id")?,
                    text(request, "execution_id")?, text(request, "context_kind")?,
                    text(request, "source")?, text(request, "timezone")?, number(request, "started_at_ms")?,
                ],
            )
            .map_err(sql)?;
        Ok(Value::Null)
    })
}

/// 会话收尾、所有累计量和事件同事务提交；预警另起事务。
pub fn flush_records(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let connection = writer_scope::open_writer(Path::new(path))?;
    writer_scope::immediate(&connection, || flush_records_on(&connection, request))?;
    let warnings: Vec<&Value> = array(request, "events")?
        .iter()
        .filter(|event| {
            event
                .pointer("/metadata/quota_warning")
                .is_some_and(Value::is_object)
        })
        .collect();
    let warning_error = if warnings.is_empty() {
        None
    } else {
        writer_scope::immediate(&connection, || {
            for event in &warnings {
                if event.pointer("/metadata/quota_warning/account_id") != event.get("account_id") {
                    return Err(AnalysisError::new(
                        "ACCOUNT_CONTROL",
                        "额度预警与账户不匹配",
                    ));
                }
            }
            let warnings: Vec<_> = warnings
                .iter()
                .map(|event| &event["metadata"]["quota_warning"])
                .collect();
            crate::control_monitor::enqueue_quota_warnings_on(
                &connection,
                &json!(warnings),
                text(request, "warning_created_at")?,
            )
        })
        .err()
        .map(|error| error.to_string())
    };
    Ok(json!({"warning_error": warning_error}))
}

fn flush_records_on(connection: &Connection, request: &Value) -> Result<Value, AnalysisError> {
    let mut delta_statement = connection.prepare(
        "INSERT INTO account_control_counter_delta
         (account_id,execution_id,context_kind,source,control_date,bucket_type,bucket_start,scope_type,symbol,operation,delta_count,delta_uid)
         VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12) ON CONFLICT(delta_uid) DO NOTHING",
    ).map_err(sql)?;
    for row in array(request, "counter_deltas")? {
        let delta = number(row, "delta_count")?;
        if delta < 1 || number(row, "account_id")? < 1 || text(row, "operation")?.trim().is_empty()
        {
            return Err(invalid("counter_deltas"));
        }
        delta_statement
            .execute(params![
                number(row, "account_id")?,
                text(row, "execution_id")?,
                text(row, "context_kind")?,
                text(row, "source")?,
                text(row, "control_date")?,
                stored_enum(row, "bucket_type", &[("minute", "MINUTE"), ("day", "DAY")])?,
                text(row, "bucket_start")?,
                stored_enum(
                    row,
                    "scope_type",
                    &[("account", "ACCOUNT"), ("symbol", "SYMBOL")]
                )?,
                optional_text(row, "symbol")?,
                text(row, "operation")?,
                delta,
                text(row, "delta_uid")?,
            ])
            .map_err(sql)?;
    }
    let mut event_statement = connection.prepare(
        "INSERT INTO account_control_event
         (account_id,execution_id,context_kind,source,control_date,seq,channel,operation,symbol,metadata,decision,counted,outcome,event_uid,created_at,occurred_at_ms)
         VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14,?15,?16) ON CONFLICT(event_uid) DO NOTHING",
    ).map_err(sql)?;
    for row in array(request, "events")? {
        let counted = row
            .get("counted")
            .and_then(Value::as_bool)
            .ok_or_else(|| invalid("counted"))?;
        if number(row, "account_id")? < 1
            || number(row, "seq")? < 0
            || number(row, "occurred_at_ms")? < 0
            || text(row, "operation")?.trim().is_empty()
        {
            return Err(invalid("events"));
        }
        event_statement
            .execute(params![
                number(row, "account_id")?,
                text(row, "execution_id")?,
                text(row, "context_kind")?,
                text(row, "source")?,
                text(row, "control_date")?,
                number(row, "seq")?,
                text(row, "channel")?,
                text(row, "operation")?,
                optional_text(row, "symbol")?,
                metadata(row)?,
                stored_enum(
                    row,
                    "decision",
                    &[("allowed", "ALLOWED"), ("blocked", "BLOCKED")]
                )?,
                counted,
                text(row, "outcome")?,
                text(row, "event_uid")?,
                text(row, "created_at")?,
                number(row, "occurred_at_ms")?,
            ])
            .map_err(sql)?;
    }
    if let Some(session) = request
        .get("close_session")
        .filter(|value| !value.is_null())
    {
        connection.execute(
            "UPDATE account_control_session SET status='closed',finished_at_ms=?1 WHERE instance_id=?2",
            params![number(session,"finished_at_ms")?, text(session,"instance_id")?],
        ).map_err(sql)?;
    }
    Ok(Value::Null)
}

/// 人工撤单回填只更新既有事件，不创建新的控制事实。
pub fn update_event_outcomes(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let connection = writer_scope::open_writer(Path::new(path))?;
    writer_scope::immediate(&connection, || {
        update_event_outcomes_on(&connection, &request["events"])
    })
}

pub(crate) fn update_event_outcomes_on(
    connection: &Connection,
    events: &Value,
) -> Result<Value, AnalysisError> {
    let mut updated_uids = Vec::new();
    let mut statement = connection
        .prepare("UPDATE account_control_event SET outcome=?1,metadata=?2 WHERE event_uid=?3 AND (?4 IS NULL OR account_id=?4)")
        .map_err(sql)?;
    for row in events.as_array().ok_or_else(|| invalid("events"))? {
        let account_id = row
            .get("account_id")
            .filter(|value| !value.is_null())
            .map(|_| number(row, "account_id"))
            .transpose()?;
        let uid = text(row, "event_uid")?;
        let updated = statement
            .execute(params![
                text(row, "outcome")?,
                metadata(row)?,
                uid,
                account_id,
            ])
            .map_err(sql)?;
        if updated > 0 {
            updated_uids.push(uid);
        }
    }
    Ok(json!({"updated": updated_uids.len(), "updated_uids": updated_uids}))
}
