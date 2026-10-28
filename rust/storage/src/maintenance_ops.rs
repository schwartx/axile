//! 离线维护与回放报告；表、过滤条件、备份和事务都留在原生层。

use std::collections::BTreeMap;
use std::fs::OpenOptions;
use std::path::{Path, PathBuf};
use std::time::Duration;

use rusqlite::{Connection, OptionalExtension};
use serde_json::{json, Map, Value};

use crate::error::AnalysisError;

fn invalid(message: impl Into<String>) -> AnalysisError {
    AnalysisError::new("MAINTENANCE_INVALID", message)
}

fn sql(error: rusqlite::Error) -> AnalysisError {
    AnalysisError::new("MAINTENANCE_SQL", error.to_string())
}

fn io(error: std::io::Error) -> AnalysisError {
    AnalysisError::new("MAINTENANCE_IO", error.to_string())
}

fn account_id(request: &Value) -> Result<i64, AnalysisError> {
    request
        .get("account_id")
        .and_then(Value::as_i64)
        .ok_or_else(|| invalid("缺少 account_id"))
}

pub(crate) fn backup_path(
    database: &Path,
    request: &Value,
    purpose: &str,
) -> Result<PathBuf, AnalysisError> {
    let stamp = request
        .get("backup_suffix")
        .and_then(Value::as_str)
        .filter(|stamp| !stamp.is_empty() && stamp.bytes().all(|c| c.is_ascii_alphanumeric()))
        .ok_or_else(|| invalid("缺少有效备份时间"))?;
    let filename = database
        .file_name()
        .and_then(|name| name.to_str())
        .ok_or_else(|| invalid("无效数据库路径"))?;
    for index in 1..10_000 {
        let candidate =
            database.with_file_name(format!("{filename}.{purpose}-{stamp}-{index}.bak"));
        match OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&candidate)
        {
            Ok(_) => {
                std::fs::set_permissions(
                    &candidate,
                    std::fs::metadata(database).map_err(io)?.permissions(),
                )
                .map_err(io)?;
                return Ok(candidate);
            }
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(error) => return Err(io(error)),
        }
    }
    Err(invalid("无法生成不覆盖已有文件的备份路径"))
}

pub(crate) fn backup(database: &Path, target: &Path) -> Result<(), AnalysisError> {
    let result = (|| {
        let source = crate::storage_connection::open_reader(database)?;
        let mut destination = Connection::open(target).map_err(sql)?;
        let backup = rusqlite::backup::Backup::new(&source, &mut destination).map_err(sql)?;
        backup
            .run_to_completion(128, Duration::from_millis(10), None)
            .map_err(sql)
    })();
    if result.is_err() {
        let _ = std::fs::remove_file(target);
    }
    result
}

/// 一次读取回放提交基线或该基线之后的意图、调度跳过结果。
pub fn replay_observe(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let account = account_id(request)?;
    crate::with_read_snapshot(Path::new(path), |connection| {
        let intent: Option<(i64, String)> = connection.query_row(
            "SELECT id,status FROM execution_intent WHERE account_id=?1 ORDER BY id DESC LIMIT 1",
            [account], |row| Ok((row.get(0)?,row.get(1)?))).optional().map_err(sql)?;
        let skip: Option<(i64, String)> = connection.query_row(
            "SELECT id,reason_code FROM schedule_skip WHERE account_id=?1 ORDER BY id DESC LIMIT 1",
            [account], |row| Ok((row.get(0)?,row.get(1)?))).optional().map_err(sql)?;
        let baseline_intent = request
            .get("baseline_intent_id")
            .and_then(Value::as_i64)
            .unwrap_or(i64::MAX);
        let baseline_skip = request
            .get("baseline_skip_id")
            .and_then(Value::as_i64)
            .unwrap_or(i64::MAX);
        let outcome = match &intent {
            Some((id, status)) if *id > baseline_intent => {
                (!matches!(status.as_str(), "QUEUED" | "RUNNING" | "TERMINATING"))
                    .then(|| format!("intent:{status}"))
            }
            _ => skip
                .as_ref()
                .filter(|(id, _)| *id > baseline_skip)
                .map(|(_, reason)| format!("skip:{reason}")),
        };
        Ok(
            json!({"latest_intent_id":intent.as_ref().map_or(0,|(id,_)|*id),
            "latest_skip_id":skip.as_ref().map_or(0,|(id,_)|*id),"outcome":outcome}),
        )
    })
}

fn execution_report(connection: &Connection, account: Option<i64>) -> Result<Value, AnalysisError> {
    let span = connection.query_row(
        "SELECT MIN(created_at),MAX(created_at) FROM executerecord WHERE (?1 IS NULL OR account_id=?1)",
        [account], |row| Ok(json!([row.get::<_,Option<String>>(0)?,row.get::<_,Option<String>>(1)?]))).map_err(sql)?;
    let mut query = connection
        .prepare(
            "SELECT DISTINCT t.map_key FROM executerecord r
         JOIN execution_input i ON i.id=r.input_id AND i.account_id=r.account_id
         JOIN execution_input_target t ON t.input_id=i.id
         WHERE t.role='curr_target' AND t.map_key<>'' AND typeof(t.value) IN ('integer','real')
         AND t.value BETWEEN -1.7976931348623157e308 AND 1.7976931348623157e308
         AND (?1 IS NULL OR r.account_id=?1) ORDER BY t.map_key LIMIT 20",
        )
        .map_err(sql)?;
    let symbols = query
        .query_map([account], |row| row.get::<_, String>(0))
        .map_err(sql)?
        .collect::<rusqlite::Result<Vec<_>>>()
        .map_err(sql)?;
    let mut query = connection.prepare("SELECT status,COUNT(*) FROM executerecord WHERE (?1 IS NULL OR account_id=?1) GROUP BY status").map_err(sql)?;
    let mut statuses = BTreeMap::<String, i64>::new();
    let mut rows = query.query([account]).map_err(sql)?;
    while let Some(row) = rows.next().map_err(sql)? {
        let status = match row.get_ref(0).map_err(sql)? {
            rusqlite::types::ValueRef::Text(raw) if !raw.is_empty() => {
                String::from_utf8_lossy(raw).into_owned()
            }
            _ => "?".into(),
        };
        *statuses.entry(status).or_default() += row.get::<_, i64>(1).map_err(sql)?;
    }
    Ok(json!({"executerecord_span":span,"target_symbols":symbols,"execution_status":statuses}))
}

/// 报告只读取标量事实；未知值原样保留，历史文档不作为回退。
pub fn replay_report(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    if request
        .get("account_id")
        .is_some_and(|value| !value.is_null() && value.as_i64().is_none())
    {
        return Err(invalid("account_id 必须为整数或 null"));
    }
    let account = request.get("account_id").and_then(Value::as_i64);
    crate::with_read_snapshot(Path::new(path), |connection| {
        let mut statement = connection
            .prepare("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
            .map_err(sql)?;
        let tables = statement
            .query_map([], |row| row.get::<_, String>(0))
            .map_err(sql)?
            .collect::<rusqlite::Result<Vec<_>>>()
            .map_err(sql)?;
        let mut counts = Map::new();
        for table in tables {
            let escaped = table.replace('"', "\"\"");
            let count = connection
                .query_row(&format!("SELECT COUNT(*) FROM \"{escaped}\""), [], |row| {
                    row.get::<_, i64>(0)
                })
                .map_err(sql)?;
            counts.insert(table, json!(count));
        }
        let mut report = Map::new();
        if counts
            .get("executerecord")
            .and_then(Value::as_i64)
            .unwrap_or(0)
            > 0
        {
            report.extend(
                execution_report(connection, account)?
                    .as_object()
                    .expect("report object")
                    .clone(),
            );
        }
        if counts
            .get("execution_event")
            .and_then(Value::as_i64)
            .unwrap_or(0)
            > 0
        {
            let mut statement = connection.prepare("SELECT event_type,COUNT(*) FROM execution_event WHERE (?1 IS NULL OR account_id=?1) GROUP BY event_type ORDER BY 2 DESC").map_err(sql)?;
            let pairs = statement
                .query_map([account], |row| {
                    Ok((row.get::<_, String>(0)?, json!(row.get::<_, i64>(1)?)))
                })
                .map_err(sql)?;
            report.insert(
                "event_types".into(),
                Value::Object(
                    pairs
                        .collect::<rusqlite::Result<Map<_, _>>>()
                        .map_err(sql)?,
                ),
            );
        }
        if counts
            .get("account_asset_snapshot")
            .and_then(Value::as_i64)
            .unwrap_or(0)
            > 0
        {
            let span = connection.query_row("SELECT MIN(created_at),MAX(created_at) FROM account_asset_snapshot WHERE (?1 IS NULL OR account_id=?1)",[account], |row|Ok(json!([row.get::<_,Option<String>>(0)?,row.get::<_,Option<String>>(1)?]))).map_err(sql)?;
            report.insert("asset_snapshot_span".into(), span);
        }
        report.insert("tables".into(), Value::Object(counts));
        Ok(Value::Object(report))
    })
}
