//! 账户运行态对齐的短事务与排程读取。

use std::path::Path;

use rusqlite::{params, Connection, OptionalExtension, Row};
use serde::Deserialize;
use serde_json::{json, Value};

use crate::error::AnalysisError;
use crate::storage_connection::open_reader;
use crate::writer_scope::{immediate, open_writer};

fn database(error: rusqlite::Error) -> AnalysisError {
    AnalysisError::new("DATABASE_QUERY", error.to_string())
}

fn request<T: serde::de::DeserializeOwned>(value: &Value) -> Result<T, AnalysisError> {
    serde_json::from_value(value.clone())
        .map_err(|error| AnalysisError::new("INVALID_REQUEST", error.to_string()))
}

fn sync_row(row: &Row<'_>) -> rusqlite::Result<Value> {
    Ok(json!({
        "id": row.get::<_, i64>(0)?,
        "account_id": row.get::<_, i64>(1)?,
        "revision": row.get::<_, i64>(2)?,
        "status": row.get::<_, String>(3)?,
        "reset_worker": row.get::<_, bool>(4)?,
        "create_request_key": row.get::<_, Option<String>>(5)?,
        "attempts": row.get::<_, i64>(6)?,
        "last_error": row.get::<_, Option<String>>(7)?,
        "requested_at": crate::time_us::read(row, 8)?,
        "last_attempt_at": crate::time_us::read_optional(row, 9)?,
        "synchronized_at": crate::time_us::read_optional(row, 10)?,
    }))
}

pub(crate) fn get_on(
    connection: &Connection,
    account_id: i64,
) -> Result<Option<Value>, AnalysisError> {
    connection
        .query_row(
            "SELECT id,account_id,revision,status,reset_worker,create_request_key,attempts,
             last_error,requested_at_us,last_attempt_at_us,synchronized_at_us
             FROM accountruntimesync WHERE account_id=?1",
            [account_id],
            sync_row,
        )
        .optional()
        .map_err(database)
}

pub fn public_state(path: &str, value: &Value) -> Result<Value, AnalysisError> {
    let account_id = value["account_id"]
        .as_i64()
        .ok_or_else(|| AnalysisError::new("INVALID_REQUEST", "account_id 必须为整数"))?;
    crate::with_read_snapshot(Path::new(path), |connection| {
        let state = connection.query_row(
            "SELECT s.status,s.revision,s.attempts,s.last_error,s.last_attempt_at_us,s.synchronized_at_us FROM account a LEFT JOIN accountruntimesync s ON s.account_id=a.id WHERE a.id=?1",
            [account_id], |row| Ok(json!({"status":row.get::<_,Option<String>>(0)?,"revision":row.get::<_,Option<i64>>(1)?,"attempts":row.get::<_,Option<i64>>(2)?,"last_error":row.get::<_,Option<String>>(3)?,"last_attempt_at":crate::time_us::read_optional(row,4)?,"synchronized_at":crate::time_us::read_optional(row,5)?})),
        ).optional().map_err(database)?.ok_or_else(|| AnalysisError::new("ACCOUNT_NOT_FOUND", "账户不存在"))?;
        if state["status"].is_null() {
            return Err(AnalysisError::new(
                "ACCOUNT_SYNC_PENDING",
                "账户运行态尚待首次同步",
            ));
        }
        if !matches!(
            state["status"].as_str(),
            Some("pending" | "synchronized" | "failed")
        ) {
            return Err(AnalysisError::new("ACCOUNT_DATA", "账户运行态状态无效"));
        }
        Ok(state)
    })
}

pub(crate) fn enqueue_on(
    connection: &Connection,
    account_id: i64,
    reset_worker: bool,
    create_request_key: Option<&str>,
    now: &str,
) -> Result<Value, AnalysisError> {
    let now_us = crate::time_us::parse(now).map_err(AnalysisError::from)?;
    connection
        .query_row(
            "INSERT INTO accountruntimesync
             (account_id,revision,status,reset_worker,create_request_key,attempts,requested_at_us)
             VALUES (?1,1,'pending',?2,?3,0,?4)
             ON CONFLICT(account_id) DO UPDATE SET
             revision=revision+1,status='pending',reset_worker=reset_worker OR excluded.reset_worker,
             last_error=NULL,requested_at_us=excluded.requested_at_us,synchronized_at_us=NULL
             RETURNING id,account_id,revision,status,reset_worker,create_request_key,attempts,
             last_error,requested_at_us,last_attempt_at_us,synchronized_at_us",
            params![account_id, reset_worker, create_request_key, now_us],
            sync_row,
        )
        .map_err(database)
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Begin {
    account_id: i64,
    #[serde(default)]
    scheduled: bool,
    now: String,
}

fn begin_on(connection: &Connection, input: &Begin) -> Result<Value, AnalysisError> {
    let Some(account) = crate::accounts::snapshot_on(connection, input.account_id)? else {
        return if input.scheduled {
            Ok(Value::Null)
        } else {
            Err(AnalysisError::new(
                "ACCOUNT_NOT_FOUND",
                format!("账户不存在: {}", input.account_id),
            ))
        };
    };
    if input.scheduled
        && (account["is_started"] != true
            || !matches!(account["trade_channel"].as_str(), Some("ctp" | "tq")))
    {
        return Ok(Value::Null);
    }
    let now_us = crate::time_us::parse(&input.now).map_err(AnalysisError::from)?;
    connection
        .execute(
            "INSERT INTO accountruntimesync
             (account_id,revision,status,reset_worker,attempts,requested_at_us)
             VALUES (?1,1,'pending',0,0,?2) ON CONFLICT(account_id) DO NOTHING",
            params![input.account_id, now_us],
        )
        .map_err(database)?;
    let (revision, reset_worker, portfolio_id, last_execution) = connection
        .query_row(
            "UPDATE accountruntimesync SET attempts=attempts+1,last_attempt_at_us=?2,
             status='pending',last_error=NULL,synchronized_at_us=NULL WHERE account_id=?1
             RETURNING revision,reset_worker,
             (SELECT portfolio_id FROM portfolioaccount WHERE account_id=?1 ORDER BY id DESC LIMIT 1),
             (SELECT created_at_us FROM executerecord WHERE account_id=?1 AND result_staged=0 ORDER BY id DESC LIMIT 1)",
            params![input.account_id, now_us],
            |row| Ok((row.get::<_, i64>(0)?, row.get::<_, bool>(1)?,
                row.get::<_, Option<i64>>(2)?, crate::time_us::read_optional(row, 3)?)),
        )
        .map_err(database)?;
    // 开始即留审计，取消或进程退出也不会丢失已提交的尝试。
    connection
        .execute(
            "INSERT INTO accountruntimesyncattempt(account_id,revision,succeeded,error,attempted_at_us)
             VALUES (?1,?2,0,NULL,?3)",
            params![input.account_id, revision, now_us],
        )
        .map_err(database)?;
    Ok(
        json!({"account":account,"revision":revision,"reset_worker":reset_worker,
        "portfolio_id":portfolio_id,"last_execution":last_execution.unwrap_or_default(),
        "attempt_id":connection.last_insert_rowid()}),
    )
}

pub fn begin_attempt(path: &str, value: &Value) -> Result<Value, AnalysisError> {
    let input: Begin = request(value)?;
    let connection = open_writer(Path::new(path))?;
    immediate(&connection, || begin_on(&connection, &input))
}

#[derive(Deserialize)]
#[serde(rename_all = "snake_case")]
enum Status {
    Pending,
    Synchronized,
    Failed,
}

impl Status {
    fn as_str(&self) -> &str {
        match self {
            Self::Pending => "pending",
            Self::Synchronized => "synchronized",
            Self::Failed => "failed",
        }
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Finish {
    account_id: i64,
    revision: i64,
    attempt_id: i64,
    status: Status,
    #[serde(default)]
    error: Option<String>,
    now: String,
}

fn finish_on(connection: &Connection, input: &Finish) -> Result<Value, AnalysisError> {
    let now_us = crate::time_us::parse(&input.now).map_err(AnalysisError::from)?;
    let succeeded = matches!(input.status, Status::Synchronized);
    let changed = connection
        .execute(
            "UPDATE accountruntimesyncattempt SET succeeded=?4,error=?5,attempted_at_us=?6
             WHERE id=?1 AND account_id=?2 AND revision=?3",
            params![
                input.attempt_id,
                input.account_id,
                input.revision,
                succeeded,
                input.error,
                now_us
            ],
        )
        .map_err(database)?;
    if changed != 1 {
        return Err(AnalysisError::new(
            "RUNTIME_ATTEMPT_NOT_FOUND",
            "运行态对齐尝试不存在",
        ));
    }
    connection
        .execute(
            "UPDATE accountruntimesync SET status=?3,last_error=?4,
             reset_worker=CASE WHEN ?5 THEN 0 ELSE reset_worker END,
             synchronized_at_us=CASE WHEN ?5 THEN ?6 ELSE synchronized_at_us END
             WHERE account_id=?1 AND revision=?2",
            params![
                input.account_id,
                input.revision,
                input.status.as_str(),
                input.error,
                succeeded,
                now_us
            ],
        )
        .map_err(database)?;
    get_on(connection, input.account_id)?.ok_or_else(|| {
        AnalysisError::new(
            "ACCOUNT_NOT_FOUND",
            format!("账户不存在: {}", input.account_id),
        )
    })
}

pub fn finish_attempt(path: &str, value: &Value) -> Result<Value, AnalysisError> {
    let input: Finish = request(value)?;
    let connection = open_writer(Path::new(path))?;
    immediate(&connection, || finish_on(&connection, &input))
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Scan {
    #[serde(default)]
    started_china_only: bool,
}

pub fn scan(path: &str, value: &Value) -> Result<Value, AnalysisError> {
    let input: Scan = request(value)?;
    let connection = open_reader(Path::new(path))?;
    let mut statement = connection
        .prepare(if input.started_china_only {
            "SELECT a.id FROM account a JOIN account_settings s ON s.account_id=a.id
             WHERE a.trade_channel IN ('ctp','tq') AND json_extract(s.schedule,'$.is_started')=1 ORDER BY a.id"
        } else {
            "SELECT id FROM account ORDER BY id"
        })
        .map_err(database)?;
    let rows = statement
        .query_map([], |row| row.get::<_, i64>(0))
        .map_err(database)?
        .collect::<Result<Vec<_>, _>>()
        .map_err(database)?;
    Ok(json!(rows))
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct AccountId {
    account_id: i64,
}

#[derive(Deserialize)]
struct Schedule {
    #[serde(default)]
    is_started: bool,
    #[serde(default)]
    cron_expr: String,
    #[serde(default)]
    supplement: Option<Value>,
}

pub fn scheduler_account(path: &str, value: &Value) -> Result<Value, AnalysisError> {
    let input: AccountId = request(value)?;
    let connection = open_reader(Path::new(path))?;
    let row = connection
        .query_row(
            "SELECT a.trade_channel,s.schedule,
             (SELECT portfolio_id FROM portfolioaccount WHERE account_id=a.id ORDER BY id DESC LIMIT 1)
             FROM account a JOIN account_settings s ON s.account_id=a.id WHERE a.id=?1",
            [input.account_id],
            |row| Ok((row.get::<_, String>(0)?,row.get::<_, String>(1)?,row.get::<_, Option<i64>>(2)?)),
        )
        .optional()
        .map_err(database)?;
    let Some((channel, schedule, portfolio_id)) = row else {
        return Ok(Value::Null);
    };
    let schedule: Schedule = serde_json::from_str(&schedule)
        .map_err(|error| AnalysisError::new("INVALID_DATA", error.to_string()))?;
    Ok(
        json!({"trade_channel":channel,"is_started":schedule.is_started,
        "cron_expr":schedule.cron_expr,"supplement":schedule.supplement,"portfolio_id":portfolio_id}),
    )
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Skip {
    account_id: i64,
    channel: String,
    triggered_at: String,
    calendar_id: String,
    calendar_day: String,
    calendar_label: String,
    reason_code: String,
}

pub fn record_schedule_skip(path: &str, value: &Value) -> Result<Value, AnalysisError> {
    let input: Skip = request(value)?;
    if ![
        "CALENDAR.CLOSED",
        "CALENDAR.NO_NIGHT_SESSION",
        "CALENDAR.SESSION_CLOSED",
        "CALENDAR.UNAVAILABLE",
        "BUSY",
    ]
    .contains(&input.reason_code.as_str())
    {
        return Err(AnalysisError::new("INVALID_REQUEST", "排程跳过原因无效"));
    }
    let triggered_us = crate::time_us::parse(&input.triggered_at).map_err(AnalysisError::from)?;
    let connection = open_writer(Path::new(path))?;
    immediate(&connection, || {
        connection.execute(
            "INSERT INTO schedule_skip(account_id,channel,triggered_at_us,calendar_id,calendar_day,calendar_label,reason_code)
             VALUES (?1,?2,?3,?4,?5,?6,?7)",
            params![input.account_id,input.channel,triggered_us,input.calendar_id,input.calendar_day,input.calendar_label,input.reason_code],
        ).map_err(database)?;
        Ok(json!({"id":connection.last_insert_rowid()}))
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn connection() -> Connection {
        let connection = Connection::open_in_memory().unwrap();
        connection.execute_batch(
            "CREATE TABLE account(id INTEGER PRIMARY KEY,name TEXT,market TEXT,trade_channel TEXT,
             brokerage TEXT,remark TEXT,portfolio_id INTEGER,copied_from_account_id INTEGER,
             copied_from_account_name TEXT,updated_at_us INTEGER NOT NULL,created_at_us INTEGER NOT NULL);
             CREATE TABLE account_settings(account_id INTEGER,connection TEXT,execution TEXT,
             schedule TEXT,notification TEXT,backtest TEXT);
             CREATE TABLE portfolioaccount(id INTEGER PRIMARY KEY,account_id INTEGER,portfolio_id INTEGER);
             CREATE TABLE executerecord(id INTEGER PRIMARY KEY,account_id INTEGER,created_at_us INTEGER,result_staged INTEGER DEFAULT 0);
             CREATE TABLE accountruntimesync(id INTEGER PRIMARY KEY,account_id INTEGER UNIQUE,revision INTEGER,
             status TEXT,reset_worker INTEGER,create_request_key TEXT UNIQUE,attempts INTEGER,last_error TEXT,
             requested_at_us INTEGER,last_attempt_at_us INTEGER,synchronized_at_us INTEGER);
             CREATE TABLE accountruntimesyncattempt(id INTEGER PRIMARY KEY,account_id INTEGER,revision INTEGER,
             succeeded INTEGER,error TEXT,attempted_at_us INTEGER);
             INSERT INTO account(id,name,trade_channel,updated_at_us,created_at_us)
             VALUES (1,'first','ctp',1792717200000000,1792717200000000);
             INSERT INTO account_settings VALUES (1,'{}','{}','{\"is_started\":true}','{}','{}');"
        ).unwrap();
        connection
    }

    #[test]
    fn enqueue_preserves_reset_key_and_caller_transaction() {
        let connection = connection();
        connection.execute_batch("BEGIN IMMEDIATE").unwrap();
        enqueue_on(
            &connection,
            1,
            true,
            Some("create-1"),
            "2026-10-23T01:00:00.000000Z",
        )
        .unwrap();
        let result = enqueue_on(
            &connection,
            1,
            false,
            Some("ignored"),
            "2026-10-23T02:00:00.000000Z",
        )
        .unwrap();
        assert_eq!(result["revision"], 2);
        assert_eq!(result["reset_worker"], true);
        assert_eq!(result["create_request_key"], "create-1");
        assert!(!connection.is_autocommit());
        connection.execute_batch("ROLLBACK").unwrap();
        assert!(get_on(&connection, 1).unwrap().is_none());
    }

    #[test]
    fn instants_are_integer_microseconds_and_invalid_input_does_not_write() {
        let connection = connection();
        let saved = enqueue_on(
            &connection,
            1,
            false,
            None,
            "2026-10-23T09:00:00.000001+08:00",
        )
        .unwrap();
        assert_eq!(saved["requested_at"], "2026-10-23T01:00:00.000001Z");
        let stored: (String, i64) = connection
            .query_row(
                "SELECT typeof(requested_at_us),requested_at_us FROM accountruntimesync",
                [],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .unwrap();
        assert_eq!(stored, ("integer".into(), 1_792_717_200_000_001));
        assert!(enqueue_on(&connection, 1, true, None, "invalid").is_err());
        assert_eq!(get_on(&connection, 1).unwrap().unwrap()["revision"], 1);
    }

    #[test]
    fn old_success_failure_and_pending_only_update_their_audit() {
        for status in [Status::Synchronized, Status::Failed, Status::Pending] {
            let connection = connection();
            enqueue_on(&connection, 1, false, None, "2026-10-23T01:00:00.000000Z").unwrap();
            connection
                .execute(
                    "INSERT INTO accountruntimesyncattempt VALUES (1,1,1,0,NULL,1792717200000000)",
                    [],
                )
                .unwrap();
            enqueue_on(&connection, 1, true, None, "2026-10-23T02:00:00.000000Z").unwrap();
            let succeeded = matches!(status, Status::Synchronized);
            let current = finish_on(
                &connection,
                &Finish {
                    account_id: 1,
                    revision: 1,
                    attempt_id: 1,
                    status,
                    error: Some("old result".into()),
                    now: "2026-10-23T04:00:00.000000Z".into(),
                },
            )
            .unwrap();
            assert_eq!(current["revision"], 2);
            assert_eq!(current["reset_worker"], true);
            assert_eq!(current["status"], "pending");
            assert_eq!(current["last_error"], Value::Null);
            assert_eq!(
                connection
                    .query_row(
                        "SELECT succeeded FROM accountruntimesyncattempt",
                        [],
                        |row| row.get::<_, bool>(0)
                    )
                    .unwrap(),
                succeeded
            );
        }
    }

    #[test]
    fn finish_success_clears_reset_only_for_its_revision() {
        let connection = connection();
        enqueue_on(&connection, 1, true, None, "2026-10-23T01:00:00.000000Z").unwrap();
        connection
            .execute(
                "INSERT INTO accountruntimesyncattempt VALUES (1,1,1,0,NULL,1792717200000000)",
                [],
            )
            .unwrap();
        let result = finish_on(
            &connection,
            &Finish {
                account_id: 1,
                revision: 1,
                attempt_id: 1,
                status: Status::Synchronized,
                error: None,
                now: "2026-10-23T04:00:00.000000Z".into(),
            },
        )
        .unwrap();
        assert_eq!(result["status"], "synchronized");
        assert_eq!(result["reset_worker"], false);
        assert_eq!(result["synchronized_at"], "2026-10-23T04:00:00.000000Z");
    }

    #[test]
    fn begin_commits_audit_and_returns_latest_binding_and_execution() {
        let connection = connection();
        connection
            .execute_batch(
                "INSERT INTO portfolioaccount VALUES (1,1,5),(2,1,NULL);
            INSERT INTO executerecord(id,account_id,created_at_us) VALUES (1,1,1792731600000000),(2,1,1792735200000000);",
            )
            .unwrap();
        let target = immediate(&connection, || {
            begin_on(
                &connection,
                &Begin {
                    account_id: 1,
                    scheduled: false,
                    now: "2026-10-23T03:00:00.000000Z".into(),
                },
            )
        })
        .unwrap();
        assert!(connection.is_autocommit());
        assert_eq!(target["account"]["name"], "first");
        assert_eq!(target["portfolio_id"], Value::Null);
        assert_eq!(target["last_execution"], "2026-10-23T06:00:00.000000Z");
        assert_eq!(target["attempt_id"], 1);
        assert_eq!(
            connection
                .query_row(
                    "SELECT count(*) FROM accountruntimesyncattempt WHERE succeeded=0",
                    [],
                    |row| row.get::<_, i64>(0)
                )
                .unwrap(),
            1
        );
        assert_eq!(get_on(&connection, 1).unwrap().unwrap()["attempts"], 1);
    }

    #[test]
    fn stale_scheduled_candidate_does_not_create_sync_or_attempt() {
        let connection = connection();
        connection
            .execute(
                "UPDATE account_settings SET schedule='{\"is_started\":false}'",
                [],
            )
            .unwrap();
        let target = immediate(&connection, || {
            begin_on(
                &connection,
                &Begin {
                    account_id: 1,
                    scheduled: true,
                    now: "2026-10-23T03:00:00.000000Z".into(),
                },
            )
        })
        .unwrap();
        assert_eq!(target, Value::Null);
        assert!(get_on(&connection, 1).unwrap().is_none());
        assert_eq!(
            connection
                .query_row(
                    "SELECT count(*) FROM accountruntimesyncattempt",
                    [],
                    |row| row.get::<_, i64>(0)
                )
                .unwrap(),
            0
        );
    }
}
