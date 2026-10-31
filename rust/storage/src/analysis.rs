//! Backend analysis: typed current state and daily outputs, without frozen fact copies.
pub(crate) mod contribution;
mod read;
use crate::error::AnalysisError;
pub(crate) use read::{chart, costs, detail, status, summaries};
use rusqlite::{params, types::Value as SqlValue, Connection, OptionalExtension};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::path::Path;
pub(crate) const LOGIC_VERSION: &str = "26";
pub(crate) const TABLES: [&str; 5] = [
    "analysis_state",
    "analysis_replay_position",
    "analysis_contribution_state",
    "analysis_day",
    "analysis_contribution_day",
];
pub(crate) fn sql(e: rusqlite::Error) -> AnalysisError {
    AnalysisError::caused_by("ANALYSIS_DATABASE", e)
}
pub(crate) fn failure(code: &str, message: impl Into<String>) -> AnalysisError {
    AnalysisError::new(code, message)
}
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub(crate) struct State {
    pub account_id: i64,
    pub range_key: String,
    pub version: i64,
    pub logic_version: String,
    pub engine_version: String,
    #[serde(rename = "computed_at_us")]
    pub computed_at: i64,
    pub fee_rate: f64,
    pub trade_channel: String,
    pub max_id: i64,
    #[serde(rename = "start_us")]
    pub start: i64,
    #[serde(rename = "last_time_us")]
    pub last_time: Option<i64>,
    pub last_id: Option<i64>,
    pub initial_equity: Option<f64>,
    #[serde(rename = "baseline_time_us")]
    pub baseline_time: Option<i64>,
    pub baseline_id: Option<i64>,
    pub baseline_execution: Option<String>,
    pub baseline_asset: Option<f64>,
    pub observations: i64,
    pub invalid_assets: i64,
    pub used: i64,
    pub target_used: i64,
    pub skipped: i64,
    pub missing_target: i64,
    pub missing_price: i64,
    #[serde(rename = "skip_first_us")]
    pub skip_first: Option<i64>,
    #[serde(rename = "skip_last_us")]
    pub skip_last: Option<i64>,
    pub target_skipped: i64,
    pub target_missing_target: i64,
    pub target_missing_price: i64,
    #[serde(rename = "target_skip_first_us")]
    pub target_skip_first: Option<i64>,
    #[serde(rename = "target_skip_last_us")]
    pub target_skip_last: Option<i64>,
    pub fallback_count: i64,
    #[serde(rename = "executable_start_us")]
    pub executable_start: Option<i64>,
    #[serde(rename = "target_start_us")]
    pub target_start: Option<i64>,
    pub executable_equity: Option<f64>,
    pub target_equity: Option<f64>,
    pub executable_initialized: i64,
    pub target_initialized: i64,
    pub executable_previous_close: Option<f64>,
    pub target_previous_close: Option<f64>,
    pub executable_close_day: Option<String>,
    pub target_close_day: Option<String>,
    pub executable_close_equity: Option<f64>,
    pub target_close_equity: Option<f64>,
    pub current_day: Option<String>,
    pub last_asset: Option<f64>,
    pub last_execution: Option<String>,
    pub day_participant: i64,
    #[serde(rename = "contribution_time_us")]
    pub contribution_time: Option<i64>,
    pub contribution_day: Option<String>,
    pub contribution_currency: Option<String>,
    pub initial_known: i64,
}

/// Bind named typed columns. JSON only crosses the Rust value boundary; no JSON body is stored.
pub(crate) fn put(c: &Connection, table: &str, value: &Value) -> Result<(), AnalysisError> {
    let body = value
        .as_object()
        .ok_or_else(|| failure("ANALYSIS_STATE", "expected typed row"))?;
    let names = body
        .keys()
        .map(|n| format!("\"{n}\""))
        .collect::<Vec<_>>()
        .join(",");
    let values = body
        .values()
        .map(|v| match v {
            Value::Null => Ok(SqlValue::Null),
            Value::String(s) => Ok(SqlValue::Text(s.clone())),
            Value::Number(n) if n.is_i64() => Ok(SqlValue::Integer(n.as_i64().unwrap())),
            Value::Number(n) => n
                .as_f64()
                .filter(|v| v.is_finite())
                .map(SqlValue::Real)
                .ok_or_else(|| failure("ANALYSIS_STATE", "nonfinite scalar")),
            Value::Bool(b) => Ok(SqlValue::Integer(i64::from(*b))),
            _ => Err(failure("ANALYSIS_STATE", "state columns must be scalars")),
        })
        .collect::<Result<Vec<_>, _>>()?;
    c.execute(
        &format!(
            "INSERT OR REPLACE INTO {table}({names}) VALUES({})",
            vec!["?"; values.len()].join(",")
        ),
        rusqlite::params_from_iter(values),
    )
    .map_err(sql)?;
    Ok(())
}
pub(crate) fn rows(
    c: &Connection,
    query: &str,
    args: &[&dyn rusqlite::ToSql],
) -> Result<Vec<Value>, AnalysisError> {
    let mut q = c.prepare(query).map_err(sql)?;
    let names = q
        .column_names()
        .iter()
        .map(|n| n.to_string())
        .collect::<Vec<_>>();
    let mut rows = q.query(args).map_err(sql)?;
    let mut out = Vec::new();
    while let Some(row) = rows.next().map_err(sql)? {
        let mut body = serde_json::Map::new();
        for (i, name) in names.iter().enumerate() {
            body.insert(
                name.clone(),
                match row.get_ref(i).map_err(sql)? {
                    rusqlite::types::ValueRef::Null => Value::Null,
                    rusqlite::types::ValueRef::Integer(v) => json!(v),
                    rusqlite::types::ValueRef::Real(v) => json!(v),
                    rusqlite::types::ValueRef::Text(v) => json!(std::str::from_utf8(v)
                        .map_err(|_| failure("ANALYSIS_STATE", "invalid UTF-8"))?),
                    _ => return Err(failure("ANALYSIS_STATE", "unexpected blob")),
                },
            );
        }
        out.push(Value::Object(body));
    }
    Ok(out)
}
pub(crate) fn state(
    c: &Connection,
    account: i64,
    range: &str,
) -> Result<Option<State>, AnalysisError> {
    rows(
        c,
        "SELECT * FROM analysis_state WHERE account_id=?1 AND range_key=?2",
        &[&account, &range],
    )?
    .pop()
    .map(|v| serde_json::from_value(v).map_err(|e| failure("ANALYSIS_STATE", e.to_string())))
    .transpose()
}

/// Calculate in an independent scratch database, then atomically compare and apply only changed days.
pub(crate) fn update(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let account = request["account_id"]
        .as_i64()
        .filter(|id| *id > 0)
        .ok_or_else(|| failure("INVALID_REQUEST", "account_id must be positive"))?;
    let version = request["expected_source_version"]
        .as_i64()
        .ok_or_else(|| failure("INVALID_REQUEST", "source version is required"))?;
    let token = request["claim_token"]
        .as_str()
        .filter(|s| !s.is_empty())
        .ok_or_else(|| failure("INVALID_REQUEST", "claim token is required"))?;
    let now = request["computed_at"]
        .as_str()
        .ok_or_else(|| failure("INVALID_REQUEST", "computed_at is required"))?;
    let now = crate::time_us::parse(now).map_err(|e| failure("INVALID_REQUEST", e))?;
    let stage = Connection::open_in_memory().map_err(sql)?;
    stage
        .execute_batch("PRAGMA foreign_keys=OFF")
        .map_err(sql)?;
    stage
        .execute_batch(include_str!("analysis/schema.sql"))
        .map_err(sql)?;
    let mut rebuilt = Vec::new();
    let mut processed = 0;
    let ready = crate::with_read_snapshot(Path::new(path), |source| {
        let current: i64 = source
            .query_row(
                "SELECT source_version FROM account_analysis WHERE account_id=?1",
                [account],
                |r| r.get(0),
            )
            .map_err(sql)?;
        if current != version {
            return Ok(false);
        }
        for range in ["30", "90", "all"] {
            let (rebuild, count) = crate::performance::incremental::compute(
                source, &stage, account, range, version, now,
            )?;
            if rebuild {
                rebuilt.push(range);
            }
            processed += count;
        }
        Ok(true)
    })?;
    let writer = crate::storage_connection::open_writer(Path::new(path), false)?;
    crate::writer_scope::immediate(&writer, || {
        let current:Option<(i64,Option<i64>,Option<String>)>=writer.query_row("SELECT source_version,running_version,running_token FROM account_analysis WHERE account_id=?1",[account],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?))).optional().map_err(sql)?;
        if !ready
            || current.as_ref().is_none_or(|(v, r, t)| {
                *v != version || *r != Some(version) || t.as_deref() != Some(token)
            })
        {
            return Ok(json!({"updated":false,"version":version,"account_id":account}));
        }
        for range in rebuilt {
            for table in TABLES {
                writer
                    .execute(
                        &format!("DELETE FROM {table} WHERE account_id=?1 AND range_key=?2"),
                        params![account, range],
                    )
                    .map_err(sql)?;
            }
        }
        for range in ["30", "90", "all"] {
            for table in ["analysis_replay_position", "analysis_contribution_state"] {
                writer
                    .execute(
                        &format!("DELETE FROM {table} WHERE account_id=?1 AND range_key=?2"),
                        params![account, range],
                    )
                    .map_err(sql)?;
            }
        }
        for table in TABLES {
            for row in rows(&stage, &format!("SELECT * FROM {table}"), &[])? {
                put(&writer, table, &row)?;
            }
        }
        writer.execute("UPDATE account_analysis SET requested=0,running_version=NULL,running_token=NULL,failures=0,retry_at_us=0,error=NULL,dirty_record=NULL WHERE account_id=?1",[account]).map_err(sql)?;
        Ok(
            json!({"updated":true,"version":version,"account_id":account,"processed_observations":processed}),
        )
    })
}

pub(crate) fn query(path: &str, q: &Value) -> Result<Value, AnalysisError> {
    let id = q["account_id"]
        .as_i64()
        .filter(|v| *v > 0)
        .ok_or_else(|| failure("INVALID_REQUEST", "account_id must be positive"))?;
    let key = q["range"].as_str().unwrap_or("all");
    if !["30", "90", "all"].contains(&key) {
        return Err(failure("INVALID_REQUEST", "invalid analysis range"));
    }
    let command = q["command"]
        .as_str()
        .ok_or_else(|| failure("INVALID_REQUEST", "command is required"))?;
    let version = q["version"].as_str().unwrap_or("");
    crate::with_read_snapshot(Path::new(path), |c| match command {
        "status" => status(c, id),
        "chart" => chart(c, id, version, key),
        "costs" => costs(c, id, version, key, q),
        "detail" => {
            let record = q["record_id"]
                .as_i64()
                .filter(|v| *v > 0)
                .ok_or_else(|| failure("INVALID_REQUEST", "record_id is required"))?;
            let limit = q["limit"].as_u64().unwrap_or(200);
            if !(1..=500).contains(&limit) {
                return Err(failure("INVALID_REQUEST", "invalid detail page size"));
            }
            let offset = q["offset"].as_u64().unwrap_or(0);
            detail(
                c,
                id,
                version,
                key,
                record,
                limit as usize,
                usize::try_from(offset)
                    .map_err(|_| failure("INVALID_REQUEST", "invalid offset"))?,
            )
        }
        _ => Err(failure("INVALID_REQUEST", "unknown analysis read")),
    })
}
