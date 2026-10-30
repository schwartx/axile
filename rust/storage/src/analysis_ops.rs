//! 分析准备和提交确认；渠道 SDK 计算在 Python、数据库事务之外完成。

use std::path::Path;

use chrono::NaiveDate;
use rusqlite::{params, Connection, OptionalExtension};
use serde_json::{json, Value};

use crate::error::AnalysisError;

fn invalid(message: &str) -> AnalysisError {
    AnalysisError::new("INVALID_REQUEST", message)
}

fn database_error(error: rusqlite::Error) -> AnalysisError {
    AnalysisError::new("ANALYSIS_DATABASE", error.to_string())
}

fn integer(request: &Value, key: &str) -> Result<i64, AnalysisError> {
    request
        .get(key)
        .and_then(Value::as_i64)
        .ok_or_else(|| invalid(&format!("{key} must be an integer")))
}

fn text<'a>(request: &'a Value, key: &str) -> Result<&'a str, AnalysisError> {
    request
        .get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| invalid(&format!("{key} must be a string")))
}

fn read_context(connection: &Connection, account: i64) -> Result<Value, AnalysisError> {
    let metadata = connection
        .query_row(
            "SELECT a.trade_channel,s.backtest FROM account a
             LEFT JOIN account_settings s ON s.account_id=a.id WHERE a.id=?1",
            [account],
            |row| {
                Ok((
                    row.get::<_, Option<String>>(0)?,
                    row.get::<_, Option<String>>(1)?,
                ))
            },
        )
        .optional()
        .map_err(database_error)?;
    let Some((channel, settings)) = metadata else {
        return Ok(Value::Null);
    };
    let settings: Value = settings
        .as_deref()
        .map(serde_json::from_str)
        .transpose()
        .map_err(|error| AnalysisError::new("ANALYSIS_DATABASE", error.to_string()))?
        .unwrap_or(Value::Null);
    let mut bounds = Vec::new();
    for order in ["ASC", "DESC"] {
        let value:Option<i64>=connection.query_row(&format!("SELECT created_at_us FROM executerecord WHERE account_id=?1 AND result_staged=0 ORDER BY created_at_us {order},id {order} LIMIT 1"),[account],|r|r.get(0)).optional().map_err(database_error)?;
        bounds.push(
            value
                .map(|t| crate::time_us::local_datetime(t).map(|v| v.date()))
                .transpose()
                .map_err(AnalysisError::from)?,
        );
    }
    let mut first = bounds[0];
    let last = bounds[1];
    if let Some(state) = crate::analysis::state(connection, account, "all")? {
        let dirty: Option<i64> = connection
            .query_row(
                "SELECT dirty_record FROM account_analysis WHERE account_id=?1",
                [account],
                |r| r.get(0),
            )
            .map_err(database_error)?;
        if dirty.is_none_or(|id| id > state.max_id) {
            if let Some(day) = state.current_day {
                first = Some(
                    NaiveDate::parse_from_str(&day, "%Y-%m-%d")
                        .map_err(|e| invalid(&e.to_string()))?,
                );
            }
        }
    }
    Ok(json!({"trade_channel": channel, "settings": settings,
        "first_day": first, "last_day": last}))
}

pub fn context(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let account = integer(request, "account_id")?;
    crate::with_read_snapshot(Path::new(path), |connection| {
        read_context(connection, account)
    })
}

fn save_days(
    connection: &Connection,
    channel: &str,
    days: &[Value],
) -> Result<Value, AnalysisError> {
    let mut statement = connection
        .prepare(
            "INSERT INTO analysis_calendar_day(channel,day,status,calendar_id,label)
             VALUES(?1,?2,?3,?4,?5) ON CONFLICT(channel,day) DO UPDATE SET
             status=excluded.status,calendar_id=excluded.calendar_id,label=excluded.label",
        )
        .map_err(database_error)?;
    for row in days {
        let day = text(row, "day")?;
        if NaiveDate::parse_from_str(day, "%Y-%m-%d")
            .map(|date| date.to_string() != day)
            .unwrap_or(true)
        {
            return Err(invalid("day must be YYYY-MM-DD"));
        }
        let status = text(row, "status")?;
        if ![
            "available_open",
            "available_closed",
            "unavailable",
            "not_required",
            "unregistered",
        ]
        .contains(&status)
        {
            return Err(invalid("invalid calendar status"));
        }
        let optional = |key| match row.get(key) {
            None | Some(Value::Null) => Ok(None),
            Some(Value::String(value)) => Ok(Some(value.as_str())),
            _ => Err(invalid(&format!("{key} must be a string or null"))),
        };
        statement
            .execute(params![
                channel,
                day,
                status,
                optional("calendar_id")?,
                optional("label")?
            ])
            .map_err(database_error)?;
    }
    Ok(json!(days.len()))
}

pub fn save_calendar(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let channel = text(request, "channel")?;
    let days = request
        .get("days")
        .and_then(Value::as_array)
        .ok_or_else(|| invalid("days must be an array"))?;
    let connection = crate::writer_scope::open_writer(Path::new(path))?;
    crate::writer_scope::immediate(&connection, || save_days(&connection, channel, days))
}

fn committed(connection: &Connection, request: &Value) -> Result<Value, AnalysisError> {
    let account = integer(request, "account_id")?;
    let source = integer(request, "source_version")?;
    let logic = text(request, "logic_version")?;
    let engine = text(request, "engine_version")?;
    connection.query_row("SELECT EXISTS(SELECT 1 FROM analysis_state WHERE account_id=?1 AND range_key='all' AND version=?2 AND logic_version=?3 AND engine_version=?4)",params![account,source,logic,engine],|r|r.get::<_,bool>(0)).map(|v|json!(v)).map_err(database_error)
}

pub fn commit_confirmed(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    crate::with_read_snapshot(Path::new(path), |connection| committed(connection, request))
}
