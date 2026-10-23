//! 原生报撤事实、监测事务与读取视图。

use std::path::Path;

use chrono::{DateTime, NaiveDate, Offset, TimeZone, Utc};
use chrono_tz::Tz;
use rusqlite::{params, Connection, OptionalExtension};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use crate::error::AnalysisError;
use crate::storage_connection;
use crate::writer_scope;

fn database(error: rusqlite::Error) -> String {
    format!("DATABASE_QUERY: {error}")
}

fn decode<T: serde::de::DeserializeOwned>(data: &str) -> Result<T, String> {
    serde_json::from_str(data).map_err(|error| format!("INVALID_DATA: {error}"))
}

fn window(start: i64, end: i64) -> Result<(), String> {
    if start >= end {
        return Err("INVALID_REQUEST: start_ms must precede end_ms".into());
    }
    Ok(())
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ExecutionLinks {
    account_id: i64,
    identities: Vec<(String, String)>,
}

/// 一次快照内按账户和完整订单身份查最新执行关联。
pub fn order_execution_links(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let request: ExecutionLinks = serde_json::from_value(request.clone())
        .map_err(|error| AnalysisError::new("INVALID_REQUEST", error.to_string()))?;
    if request.account_id < 1 {
        return Err(AnalysisError::new(
            "INVALID_REQUEST",
            "account_id 必须为正整数",
        ));
    }
    if request.identities.is_empty() {
        return Ok(json!([]));
    }
    let connection = storage_connection::open_reader(Path::new(path))?;
    let mut statement = connection
        .prepare(
            "SELECT symbol,order_id,execution_id FROM execution_event WHERE id IN (
             SELECT max(id) FROM execution_event WHERE account_id=?1 AND (symbol,order_id) IN (
                 SELECT json_extract(value,'$[0]'),json_extract(value,'$[1]') FROM json_each(?2)
             ) GROUP BY symbol,order_id
         ) ORDER BY id DESC",
        )
        .map_err(database)?;
    let rows = statement
        .query_map(
            params![
                request.account_id,
                serde_json::to_string(&request.identities).expect("identities are serializable")
            ],
            |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, Option<String>>(2)?,
                ))
            },
        )
        .map_err(database)?
        .collect::<Result<Vec<_>, _>>()
        .map_err(database)?;
    Ok(json!(rows))
}

pub fn summary(
    connection: &Connection,
    account_id: i64,
    start: i64,
    end: i64,
) -> Result<Value, String> {
    window(start, end)?;
    let mut result = json!({"scope":"instrumented_ctp_calls", "recorded":0,
        "sync_accepted":0,"sync_rejected":0,"unknown":0,"broker_acknowledged":0,
        "broker_rejected":0,"cancel_recorded":0,"cancel_sync_accepted":0,
        "cancel_sync_rejected":0,"cancel_unknown":0});
    let mut statement = connection
        .prepare(
            "SELECT coalesce(json_extract(data,'$.operation'),'place_order'),
         json_extract(data,'$.send_status'),json_extract(data,'$.broker_status'),count(*)
         FROM order_submission WHERE account_id=?1 AND occurred_at_ms>=?2 AND occurred_at_ms<?3
         GROUP BY 1,2,3",
        )
        .map_err(database)?;
    let rows = statement
        .query_map(params![account_id, start, end], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, Option<String>>(1)?,
                row.get::<_, Option<String>>(2)?,
                row.get::<_, i64>(3)?,
            ))
        })
        .map_err(database)?;
    for row in rows {
        let (operation, send, broker, count) = row.map_err(database)?;
        let cancel = operation == "cancel_order";
        let status = match (cancel, send.as_deref()) {
            (true, Some("accepted")) => "cancel_sync_accepted",
            (true, Some("rejected")) => "cancel_sync_rejected",
            (true, _) => "cancel_unknown",
            (false, Some("accepted")) => "sync_accepted",
            (false, Some("rejected")) => "sync_rejected",
            (false, _) => "unknown",
        };
        for key in [
            if cancel {
                "cancel_recorded"
            } else {
                "recorded"
            },
            status,
        ] {
            result[key] = json!(result[key].as_i64().unwrap() + count);
        }
        if !cancel {
            let key = match broker.as_deref() {
                Some("acknowledged") => Some("broker_acknowledged"),
                Some("rejected") => Some("broker_rejected"),
                _ => None,
            };
            if let Some(key) = key {
                result[key] = json!(result[key].as_i64().unwrap() + count);
            }
        }
    }
    Ok(result)
}

#[derive(Deserialize, Serialize)]
struct Fact {
    submission_uid: String,
    account_id: i64,
    #[serde(default = "channel")]
    channel: String,
    source: String,
    #[serde(default = "operation")]
    operation: String,
    #[serde(default)]
    execution_id: Option<String>,
    symbol: String,
    order_id: String,
    request_id: i64,
    occurred_at_ms: i64,
    #[serde(default)]
    revision: i64,
    #[serde(default = "pending")]
    send_status: String,
    #[serde(default)]
    return_code: Option<i64>,
    #[serde(default = "unknown")]
    broker_status: String,
    #[serde(default)]
    error_id: Option<i64>,
}
fn channel() -> String {
    "ctp".into()
}
fn operation() -> String {
    "place_order".into()
}
fn pending() -> String {
    "pending".into()
}
fn unknown() -> String {
    "unknown".into()
}

fn validate_fact(fact: &Fact) -> Result<(), String> {
    if !["place_order", "cancel_order"].contains(&fact.operation.as_str())
        || !["pending", "accepted", "rejected", "unknown"].contains(&fact.send_status.as_str())
        || !["unknown", "acknowledged", "rejected"].contains(&fact.broker_status.as_str())
    {
        return Err("INVALID_DATA: invalid submission status or operation".into());
    }
    Ok(())
}

pub fn page(
    connection: &Connection,
    account_id: i64,
    day: &str,
    timezone: &str,
    start: i64,
    end: i64,
    limit: i64,
) -> Result<Value, String> {
    if !(1..=500).contains(&limit) {
        return Err("INVALID_REQUEST: limit must be 1 through 500".into());
    }
    let summary = summary(connection, account_id, start, end)?;
    let mut statement = connection.prepare(
        "SELECT data FROM order_submission WHERE account_id=?1 AND occurred_at_ms>=?2 AND occurred_at_ms<?3
         ORDER BY occurred_at_ms DESC,submission_uid LIMIT ?4"
    ).map_err(database)?;
    let rows = statement
        .query_map(params![account_id, start, end, limit], |row| {
            row.get::<_, String>(0)
        })
        .map_err(database)?;
    let mut data = Vec::new();
    for row in rows {
        let fact: Fact = decode(&row.map_err(database)?)?;
        validate_fact(&fact)?;
        data.push(fact);
    }
    Ok(json!({"control_date":day,"timezone":timezone,"summary":summary,"data":data}))
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(default, deny_unknown_fields)]
struct Settings {
    order_limit: Option<i64>,
    combined_limit: Option<i64>,
    timezone: String,
    revision: i64,
    updated_at_ms: Option<i64>,
}
impl Default for Settings {
    fn default() -> Self {
        Self {
            order_limit: None,
            combined_limit: None,
            timezone: "Asia/Shanghai".into(),
            revision: 0,
            updated_at_ms: None,
        }
    }
}

fn stored_settings(connection: &Connection, account_id: i64) -> Result<Option<Settings>, String> {
    let stored: Option<String> = connection
        .query_row(
            "SELECT data FROM order_submission_monitor WHERE account_id=?1",
            [account_id],
            |row| row.get(0),
        )
        .optional()
        .map_err(database)?;
    let config: Option<Settings> = stored.as_deref().map(decode).transpose()?;
    if let Some(config) = &config {
        validate_limits(config.order_limit, config.combined_limit)?;
    }
    Ok(config)
}

fn validate_limits(order: Option<i64>, combined: Option<i64>) -> Result<(), String> {
    if [order, combined]
        .into_iter()
        .flatten()
        .any(|value| !(1..=1_000_000_000).contains(&value))
    {
        return Err("INVALID_DATA: invalid submission monitor limit".into());
    }
    Ok(())
}

#[derive(Deserialize, Serialize)]
struct Warning {
    event_uid: String,
    account_id: i64,
    metric: String,
    value: i64,
    limit: i64,
    control_date: String,
    timezone: String,
    occurred_at_ms: i64,
    revision: i64,
    #[serde(default)]
    submission_uid: Option<String>,
    #[serde(default)]
    execution_id: Option<String>,
    source: String,
}

fn effective_window(now: i64, timezone: &str) -> Result<(String, i64, i64), String> {
    let tz: Tz = timezone
        .parse()
        .map_err(|_| "INVALID_DATA: invalid monitor timezone".to_owned())?;
    let date = DateTime::<Utc>::from_timestamp_millis(now)
        .ok_or("INVALID_REQUEST: invalid now_ms")?
        .with_timezone(&tz)
        .date_naive();
    let midnight = |date: NaiveDate| -> Result<i64, String> {
        let local = date.and_hms_opt(0, 0, 0).unwrap();
        if let Some(value) = tz.from_local_datetime(&local).earliest() {
            return Ok(value.timestamp_millis());
        }
        // Python ZoneInfo attaches the pre-transition offset to nonexistent midnight.
        let prior = local - chrono::Duration::hours(12);
        let offset = tz
            .offset_from_local_datetime(&prior)
            .earliest()
            .ok_or("INVALID_DATA: cannot resolve monitor midnight")?
            .fix()
            .local_minus_utc();
        Ok(local.and_utc().timestamp_millis() - i64::from(offset) * 1000)
    };
    Ok((
        date.to_string(),
        midnight(date)?,
        midnight(date.succ_opt().ok_or("INVALID_REQUEST: day overflow")?)?,
    ))
}

fn metric(name: &str, value: i64, unknown: i64, limit: Option<i64>) -> Value {
    let state = match limit {
        None => "disabled",
        Some(limit) if value >= limit => "reached",
        Some(_) if unknown != 0 => "unknown",
        _ => "below",
    };
    json!({"metric":name,"value":value,"unknown":unknown,"limit":limit,"state":state})
}

pub fn monitor(
    connection: &Connection,
    account_id: i64,
    day: &str,
    timezone: &str,
    start: i64,
    end: i64,
    now: i64,
) -> Result<Value, String> {
    let config = stored_settings(connection, account_id)?.unwrap_or_else(|| Settings {
        timezone: timezone.into(),
        ..Settings::default()
    });
    let (day, start, end) = if config.timezone != timezone {
        effective_window(now, &config.timezone)?
    } else {
        (day.to_owned(), start, end)
    };
    let counts = summary(connection, account_id, start, end)?;
    let count = |key: &str| counts[key].as_i64().unwrap();
    let orders = count("sync_accepted") + count("sync_rejected");
    let combined = orders + count("cancel_sync_accepted") + count("cancel_sync_rejected");
    let mut statement = connection
        .prepare(
            "SELECT data FROM order_submission_warning WHERE account_id=?1
         AND json_extract(data,'$.control_date')=?2 AND json_extract(data,'$.revision')=?3",
        )
        .map_err(database)?;
    let rows = statement
        .query_map(params![account_id, day, config.revision], |row| {
            row.get::<_, String>(0)
        })
        .map_err(database)?;
    let mut warnings = Vec::<Warning>::new();
    for row in rows {
        let warning: Warning = decode(&row.map_err(database)?)?;
        if !["order_calls", "combined_calls"].contains(&warning.metric.as_str()) {
            return Err("INVALID_DATA: invalid warning metric".into());
        }
        warnings.push(warning);
    }
    warnings.sort_by(|a, b| a.metric.cmp(&b.metric));
    Ok(
        json!({"settings":config,"control_date":day,"scope":"instrumented_ctp_calls","sampled_at_ms":now,
        "metrics":[metric("order_calls",orders,count("unknown"),config.order_limit),
        metric("combined_calls",combined,count("unknown")+count("cancel_unknown"),config.combined_limit)],"warnings":warnings}),
    )
}

fn open_writer(path: &str) -> Result<Connection, AnalysisError> {
    if path.is_empty() || path == ":memory:" {
        return Err(AnalysisError::new(
            "DATABASE_OPEN",
            "原生报单记录需要持久化 SQLite 服务数据库",
        ));
    }
    writer_scope::open_writer(Path::new(path))
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct SaveFact {
    fact: Fact,
    now_ms: i64,
}

/// 同步提交事实及到值预警,返回前确保 pending 已持久化。
pub fn save_order_submission(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let request: SaveFact = serde_json::from_value(request.clone())
        .map_err(|error| AnalysisError::new("INVALID_REQUEST", error.to_string()))?;
    let fact = request.fact;
    validate_fact(&fact)?;
    if fact.account_id < 1 || fact.revision < 0 {
        return Err(AnalysisError::new(
            "INVALID_REQUEST",
            "账户身份或事实版本无效",
        ));
    }
    let connection = open_writer(path)?;
    writer_scope::immediate(&connection, || {
        let applied = connection.execute(
            "INSERT INTO order_submission(submission_uid,account_id,occurred_at_ms,revision,data)
             VALUES (?1,?2,?3,?4,?5) ON CONFLICT(submission_uid) DO UPDATE SET
             occurred_at_ms=excluded.occurred_at_ms,revision=excluded.revision,data=excluded.data
             WHERE excluded.revision>order_submission.revision
             AND excluded.account_id=order_submission.account_id",
            params![fact.submission_uid, fact.account_id, fact.occurred_at_ms, fact.revision,
                serde_json::to_string(&fact).expect("fact is serializable")],
        ).map_err(database)? != 0;
        if applied {
            if let Some(config) = stored_settings(&connection, fact.account_id)? {
                record_warnings(
                    &connection,
                    fact.account_id,
                    fact.occurred_at_ms,
                    request.now_ms,
                    &config,
                    Some(&fact),
                )?;
            }
        } else {
            let account_id: i64 = connection
                .query_row(
                    "SELECT account_id FROM order_submission WHERE submission_uid=?1",
                    [&fact.submission_uid],
                    |row| row.get(0),
                )
                .map_err(database)?;
            if account_id != fact.account_id {
                return Err(AnalysisError::new(
                    "INVALID_REQUEST",
                    "报单身份不能跨账户复用",
                ));
            }
        }
        Ok(json!({"applied":applied}))
    })
}

fn record_warnings(
    connection: &Connection,
    account_id: i64,
    timestamp: i64,
    now: i64,
    config: &Settings,
    fact: Option<&Fact>,
) -> Result<(), String> {
    let (day, start, end) = effective_window(timestamp, &config.timezone)?;
    if let Some(updated) = config.updated_at_ms {
        if day < effective_window(updated, &config.timezone)?.0 {
            // 新配置不追溯到前一个自然日的迟到回报。
            return Ok(());
        }
    }
    let counts = summary(connection, account_id, start, end)?;
    let count = |key: &str| counts[key].as_i64().unwrap();
    let orders = count("sync_accepted") + count("sync_rejected");
    for (metric, value, limit) in [
        ("order_calls", orders, config.order_limit),
        (
            "combined_calls",
            orders + count("cancel_sync_accepted") + count("cancel_sync_rejected"),
            config.combined_limit,
        ),
    ] {
        let Some(limit) = limit.filter(|limit| value >= *limit) else {
            continue;
        };
        let warning = Warning {
            event_uid: format!("{account_id}:{}:{day}:{metric}", config.revision),
            account_id,
            metric: metric.into(),
            value,
            limit,
            control_date: day.clone(),
            timezone: config.timezone.clone(),
            occurred_at_ms: now,
            revision: config.revision,
            submission_uid: fact.map(|fact| fact.submission_uid.clone()),
            execution_id: fact.and_then(|fact| fact.execution_id.clone()),
            source: fact
                .map_or("configuration", |fact| fact.source.as_str())
                .into(),
        };
        connection
            .execute(
                "INSERT INTO order_submission_warning(event_uid,account_id,occurred_at_ms,data)
             VALUES (?1,?2,?3,?4) ON CONFLICT(event_uid) DO NOTHING",
                params![
                    warning.event_uid,
                    account_id,
                    now,
                    serde_json::to_string(&warning).expect("warning is serializable")
                ],
            )
            .map_err(database)?;
    }
    Ok(())
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct SaveMonitor {
    account_id: i64,
    order_limit: Option<i64>,
    combined_limit: Option<i64>,
    timezone: String,
    now_ms: i64,
}

/// 配置版本在写锁内递增,同配置不变更,保存后立即重评估。
pub fn save_submission_monitor(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let request: SaveMonitor = serde_json::from_value(request.clone())
        .map_err(|error| AnalysisError::new("INVALID_REQUEST", error.to_string()))?;
    validate_limits(request.order_limit, request.combined_limit)?;
    if request.account_id < 1 {
        return Err(AnalysisError::new(
            "INVALID_REQUEST",
            "account_id 必须为正整数",
        ));
    }
    let (day, start, end) = effective_window(request.now_ms, &request.timezone)?;
    let connection = open_writer(path)?;
    writer_scope::immediate(&connection, || {
        let stored = stored_settings(&connection, request.account_id)?;
        let changed = stored.as_ref().map_or(true, |old| {
            (old.order_limit, old.combined_limit, &old.timezone)
                != (
                    request.order_limit,
                    request.combined_limit,
                    &request.timezone,
                )
        });
        let previous = stored.unwrap_or_else(|| Settings {
            timezone: request.timezone.clone(),
            ..Settings::default()
        });
        let current = if changed {
            Settings {
                order_limit: request.order_limit,
                combined_limit: request.combined_limit,
                timezone: request.timezone.clone(),
                revision: previous
                    .revision
                    .checked_add(1)
                    .ok_or_else(|| AnalysisError::new("INVALID_DATA", "监测版本溢出"))?,
                updated_at_ms: Some(request.now_ms),
            }
        } else {
            previous.clone()
        };
        if changed {
            connection
                .execute(
                    "INSERT INTO order_submission_monitor(account_id,data) VALUES (?1,?2)
                 ON CONFLICT(account_id) DO UPDATE SET data=excluded.data",
                    params![
                        request.account_id,
                        serde_json::to_string(&current).expect("settings are serializable")
                    ],
                )
                .map_err(database)?;
            record_warnings(
                &connection,
                request.account_id,
                request.now_ms,
                request.now_ms,
                &current,
                None,
            )?;
        }
        let view = monitor(
            &connection,
            request.account_id,
            &day,
            &request.timezone,
            start,
            end,
            request.now_ms,
        )?;
        Ok(json!({"changed":changed,"previous":previous,"current":current,"monitor":view}))
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;
    use std::sync::{
        atomic::{AtomicU64, Ordering},
        Arc, Barrier,
    };
    use std::thread;
    fn connection() -> Connection {
        let connection = Connection::open_in_memory().unwrap();
        connection.execute_batch("CREATE TABLE order_submission(submission_uid TEXT, account_id INTEGER, occurred_at_ms INTEGER,data TEXT);
            CREATE TABLE order_submission_monitor(account_id INTEGER,data TEXT);
            CREATE TABLE order_submission_warning(account_id INTEGER,data TEXT);").unwrap();
        connection
    }
    #[test]
    fn page_defaults_and_summary_are_independent_of_limit() {
        let db = connection();
        for (uid, status, operation, time) in [
            ("a", "accepted", "place_order", 10),
            ("b", "rejected", "cancel_order", 11),
            ("c", "pending", "place_order", 12),
            ("outside", "accepted", "place_order", 20),
        ] {
            let mut data = json!({"submission_uid":uid,"account_id":1,"source":"script","symbol":"ag","order_id":uid,"request_id":1,"occurred_at_ms":time,"send_status":status});
            if operation == "cancel_order" {
                data["operation"] = json!(operation);
            }
            db.execute(
                "INSERT INTO order_submission VALUES (?1,1,?2,?3)",
                params![uid, time, data.to_string()],
            )
            .unwrap();
        }
        let page = page(&db, 1, "day", "UTC", 0, 20, 1).unwrap();
        assert_eq!(page["data"][0]["submission_uid"], "c");
        assert_eq!(page["data"][0]["broker_status"], "unknown");
        assert_eq!(page["data"][0]["operation"], "place_order");
        assert_eq!(page["summary"]["recorded"], 2);
        assert_eq!(page["summary"]["cancel_sync_rejected"], 1);
        assert_eq!(page["summary"]["unknown"], 1);
        assert_eq!(summary(&db, 2, 0, 20).unwrap()["recorded"], 0);
    }
    #[test]
    fn monitor_uses_saved_timezone_and_revision() {
        let db = connection();
        db.execute(
            "INSERT INTO order_submission_monitor VALUES (1,?1)",
            [json!({"timezone":"America/New_York","revision":2,"order_limit":1}).to_string()],
        )
        .unwrap();
        let now = 1792382400000_i64;
        let (day, start, end) = effective_window(now, "America/New_York").unwrap();
        db.execute(
            "INSERT INTO order_submission VALUES ('a',1,?1,?2)",
            params![start, json!({"send_status":"accepted"}).to_string()],
        )
        .unwrap();
        for (metric, revision, warning_day) in [
            ("order_calls", 2, day.as_str()),
            ("combined_calls", 2, day.as_str()),
            ("order_calls", 1, day.as_str()),
            ("order_calls", 2, "old"),
        ] {
            let warning = json!({"event_uid":format!("{metric}:{revision}:{warning_day}"),"account_id":1,
                "metric":metric,"value":1,"limit":1,"control_date":warning_day,"timezone":"America/New_York",
                "occurred_at_ms":now,"revision":revision,"source":"configuration"});
            db.execute(
                "INSERT INTO order_submission_warning VALUES (1,?1)",
                [warning.to_string()],
            )
            .unwrap();
        }
        let result = monitor(&db, 1, "wrong", "UTC", start - 86400000, start, now).unwrap();
        assert_eq!(result["warnings"].as_array().unwrap().len(), 2);
        assert_eq!(result["warnings"][0]["metric"], "combined_calls");
        assert_eq!(result["warnings"][0]["execution_id"], Value::Null);
        assert_eq!(result["control_date"], day);
        assert_eq!(result["metrics"][0]["state"], "reached");
        assert_eq!(result["metrics"][1]["state"], "disabled");
        assert_eq!(result["sampled_at_ms"], now);
        assert!(end > start);
    }
    #[test]
    fn dst_windows_and_unknown_state() {
        for (timestamp, hours) in [(1772985600000_i64, 23), (1793548800000_i64, 25)] {
            let (_, start, end) = effective_window(timestamp, "America/New_York").unwrap();
            assert_eq!(end - start, hours * 3600000);
        }
        assert_eq!(metric("order_calls", 0, 1, Some(2))["state"], "unknown");
        assert_eq!(metric("order_calls", 2, 1, Some(2))["state"], "reached");
    }

    fn writer_database() -> (PathBuf, Connection) {
        static NEXT: AtomicU64 = AtomicU64::new(0);
        let path = std::env::temp_dir().join(format!(
            "axile-submissions-{}-{}.sqlite",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        let connection = Connection::open(&path).unwrap();
        connection.execute_batch(
            "PRAGMA journal_mode=WAL;
             CREATE TABLE account(id INTEGER PRIMARY KEY);
             INSERT INTO account VALUES (1),(2);
             CREATE TABLE order_submission(submission_uid TEXT PRIMARY KEY,account_id INTEGER NOT NULL
                REFERENCES account(id),occurred_at_ms INTEGER NOT NULL,revision INTEGER NOT NULL,data TEXT NOT NULL);
             CREATE TABLE order_submission_monitor(account_id INTEGER PRIMARY KEY REFERENCES account(id),data TEXT NOT NULL);
             CREATE TABLE order_submission_warning(event_uid TEXT PRIMARY KEY,account_id INTEGER NOT NULL
                REFERENCES account(id),occurred_at_ms INTEGER NOT NULL,data TEXT NOT NULL);"
        ).unwrap();
        (path, connection)
    }

    fn fact(uid: &str, revision: i64, time: i64, status: &str) -> Value {
        json!({"fact":{"submission_uid":uid,"account_id":1,"source":"script","symbol":"ag",
            "order_id":uid,"request_id":1,"occurred_at_ms":time,"revision":revision,
            "send_status":status,"execution_id":"execution"},"now_ms":time+1})
    }

    fn config(time: i64, order: i64, combined: i64) -> Value {
        json!({"account_id":1,"order_limit":order,"combined_limit":combined,"timezone":"UTC","now_ms":time})
    }

    #[test]
    fn writer_is_durable_strictly_revisioned_and_never_creates_missing_database() {
        let (path, db) = writer_database();
        let path_str = path.to_str().unwrap();
        let time = 1792382400000_i64;
        let pending = fact("a", 0, time, "pending");
        assert_eq!(
            save_order_submission(path_str, &pending).unwrap()["applied"],
            true
        );
        let reopened = Connection::open(&path).unwrap();
        let stored: String = reopened
            .query_row("SELECT data FROM order_submission", [], |row| row.get(0))
            .unwrap();
        assert_eq!(decode::<Value>(&stored).unwrap()["send_status"], "pending");
        assert_eq!(
            save_order_submission(path_str, &fact("a", 2, time + 5, "accepted")).unwrap()
                ["applied"],
            true
        );
        assert_eq!(
            save_order_submission(path_str, &fact("a", 2, time, "rejected")).unwrap()["applied"],
            false
        );
        assert_eq!(
            save_order_submission(path_str, &pending).unwrap()["applied"],
            false
        );
        let mut wrong_account = fact("a", 3, time, "rejected");
        wrong_account["fact"]["account_id"] = json!(2);
        assert_eq!(
            save_order_submission(path_str, &wrong_account)
                .unwrap_err()
                .code,
            "INVALID_REQUEST"
        );
        let stored: (i64, i64, String) = reopened.query_row(
            "SELECT revision,occurred_at_ms,json_extract(data,'$.send_status') FROM order_submission",
            [], |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        ).unwrap();
        assert_eq!(stored, (2, time + 5, "accepted".to_owned()));
        let writer = open_writer(path_str).unwrap();
        assert_eq!(
            writer
                .query_row("PRAGMA synchronous", [], |row| row.get::<_, i64>(0))
                .unwrap(),
            2
        );
        drop((writer, reopened, db));
        std::fs::remove_file(&path).unwrap();
        assert_eq!(
            save_order_submission(path_str, &pending).unwrap_err().code,
            "DATABASE_OPEN"
        );
        assert!(!path.exists());
        assert!(save_order_submission(":memory:", &pending).is_err());
    }

    #[test]
    fn execution_links_batch_matches_account_symbol_order_and_latest_row() {
        let (path, db) = writer_database();
        db.execute_batch(
            "CREATE TABLE execution_event(id INTEGER PRIMARY KEY,account_id INTEGER,
             symbol TEXT,order_id TEXT,execution_id TEXT);
             INSERT INTO execution_event VALUES
             (1,1,'rb','same','old'),(2,1,'rb','same','new'),(3,1,'ag','same','silver'),
             (4,2,'rb','same','another-account'),(5,1,'rb','other','another-order'),
             (6,1,'null','same','earlier'),(7,1,'null','same',NULL);",
        )
        .unwrap();
        let mut identities: Vec<Value> = (0..600)
            .map(|index| json!([format!("missing-{index}"), "same"]))
            .collect();
        identities.extend([
            json!(["rb", "same"]),
            json!(["ag", "same"]),
            json!(["rb", "same"]),
            json!(["null", "same"]),
        ]);
        let request = json!({"account_id":1,"identities":identities});
        assert_eq!(
            order_execution_links(path.to_str().unwrap(), &request).unwrap(),
            json!([
                ["null", "same", null],
                ["ag", "same", "silver"],
                ["rb", "same", "new"]
            ])
        );
        assert_eq!(
            order_execution_links(
                path.to_str().unwrap(),
                &json!({"account_id":2,"identities":[["rb","same"]]})
            )
            .unwrap(),
            json!([["rb", "same", "another-account"]])
        );
        assert!(order_execution_links(
            path.to_str().unwrap(),
            &json!({"account_id":1,"identities":[["rb"]]})
        )
        .is_err());
        assert_eq!(
            order_execution_links("unused", &json!({"account_id":1,"identities":[]})).unwrap(),
            json!([])
        );
        drop(db);
        std::fs::remove_file(&path).unwrap();
        assert_eq!(
            order_execution_links(path.to_str().unwrap(), &request)
                .unwrap_err()
                .code,
            "DATABASE_OPEN"
        );
        assert!(!path.exists());
    }

    #[test]
    fn concurrent_revisions_and_calls_keep_highest_fact_and_exact_crossing_once() {
        let (path, db) = writer_database();
        let time = 1792382400000_i64;
        save_submission_monitor(path.to_str().unwrap(), &config(time, 2, 3)).unwrap();
        let barrier = Arc::new(Barrier::new(12));
        let workers: Vec<_> = (0..12)
            .map(|index| {
                let (path, barrier) = (path.clone(), Arc::clone(&barrier));
                thread::spawn(move || {
                    barrier.wait();
                    let request = fact(&format!("call-{index}"), 0, time, "accepted");
                    save_order_submission(path.to_str().unwrap(), &request).unwrap();
                    save_order_submission(path.to_str().unwrap(), &request).unwrap();
                    save_order_submission(
                        path.to_str().unwrap(),
                        &fact("shared", index, time, "accepted"),
                    )
                    .unwrap();
                })
            })
            .collect();
        for worker in workers {
            worker.join().unwrap();
        }
        assert_eq!(
            db.query_row(
                "SELECT revision FROM order_submission WHERE submission_uid='shared'",
                [],
                |row| row.get::<_, i64>(0)
            )
            .unwrap(),
            11
        );
        let (day, start, end) = effective_window(time, "UTC").unwrap();
        let view = monitor(&db, 1, &day, "UTC", start, end, time).unwrap();
        assert_eq!(view["metrics"][0]["value"], 13);
        let warnings = view["warnings"].as_array().unwrap();
        assert_eq!(warnings.len(), 2);
        assert_eq!(warnings[0]["metric"], "combined_calls");
        assert_eq!(warnings[0]["value"], 3);
        assert_eq!(warnings[1]["value"], 2);
        assert!(
            warnings
                .iter()
                .all(|warning| warning["execution_id"] == "execution"
                    && warning["source"] == "script")
        );
        drop(db);
        std::fs::remove_file(path).unwrap();
    }

    #[test]
    fn configuration_races_recheck_under_write_lock_and_identical_saves_preserve_history() {
        let (path, db) = writer_database();
        let time = 1792382400000_i64;
        save_order_submission(path.to_str().unwrap(), &fact("call", 0, time, "accepted")).unwrap();
        let barrier = Arc::new(Barrier::new(8));
        let workers: Vec<_> = (0..8)
            .map(|_| {
                let (path, barrier) = (path.clone(), Arc::clone(&barrier));
                thread::spawn(move || {
                    barrier.wait();
                    save_submission_monitor(path.to_str().unwrap(), &config(time, 1, 1)).unwrap()
                })
            })
            .collect();
        let receipts: Vec<_> = workers
            .into_iter()
            .map(|worker| worker.join().unwrap())
            .collect();
        assert_eq!(
            receipts
                .iter()
                .filter(|receipt| receipt["changed"] == true)
                .count(),
            1
        );
        assert!(receipts
            .iter()
            .all(|receipt| receipt["current"]["revision"] == 1));
        let first =
            save_submission_monitor(path.to_str().unwrap(), &config(time + 100, 1, 1)).unwrap();
        assert_eq!(first["changed"], false);
        assert_eq!(first["current"]["updated_at_ms"], time);
        assert_eq!(first["monitor"]["warnings"][0]["source"], "configuration");
        assert_eq!(
            first["monitor"]["warnings"][0]["submission_uid"],
            Value::Null
        );
        let changed =
            save_submission_monitor(path.to_str().unwrap(), &config(time + 200, 2, 1)).unwrap();
        assert_eq!(changed["current"]["revision"], 2);
        assert_eq!(changed["monitor"]["warnings"].as_array().unwrap().len(), 1);
        assert_eq!(changed["monitor"]["warnings"][0]["value"], 1);
        assert_eq!(
            db.query_row("SELECT count(*) FROM order_submission_warning", [], |row| {
                row.get::<_, i64>(0)
            })
            .unwrap(),
            3
        );
        drop(db);
        std::fs::remove_file(path).unwrap();
    }

    #[test]
    fn warnings_and_fact_or_configuration_roll_back_together() {
        let (path, db) = writer_database();
        let path_str = path.to_str().unwrap();
        let time = 1792382400000_i64;
        save_submission_monitor(path_str, &config(time, 1, 1)).unwrap();
        save_order_submission(path_str, &fact("call", 0, time, "pending")).unwrap();
        db.execute_batch(
            "CREATE TRIGGER reject_warning BEFORE INSERT ON order_submission_warning
            BEGIN SELECT RAISE(ABORT,'forced warning failure'); END;",
        )
        .unwrap();
        assert!(save_order_submission(path_str, &fact("call", 1, time, "accepted")).is_err());
        assert_eq!(
            db.query_row("SELECT revision FROM order_submission", [], |row| row
                .get::<_, i64>(0))
                .unwrap(),
            0
        );
        db.execute_batch("DROP TRIGGER reject_warning;").unwrap();
        save_order_submission(path_str, &fact("call", 1, time, "accepted")).unwrap();
        db.execute_batch(
            "CREATE TRIGGER reject_warning BEFORE INSERT ON order_submission_warning
            BEGIN SELECT RAISE(ABORT,'forced warning failure'); END;",
        )
        .unwrap();
        assert!(save_submission_monitor(path_str, &config(time + 1, 2, 1)).is_err());
        assert_eq!(stored_settings(&db, 1).unwrap().unwrap().revision, 1);
        assert_eq!(
            db.query_row("SELECT count(*) FROM order_submission_warning", [], |row| {
                row.get::<_, i64>(0)
            })
            .unwrap(),
            2
        );
        drop(db);
        std::fs::remove_file(path).unwrap();
    }

    #[test]
    fn saved_timezone_dst_boundaries_and_late_previous_day_reports() {
        for (time, hours) in [(1772985600000_i64, 23), (1793548800000_i64, 25)] {
            let (path, db) = writer_database();
            let path_str = path.to_str().unwrap();
            let (day, start, end) = effective_window(time, "America/New_York").unwrap();
            assert_eq!(end - start, hours * 3600000);
            let mut update = config(time, 2, 2);
            update["timezone"] = json!("America/New_York");
            save_submission_monitor(path_str, &update).unwrap();
            for (uid, occurred) in [
                ("late", start - 1),
                ("first", start),
                ("last", end - 1),
                ("next", end),
            ] {
                save_order_submission(path_str, &fact(uid, 0, occurred, "accepted")).unwrap();
            }
            let view = monitor(&db, 1, "wrong", "UTC", 0, 1, time).unwrap();
            assert_eq!(view["control_date"], day);
            assert_eq!(view["metrics"][0]["value"], 2);
            assert_eq!(view["warnings"].as_array().unwrap().len(), 2);
            assert_eq!(view["warnings"][0]["submission_uid"], "last");
            assert_eq!(
                db.query_row("SELECT count(*) FROM order_submission_warning", [], |row| {
                    row.get::<_, i64>(0)
                })
                .unwrap(),
                2
            );
            drop(db);
            std::fs::remove_file(path).unwrap();
        }
    }
}
