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
        "requested_at": row.get::<_, String>(8)?,
        "last_attempt_at": row.get::<_, Option<String>>(9)?,
        "synchronized_at": row.get::<_, Option<String>>(10)?,
    }))
}

pub(crate) fn get_on(
    connection: &Connection,
    account_id: i64,
) -> Result<Option<Value>, AnalysisError> {
    connection
        .query_row(
            "SELECT id,account_id,revision,status,reset_worker,create_request_key,attempts,
             last_error,requested_at,last_attempt_at,synchronized_at
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
            "SELECT s.status,s.revision,s.attempts,s.last_error,s.last_attempt_at,s.synchronized_at FROM account a LEFT JOIN accountruntimesync s ON s.account_id=a.id WHERE a.id=?1",
            [account_id], |row| Ok(json!({"status":row.get::<_,Option<String>>(0)?,"revision":row.get::<_,Option<i64>>(1)?,"attempts":row.get::<_,Option<i64>>(2)?,"last_error":row.get::<_,Option<String>>(3)?,"last_attempt_at":row.get::<_,Option<String>>(4)?,"synchronized_at":row.get::<_,Option<String>>(5)?})),
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
    connection
        .query_row(
            "INSERT INTO accountruntimesync
             (account_id,revision,status,reset_worker,create_request_key,attempts,requested_at)
             VALUES (?1,1,'pending',?2,?3,0,?4)
             ON CONFLICT(account_id) DO UPDATE SET
             revision=revision+1,status='pending',reset_worker=reset_worker OR excluded.reset_worker,
             last_error=NULL,requested_at=excluded.requested_at,synchronized_at=NULL
             RETURNING id,account_id,revision,status,reset_worker,create_request_key,attempts,
             last_error,requested_at,last_attempt_at,synchronized_at",
            params![account_id, reset_worker, create_request_key, now],
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
    connection
        .execute(
            "INSERT INTO accountruntimesync
             (account_id,revision,status,reset_worker,attempts,requested_at)
             VALUES (?1,1,'pending',0,0,?2) ON CONFLICT(account_id) DO NOTHING",
            params![input.account_id, input.now],
        )
        .map_err(database)?;
    let (revision, reset_worker, portfolio_id, last_execution) = connection
        .query_row(
            "UPDATE accountruntimesync SET attempts=attempts+1,last_attempt_at=?2,
             status='pending',last_error=NULL,synchronized_at=NULL WHERE account_id=?1
             RETURNING revision,reset_worker,
             (SELECT portfolio_id FROM portfolioaccount WHERE account_id=?1 ORDER BY id DESC LIMIT 1),
             (SELECT created_at FROM executerecord WHERE account_id=?1 ORDER BY id DESC LIMIT 1)",
            params![input.account_id, input.now],
            |row| Ok((row.get::<_, i64>(0)?, row.get::<_, bool>(1)?,
                row.get::<_, Option<i64>>(2)?, row.get::<_, Option<String>>(3)?)),
        )
        .map_err(database)?;
    // 开始即留审计，取消或进程退出也不会丢失已提交的尝试。
    connection
        .execute(
            "INSERT INTO accountruntimesyncattempt(account_id,revision,succeeded,error,attempted_at)
             VALUES (?1,?2,0,NULL,?3)",
            params![input.account_id, revision, input.now],
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
    let succeeded = matches!(input.status, Status::Synchronized);
    let changed = connection
        .execute(
            "UPDATE accountruntimesyncattempt SET succeeded=?4,error=?5,attempted_at=?6
             WHERE id=?1 AND account_id=?2 AND revision=?3",
            params![
                input.attempt_id,
                input.account_id,
                input.revision,
                succeeded,
                input.error,
                input.now
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
             synchronized_at=CASE WHEN ?5 THEN ?6 ELSE synchronized_at END
             WHERE account_id=?1 AND revision=?2",
            params![
                input.account_id,
                input.revision,
                input.status.as_str(),
                input.error,
                succeeded,
                input.now
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
    let connection = open_writer(Path::new(path))?;
    immediate(&connection, || {
        connection.execute(
            "INSERT INTO schedule_skip(account_id,channel,triggered_at,calendar_id,calendar_day,calendar_label,reason_code)
             VALUES (?1,?2,?3,?4,?5,?6,?7)",
            params![input.account_id,input.channel,input.triggered_at,input.calendar_id,input.calendar_day,input.calendar_label,input.reason_code],
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
             copied_from_account_name TEXT,updated_at TEXT,created_at TEXT);
             CREATE TABLE account_settings(account_id INTEGER,connection TEXT,execution TEXT,
             schedule TEXT,notification TEXT,backtest TEXT);
             CREATE TABLE portfolioaccount(id INTEGER PRIMARY KEY,account_id INTEGER,portfolio_id INTEGER);
             CREATE TABLE executerecord(id INTEGER PRIMARY KEY,account_id INTEGER,created_at TEXT);
             CREATE TABLE accountruntimesync(id INTEGER PRIMARY KEY,account_id INTEGER UNIQUE,revision INTEGER,
             status TEXT,reset_worker INTEGER,create_request_key TEXT UNIQUE,attempts INTEGER,last_error TEXT,
             requested_at TEXT,last_attempt_at TEXT,synchronized_at TEXT);
             CREATE TABLE accountruntimesyncattempt(id INTEGER PRIMARY KEY,account_id INTEGER,revision INTEGER,
             succeeded INTEGER,error TEXT,attempted_at TEXT);
             INSERT INTO account(id,name,trade_channel) VALUES (1,'first','ctp');
             INSERT INTO account_settings VALUES (1,'{}','{}','{\"is_started\":true}','{}','{}');"
        ).unwrap();
        connection
    }

    #[test]
    fn enqueue_preserves_reset_key_and_caller_transaction() {
        let connection = connection();
        connection.execute_batch("BEGIN IMMEDIATE").unwrap();
        enqueue_on(&connection, 1, true, Some("create-1"), "first").unwrap();
        let result = enqueue_on(&connection, 1, false, Some("ignored"), "second").unwrap();
        assert_eq!(result["revision"], 2);
        assert_eq!(result["reset_worker"], true);
        assert_eq!(result["create_request_key"], "create-1");
        assert!(!connection.is_autocommit());
        connection.execute_batch("ROLLBACK").unwrap();
        assert!(get_on(&connection, 1).unwrap().is_none());
    }

    #[test]
    fn old_success_failure_and_pending_only_update_their_audit() {
        for status in [Status::Synchronized, Status::Failed, Status::Pending] {
            let connection = connection();
            enqueue_on(&connection, 1, false, None, "first").unwrap();
            connection
                .execute(
                    "INSERT INTO accountruntimesyncattempt VALUES (1,1,1,0,NULL,'first')",
                    [],
                )
                .unwrap();
            enqueue_on(&connection, 1, true, None, "second").unwrap();
            let succeeded = matches!(status, Status::Synchronized);
            let current = finish_on(
                &connection,
                &Finish {
                    account_id: 1,
                    revision: 1,
                    attempt_id: 1,
                    status,
                    error: Some("old result".into()),
                    now: "finish".into(),
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
        enqueue_on(&connection, 1, true, None, "first").unwrap();
        connection
            .execute(
                "INSERT INTO accountruntimesyncattempt VALUES (1,1,1,0,NULL,'first')",
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
                now: "finish".into(),
            },
        )
        .unwrap();
        assert_eq!(result["status"], "synchronized");
        assert_eq!(result["reset_worker"], false);
        assert_eq!(result["synchronized_at"], "finish");
    }

    #[test]
    fn begin_commits_audit_and_returns_latest_binding_and_execution() {
        let connection = connection();
        connection
            .execute_batch(
                "INSERT INTO portfolioaccount VALUES (1,1,5),(2,1,NULL);
            INSERT INTO executerecord VALUES (1,1,'earlier'),(2,1,'latest');",
            )
            .unwrap();
        let target = immediate(&connection, || {
            begin_on(
                &connection,
                &Begin {
                    account_id: 1,
                    scheduled: false,
                    now: "begin".into(),
                },
            )
        })
        .unwrap();
        assert!(connection.is_autocommit());
        assert_eq!(target["account"]["name"], "first");
        assert_eq!(target["portfolio_id"], Value::Null);
        assert_eq!(target["last_execution"], "latest");
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
                    now: "begin".into(),
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
