//! Coarse-grained native order submission and monitoring read views.

use chrono::{DateTime, NaiveDate, Offset, TimeZone, Utc};
use chrono_tz::Tz;
use rusqlite::{params, Connection, OptionalExtension};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

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
        if !["place_order", "cancel_order"].contains(&fact.operation.as_str())
            || !["pending", "accepted", "rejected", "unknown"].contains(&fact.send_status.as_str())
            || !["unknown", "acknowledged", "rejected"].contains(&fact.broker_status.as_str())
        {
            return Err("INVALID_DATA: invalid submission status or operation".into());
        }
        data.push(fact);
    }
    Ok(json!({"control_date":day,"timezone":timezone,"summary":summary,"data":data}))
}

#[derive(Deserialize, Serialize)]
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
    let stored: Option<String> = connection
        .query_row(
            "SELECT data FROM order_submission_monitor WHERE account_id=?1",
            [account_id],
            |row| row.get(0),
        )
        .optional()
        .map_err(database)?;
    let config: Settings = match stored {
        Some(data) => decode(&data)?,
        None => Settings {
            timezone: timezone.into(),
            ..Settings::default()
        },
    };
    if [config.order_limit, config.combined_limit]
        .into_iter()
        .flatten()
        .any(|v| !(1..=1_000_000_000).contains(&v))
    {
        return Err("INVALID_DATA: invalid submission monitor limit".into());
    }
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

#[cfg(test)]
mod tests {
    use super::*;
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
}
