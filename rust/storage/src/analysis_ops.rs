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
    let mut statement = connection
        .prepare("SELECT created_at FROM executerecord WHERE account_id=?1 AND history_visible=1 AND result_staged=0")
        .map_err(database_error)?;
    let mut rows = statement.query([account]).map_err(database_error)?;
    let mut first: Option<NaiveDate> = None;
    let mut last: Option<NaiveDate> = None;
    while let Some(row) = rows.next().map_err(database_error)? {
        let timestamp: String = row.get(0).map_err(database_error)?;
        let day = crate::calendar::local_time(&timestamp)
            .map_err(AnalysisError::from)?
            .date();
        first = Some(first.map_or(day, |first| first.min(day)));
        last = Some(last.map_or(day, |last| last.max(day)));
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
    let token = text(request, "running_token")?;
    let logic = text(request, "logic_version")?;
    let engine = text(request, "engine_version")?;
    connection
        .query_row(
            "SELECT EXISTS(SELECT 1 FROM performance_snapshot WHERE id=?1 AND account_id=?2
             AND source_version=?3 AND logic_version=?4 AND engine_version=?5
             AND read_view_schema_version=1)",
            params![token, account, source, logic, engine],
            |row| row.get::<_, bool>(0),
        )
        .map(|found| json!(found))
        .map_err(database_error)
}

pub fn publication_committed(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    crate::with_read_snapshot(Path::new(path), |connection| committed(connection, request))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn context_uses_account_scope_and_shanghai_dates() {
        let connection = Connection::open_in_memory().unwrap();
        connection
            .execute_batch(
                "CREATE TABLE account(id INTEGER,trade_channel TEXT);
             CREATE TABLE account_settings(account_id INTEGER,backtest TEXT);
             CREATE TABLE executerecord(account_id INTEGER,created_at TEXT,history_visible INTEGER DEFAULT 1,result_staged INTEGER DEFAULT 0);
             INSERT INTO account VALUES(1,'ctp'),(2,'ctp');
             INSERT INTO account_settings VALUES(1,'{\"backtest_fee_rate\":0.01}');
             INSERT INTO executerecord(account_id,created_at) VALUES(1,'2026-01-01T23:30:00-05:00'),
             (1,'2026-01-02T00:30:00+14:00'),(2,'2020-01-01');",
            )
            .unwrap();
        let result = read_context(&connection, 1).unwrap();
        assert_eq!(result["first_day"], "2026-01-01");
        assert_eq!(result["last_day"], "2026-01-02");
        assert_eq!(result["settings"]["backtest_fee_rate"], 0.01);
        assert_eq!(read_context(&connection, 3).unwrap(), Value::Null);
        assert_eq!(
            read_context(&connection, 2).unwrap()["settings"],
            Value::Null
        );
    }

    #[test]
    fn calendar_batch_updates_atomically_and_preserves_other_days() {
        let connection = Connection::open_in_memory().unwrap();
        connection.execute_batch(
            "CREATE TABLE analysis_calendar_day(channel TEXT,day TEXT,status TEXT,calendar_id TEXT,
             label TEXT,PRIMARY KEY(channel,day));
             INSERT INTO analysis_calendar_day VALUES('ctp','2025-01-01','unavailable',NULL,NULL);",
        ).unwrap();
        let rows = json!([{"day":"2026-01-01","status":"available_open","calendar_id":"SHFE"},
            {"day":"invalid","status":"unavailable"}]);
        let error = crate::writer_scope::immediate(&connection, || {
            save_days(&connection, "ctp", rows.as_array().unwrap())
        })
        .unwrap_err();
        assert_eq!(error.code, "INVALID_REQUEST");
        assert_eq!(
            connection
                .query_row("SELECT COUNT(*) FROM analysis_calendar_day", [], |row| row
                    .get::<_, i64>(
                    0
                ))
                .unwrap(),
            1
        );
        let rows = json!([{"day":"2026-01-01","status":"available_open","calendar_id":"SHFE"}]);
        assert_eq!(
            crate::writer_scope::immediate(&connection, || save_days(
                &connection,
                "ctp",
                rows.as_array().unwrap()
            ))
            .unwrap(),
            1
        );
        let rows = json!([{"day":"2026-01-01","status":"unavailable","calendar_id":null}]);
        crate::writer_scope::immediate(&connection, || {
            save_days(&connection, "ctp", rows.as_array().unwrap())
        })
        .unwrap();
        assert_eq!(
            connection
                .query_row("SELECT COUNT(*) FROM analysis_calendar_day", [], |row| row
                    .get::<_, i64>(
                    0
                ))
                .unwrap(),
            2
        );
        assert_eq!(
            connection
                .query_row(
                    "SELECT status FROM analysis_calendar_day WHERE day='2026-01-01'",
                    [],
                    |row| row.get::<_, String>(0)
                )
                .unwrap(),
            "unavailable"
        );
    }

    #[test]
    fn publication_confirmation_checks_the_exact_claim() {
        let connection = Connection::open_in_memory().unwrap();
        connection.execute_batch(
            "CREATE TABLE performance_snapshot(id TEXT,account_id INTEGER,source_version INTEGER,
             logic_version TEXT,engine_version TEXT,read_view_schema_version INTEGER);
             INSERT INTO performance_snapshot VALUES('token',1,7,'19','equity-ledger-1',1);",
        ).unwrap();
        let request = json!({"account_id":1,"source_version":7,"running_token":"token",
            "logic_version":"19","engine_version":"equity-ledger-1"});
        assert_eq!(committed(&connection, &request).unwrap(), true);
        for (key, value) in [
            ("account_id", json!(2)),
            ("source_version", json!(8)),
            ("running_token", json!("old")),
            ("logic_version", json!("18")),
            ("engine_version", json!("old")),
        ] {
            let mut stale = request.clone();
            stale[key] = value;
            assert_eq!(committed(&connection, &stale).unwrap(), false);
        }
    }
}
