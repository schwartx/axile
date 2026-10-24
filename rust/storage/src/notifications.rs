//! 通知 outbox 的短事务与按完成时间更新的摘要。

use std::path::Path;

use rusqlite::{params, Connection, OptionalExtension};
use serde_json::{json, Map, Value};

use crate::error::AnalysisError;
use crate::writer_scope::{immediate, open_writer};

const CONTROL_WARNING: &str = "account_control.warning";
const UNKNOWN: &str = "服务重启，通知调用结果未知";
const EVENT_COLUMNS: &str =
    "sequence,id,account_id,created_at,event_type,context,status,finished_at,error";

fn database(error: rusqlite::Error) -> AnalysisError {
    AnalysisError::new("NOTIFICATION_STORE", error.to_string())
}

fn invalid(message: impl Into<String>) -> AnalysisError {
    AnalysisError::new("INVALID_REQUEST", message)
}

fn text<'a>(request: &'a Value, key: &str) -> Result<&'a str, AnalysisError> {
    request[key]
        .as_str()
        .filter(|value| !value.is_empty())
        .ok_or_else(|| invalid(format!("{key} must be nonempty text")))
}

fn id(request: &Value, key: &str) -> Result<i64, AnalysisError> {
    request[key]
        .as_i64()
        .filter(|value| *value > 0)
        .ok_or_else(|| invalid(format!("{key} must be a positive integer")))
}

fn optional_text<'a>(request: &'a Value, key: &str) -> Result<Option<&'a str>, AnalysisError> {
    match &request[key] {
        Value::Null => Ok(None),
        Value::String(value) => Ok(Some(value)),
        _ => Err(invalid(format!("{key} must be text or null"))),
    }
}

fn event_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<Value> {
    let raw: String = row.get(5)?;
    let context = crate::json_codec::parse(&raw).map_err(|error| {
        rusqlite::Error::FromSqlConversionFailure(
            5,
            rusqlite::types::Type::Text,
            Box::new(std::io::Error::new(std::io::ErrorKind::InvalidData, error)),
        )
    })?;
    let mut event = json!({
        "sequence": row.get::<_, i64>(0)?, "id": row.get::<_, String>(1)?,
        "account_id": row.get::<_, i64>(2)?, "created_at": row.get::<_, String>(3)?,
        "event_type": row.get::<_, String>(4)?, "context": null,
        "status": row.get::<_, String>(6)?, "finished_at": row.get::<_, Option<String>>(7)?,
        "error": row.get::<_, Option<String>>(8)?
    });
    event["context"] = context;
    Ok(event)
}

fn event(connection: &Connection, sequence: i64) -> Result<Option<Value>, AnalysisError> {
    connection
        .query_row(
            &format!("SELECT {EVENT_COLUMNS} FROM notification_event WHERE sequence=?1"),
            [sequence],
            event_row,
        )
        .optional()
        .map_err(database)
}

/// 在补发、intent 或控制事实调用方的事务内入队；已冻结的上下文不被覆盖。
pub(crate) fn enqueue_in_connection(
    connection: &Connection,
    request: &Value,
) -> Result<Value, AnalysisError> {
    let event_id = text(request, "id")?;
    let account_id = id(request, "account_id")?;
    let event_type = text(request, "event_type")?;
    let created_at = text(request, "created_at")?;
    if !request["context"].is_object() {
        return Err(invalid("context must be an object"));
    }
    let existing = connection
        .query_row(
            &format!("SELECT {EVENT_COLUMNS} FROM notification_event WHERE id=?1"),
            [event_id],
            event_row,
        )
        .optional()
        .map_err(database)?;
    if let Some(existing) = existing {
        if existing["account_id"] != account_id || existing["event_type"] != event_type {
            return Err(invalid(
                "notification identity conflicts with the existing event",
            ));
        }
        return Ok(existing);
    }
    connection
        .execute(
            "INSERT INTO notification_event(id,account_id,created_at,event_type,context,status) \
         VALUES (?1,?2,?3,?4,?5,'pending') ON CONFLICT(id) DO NOTHING",
            params![
                event_id,
                account_id,
                created_at,
                event_type,
                request["context"].to_string()
            ],
        )
        .map_err(database)?;
    let existing = connection
        .query_row(
            &format!("SELECT {EVENT_COLUMNS} FROM notification_event WHERE id=?1"),
            [event_id],
            event_row,
        )
        .map_err(database)?;
    if existing["account_id"] != account_id || existing["event_type"] != event_type {
        return Err(invalid(
            "notification identity conflicts with the existing event",
        ));
    }
    Ok(existing)
}

pub fn claim(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let account_id = id(request, "account_id")?;
    let connection = open_writer(Path::new(path))?;
    immediate(&connection, || {
        let next = connection
            .query_row(
                "SELECT n.sequence,a.name,s.notification FROM notification_event n \
             JOIN account a ON a.id=n.account_id LEFT JOIN account_settings s ON s.account_id=a.id \
             WHERE n.account_id=?1 AND n.status='pending' AND NOT EXISTS \
             (SELECT 1 FROM notification_event c WHERE c.account_id=?1 AND c.status='calling') \
             ORDER BY n.sequence LIMIT 1",
                [account_id],
                |row| {
                    Ok((
                        row.get::<_, i64>(0)?,
                        row.get::<_, String>(1)?,
                        row.get::<_, Option<String>>(2)?,
                    ))
                },
            )
            .optional()
            .map_err(database)?;
        let Some((sequence, name, settings)) = next else {
            return Ok(Value::Null);
        };
        let settings: Value = settings
            .map(|raw| serde_json::from_str(&raw))
            .transpose()
            .map_err(|error| invalid(error.to_string()))?
            .unwrap_or_else(|| json!({}));
        if !settings.is_object() {
            return Err(invalid("notification settings must be an object"));
        }
        let mut current =
            event(&connection, sequence)?.ok_or_else(|| invalid("notification disappeared"))?;
        let context = current["context"]
            .as_object_mut()
            .ok_or_else(|| invalid("notification context must be an object"))?;
        context
            .entry("account")
            .or_insert_with(|| json!({"id":account_id,"name":name}));
        current["code"] = json!(optional_text(&settings, "execution_notification_code")?);
        current["feishu_key"] = json!(optional_text(&settings, "feishu_key")?);
        connection.execute("UPDATE notification_event SET status='calling' WHERE sequence=?1 AND status='pending'", [sequence]).map_err(database)?;
        current["status"] = json!("calling");
        Ok(current)
    })
}

pub(crate) fn record_account_in_connection(
    connection: &Connection,
    request: &Value,
) -> Result<Value, AnalysisError> {
    let account_id = id(request, "account_id")?;
    let finished = text(request, "finished_at")?;
    let event_type = text(request, "event_type")?;
    let ok = request["ok"]
        .as_bool()
        .ok_or_else(|| invalid("ok must be a boolean"))?;
    let execution = optional_text(request, "execution_id")?;
    let error = optional_text(request, "error")?;
    let changed = connection.execute(
        "INSERT INTO account_notification_state(account_id,last_success_at,execution_id, \
         last_attempt_event_type,last_attempt_at,last_attempt_execution_id,last_attempt_ok,last_attempt_error) \
         SELECT id,CASE WHEN ?5 THEN ?2 END,CASE WHEN ?5 THEN ?3 END,?4,?2,?3,?5,?6 \
         FROM account WHERE id=?1 ON CONFLICT(account_id) DO UPDATE SET \
         last_success_at=CASE WHEN ?5 THEN ?2 ELSE last_success_at END, \
         execution_id=CASE WHEN ?5 THEN ?3 ELSE execution_id END, \
         last_attempt_event_type=?4,last_attempt_at=?2,last_attempt_execution_id=?3, \
         last_attempt_ok=?5,last_attempt_error=?6 \
         WHERE last_attempt_at IS NULL OR last_attempt_at<?2",
        params![account_id,finished,execution,event_type,ok,error],
    ).map_err(database)?;
    Ok(json!({"updated":changed==1}))
}

pub fn record_account(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let connection = open_writer(Path::new(path))?;
    immediate(&connection, || {
        record_account_in_connection(&connection, request)
    })
}

fn record_system_in_connection(
    connection: &Connection,
    request: &Value,
) -> Result<Value, AnalysisError> {
    let finished = text(request, "finished_at")?;
    let ok = match &request["ok"] {
        Value::Null => None,
        Value::Bool(ok) => Some(*ok),
        _ => return Err(invalid("ok must be a boolean or null")),
    };
    let account = match &request["account_id"] {
        Value::Null => None,
        _ => Some(id(request, "account_id")?),
    };
    let changed = connection.execute(
        "INSERT INTO system_notification_state(id,finished_at,mode,event_type,execution_id,account_id,ok,error) \
         VALUES(1,?1,?2,?3,?4,?5,?6,?7) ON CONFLICT(id) DO UPDATE SET \
         finished_at=excluded.finished_at,mode=excluded.mode,event_type=excluded.event_type, \
         execution_id=excluded.execution_id,account_id=excluded.account_id,ok=excluded.ok,error=excluded.error \
         WHERE system_notification_state.finished_at IS NULL OR system_notification_state.finished_at<excluded.finished_at",
        params![finished,optional_text(request,"mode")?,optional_text(request,"event_type")?,optional_text(request,"execution_id")?,account,ok,optional_text(request,"error")?],
    ).map_err(database)?;
    Ok(json!({"updated":changed==1}))
}

pub fn record_system(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let connection = open_writer(Path::new(path))?;
    immediate(&connection, || {
        record_system_in_connection(&connection, request)
    })
}

pub fn get_system(path: &str, _request: &Value) -> Result<Value, AnalysisError> {
    crate::with_read_snapshot(Path::new(path), |connection| {
        connection.query_row(
            "SELECT finished_at,mode,event_type,execution_id,account_id,ok,error FROM system_notification_state WHERE id=1",
            [], |row| Ok(json!({"finished_at":row.get::<_,Option<String>>(0)?,"mode":row.get::<_,Option<String>>(1)?,"event_type":row.get::<_,Option<String>>(2)?,"execution_id":row.get::<_,Option<String>>(3)?,"account_id":row.get::<_,Option<i64>>(4)?,"ok":row.get::<_,Option<bool>>(5)?,"error":row.get::<_,Option<String>>(6)?})),
        ).optional().map_err(database).map(|row| row.unwrap_or_else(|| json!({})))
    })
}

/// 只终结仍在 calling 的事件；发送摘要由入口在这次提交之后独立保存。
pub(crate) fn finish_in_connection(
    connection: &Connection,
    request: &Value,
) -> Result<Value, AnalysisError> {
    let sequence = id(request, "sequence")?;
    let finished = text(request, "finished_at")?;
    let ok = request["ok"]
        .as_bool()
        .ok_or_else(|| invalid("ok must be a boolean"))?;
    let error = optional_text(request, "error")?;
    connection.query_row(
        "UPDATE notification_event SET status=?2,finished_at=?3,error=?4 WHERE sequence=?1 AND status='calling' \
         RETURNING sequence,account_id,event_type,status,finished_at,error",
        params![sequence,if ok {"succeeded"} else {"failed"},finished,error],
        result_row,
    ).optional().map_err(database).map(|row|row.unwrap_or(Value::Null))
}

fn result_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<Value> {
    Ok(
        json!({"sequence":row.get::<_,i64>(0)?,"account_id":row.get::<_,i64>(1)?,
        "event_type":row.get::<_,String>(2)?,"status":row.get::<_,String>(3)?,
        "finished_at":row.get::<_,Option<String>>(4)?,"error":row.get::<_,Option<String>>(5)?}),
    )
}

fn execution_id(connection: &Connection, event: &Value) -> Result<Option<String>, AnalysisError> {
    connection.query_row(
        "SELECT CASE WHEN event_type='account_control.warning' THEN json_extract(context,'$.execution_id') \
         ELSE json_extract(context,'$.execution.id') END FROM notification_event WHERE sequence=?1",
        [event["sequence"].as_i64().unwrap()], |row|row.get::<_,Option<String>>(0),
    ).optional().map_err(database).map(Option::flatten)
}

fn account_result(event: &Value, execution_id: Option<&str>, ok: bool) -> Value {
    json!({"account_id":event["account_id"],"execution_id":execution_id,
        "event_type":event["event_type"],"finished_at":event["finished_at"],"ok":ok,"error":event["error"]})
}

pub fn finish(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let record_summary = match &request["record_summary"] {
        Value::Null => true,
        Value::Bool(value) => *value,
        _ => return Err(invalid("record_summary must be a boolean")),
    };
    let connection = open_writer(Path::new(path))?;
    let event = immediate(&connection, || finish_in_connection(&connection, request))?;
    if event.is_null() {
        return Ok(json!({"updated":false}));
    }
    let mut result = json!({"updated":true,"status":event["status"],"finished_at":event["finished_at"],"summary_error":null});
    if record_summary && event["event_type"] != CONTROL_WARNING {
        if let Err(error) = immediate(&connection, || {
            let execution_id = execution_id(&connection, &event)?;
            record_account_in_connection(
                &connection,
                &account_result(
                    &event,
                    execution_id.as_deref(),
                    request["ok"].as_bool().unwrap(),
                ),
            )
        }) {
            result["summary_error"] = json!(error.error);
        }
    }
    Ok(result)
}

pub fn recover(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let finished = text(request, "finished_at")?;
    let connection = open_writer(Path::new(path))?;
    let (unknown, accounts) = immediate(&connection, || {
        let mut statement = connection.prepare("SELECT sequence,account_id,event_type,status,finished_at,error FROM notification_event WHERE status='calling' ORDER BY sequence").map_err(database)?;
        let rows = statement
            .query_map([], result_row)
            .map_err(database)?
            .collect::<rusqlite::Result<Vec<_>>>()
            .map_err(database)?;
        connection.execute("UPDATE notification_event SET status='unknown',finished_at=?1,error=?2 WHERE status='calling'",params![finished,UNKNOWN]).map_err(database)?;
        let mut statement = connection.prepare("SELECT DISTINCT n.account_id FROM notification_event n JOIN account a ON a.id=n.account_id WHERE n.status='pending' ORDER BY n.account_id").map_err(database)?;
        let accounts = statement
            .query_map([], |row| row.get::<_, i64>(0))
            .map_err(database)?
            .collect::<rusqlite::Result<Vec<_>>>()
            .map_err(database)?;
        Ok((rows, accounts))
    })?;
    let mut summary_errors = Vec::new();
    for mut event in unknown {
        event["finished_at"] = json!(finished);
        event["error"] = json!(UNKNOWN);
        let result = immediate(&connection, || {
            let execution_id = execution_id(&connection, &event)?;
            if event["event_type"] == CONTROL_WARNING {
                record_system_in_connection(
                    &connection,
                    &json!({"finished_at":finished,"event_type":event["event_type"],"execution_id":execution_id,"account_id":event["account_id"],"ok":null,"error":UNKNOWN}),
                )
            } else {
                record_account_in_connection(
                    &connection,
                    &account_result(&event, execution_id.as_deref(), false),
                )
            }
        });
        if let Err(error) = result {
            summary_errors.push(error.error);
        }
    }
    Ok(json!({"account_ids":accounts,"summary_errors":summary_errors}))
}

pub(crate) fn execution_detail(
    connection: &Connection,
    account_id: i64,
    record_id: i64,
) -> Result<Value, AnalysisError> {
    let result = crate::business_detail::read_result(connection, account_id, record_id)
        .map_err(AnalysisError::from)?;
    let needs_input = !result["extra"]["server_notification_context"]
        .as_object()
        .is_some_and(|context| !context.is_empty())
        && result.get("inputs").is_none();
    let mut detail = Map::new();
    detail.insert("result".into(), result);
    let input = if needs_input {
        let record =
            crate::business_detail::load(connection, "executerecord", "id", &json!(record_id))
                .map_err(AnalysisError::from)?
                .into_iter()
                .next()
                .ok_or_else(|| invalid("execution record does not exist"))?;
        if record.get("input_id").is_none_or(Value::is_null) {
            Value::Null
        } else {
            crate::business_detail::input(connection, &record["input_id"], &record)
                .map_err(AnalysisError::from)?
        }
    } else {
        Value::Null
    };
    detail.insert("input".into(), input);
    Ok(Value::Object(detail))
}

pub fn preview(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let account_id = id(request, "account_id")?;
    crate::with_read_snapshot(Path::new(path), |connection| {
        let record = connection.query_row("SELECT id,execution_id,created_at FROM executerecord WHERE account_id=?1 ORDER BY id DESC LIMIT 1",[account_id],|row| Ok(json!({"id":row.get::<_,i64>(0)?,"execution_id":row.get::<_,Option<String>>(1)?,"created_at":row.get::<_,String>(2)?}))).optional().map_err(database)?;
        let Some(record) = record else {
            return Ok(Value::Null);
        };
        if let Some(execution_id) = record["execution_id"]
            .as_str()
            .filter(|value| !value.is_empty())
        {
            let raw = connection
                .query_row(
                    "SELECT context FROM notification_event WHERE account_id=?1 AND id=?2",
                    params![account_id, format!("execution:{execution_id}")],
                    |row| row.get::<_, String>(0),
                )
                .optional()
                .map_err(database)?;
            if let Some(raw) = raw {
                let context: Value = crate::json_codec::parse(&raw).map_err(invalid)?;
                let object = context
                    .as_object()
                    .ok_or_else(|| invalid("notification context must be an object"))?;
                if !object.is_empty() {
                    let mut preview = json!({"record":record,"context":null});
                    preview["context"] = context;
                    return Ok(preview);
                }
            }
        }
        let detail =
            crate::business_detail::read(connection, account_id, record["id"].as_i64().unwrap())
                .map_err(AnalysisError::from)?;
        Ok(json!({"record":record,"detail":detail}))
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};

    struct Database(String);

    impl Database {
        fn new() -> Self {
            static NEXT: AtomicU64 = AtomicU64::new(0);
            let path = std::env::temp_dir()
                .join(format!(
                    "axile-notifications-{}-{}.sqlite",
                    std::process::id(),
                    NEXT.fetch_add(1, Ordering::Relaxed)
                ))
                .to_string_lossy()
                .into_owned();
            let connection = Connection::open(&path).unwrap();
            connection.execute_batch(
                "PRAGMA foreign_keys=ON;
                 CREATE TABLE account(id INTEGER PRIMARY KEY,name TEXT NOT NULL);
                 INSERT INTO account VALUES(1,'current'),(2,'second');
                 CREATE TABLE account_settings(account_id INTEGER PRIMARY KEY,notification JSON NOT NULL);
                 INSERT INTO account_settings VALUES(1,'{\"execution_notification_code\":\"code\",\"feishu_key\":\"key\"}');
                 CREATE TABLE notification_event(sequence INTEGER PRIMARY KEY,id TEXT NOT NULL UNIQUE,
                    account_id INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL,event_type TEXT NOT NULL,context JSON NOT NULL,
                    status TEXT NOT NULL,finished_at TEXT,error TEXT);
                 CREATE TABLE account_notification_state(account_id INTEGER PRIMARY KEY REFERENCES account(id) ON DELETE CASCADE,
                    last_success_at TEXT,execution_id TEXT,last_attempt_event_type TEXT,last_attempt_at TEXT,
                    last_attempt_execution_id TEXT,last_attempt_ok BOOLEAN,last_attempt_error TEXT);
                 CREATE TABLE system_notification_state(id INTEGER PRIMARY KEY,finished_at TEXT,mode TEXT,event_type TEXT,
                    execution_id TEXT,account_id INTEGER,ok BOOLEAN,error TEXT);
                 CREATE TABLE executerecord(id INTEGER PRIMARY KEY,account_id INTEGER,execution_id TEXT,created_at TEXT);"
            ).unwrap();
            Self(path)
        }

        fn add(&self, event_id: &str, event_type: &str) -> Value {
            let connection = open_writer(Path::new(&self.0)).unwrap();
            immediate(&connection, || enqueue_in_connection(&connection,&json!({
                "id":event_id,"account_id":1,"event_type":event_type,"created_at":"2026-10-23T10:00:00",
                "context":{"execution":{"id":event_id},"account":{"id":1,"name":"frozen"}}
            }))).unwrap()
        }

        fn state(&self) -> Value {
            let connection = Connection::open(&self.0).unwrap();
            connection.query_row("SELECT last_success_at,execution_id,last_attempt_at,last_attempt_execution_id,last_attempt_ok,last_attempt_error FROM account_notification_state WHERE account_id=1",[],|row| Ok(json!({
                "success_at":row.get::<_,Option<String>>(0)?,"execution_id":row.get::<_,Option<String>>(1)?,
                "attempt_at":row.get::<_,String>(2)?,"attempt_execution_id":row.get::<_,Option<String>>(3)?,
                "ok":row.get::<_,bool>(4)?,"error":row.get::<_,Option<String>>(5)?
            }))).unwrap()
        }
    }

    impl Drop for Database {
        fn drop(&mut self) {
            for suffix in ["", "-wal", "-shm"] {
                let _ = std::fs::remove_file(format!("{}{suffix}", self.0));
            }
        }
    }

    #[test]
    fn notification_context_preserves_python_nonfinite_numbers() {
        let db = Database::new();
        let connection = open_writer(Path::new(&db.0)).unwrap();
        let context = crate::json_codec::parse(r#"{"assets":{"total_assets":NaN,"available_cash":Infinity},"execution":{"id":"nonfinite"}}"#).unwrap();
        let mut request = json!({"id":"execution:nonfinite","account_id":1,"event_type":"execution.finished","created_at":"now","context":null});
        request["context"] = context;
        let event =
            immediate(&connection, || enqueue_in_connection(&connection, &request)).unwrap();
        assert_eq!(
            event["context"]["assets"]["total_assets"].to_string(),
            "NaN"
        );
        let stored = crate::business_detail::load(
            &connection,
            "notification_event",
            "id",
            &json!("execution:nonfinite"),
        )
        .unwrap();
        assert_eq!(
            stored[0]["context"]["assets"]["available_cash"].to_string(),
            "Infinity"
        );
        let claimed = claim(&db.0, &json!({"account_id":1})).unwrap();
        assert_eq!(
            claimed["context"]["assets"]["total_assets"].to_string(),
            "NaN"
        );
    }

    #[test]
    fn claims_commit_calling_and_block_later_events_until_terminal() {
        let db = Database::new();
        let first = db.add("first", "execution.finished");
        let second = db.add("second", "supplement.cancelled");
        let claimed = claim(&db.0, &json!({"account_id":1})).unwrap();
        assert_eq!(claimed["sequence"], first["sequence"]);
        assert_eq!(claimed["context"]["account"]["name"], "frozen");
        assert_eq!(claimed["code"], "code");
        assert_eq!(
            event(
                &Connection::open(&db.0).unwrap(),
                first["sequence"].as_i64().unwrap()
            )
            .unwrap()
            .unwrap()["status"],
            "calling"
        );
        assert!(claim(&db.0, &json!({"account_id":1})).unwrap().is_null());
        finish(&db.0,&json!({"sequence":first["sequence"],"finished_at":"2026-10-23T10:01:00","ok":false,"error":"failed"})).unwrap();
        assert_eq!(
            claim(&db.0, &json!({"account_id":1})).unwrap()["sequence"],
            second["sequence"]
        );
        recover(&db.0, &json!({"finished_at":"2026-10-23T10:02:00"})).unwrap();
        assert!(claim(&db.0, &json!({"account_id":1})).unwrap().is_null());
        assert_eq!(db.state()["attempt_execution_id"], "second");
        assert_eq!(db.state()["error"], UNKNOWN);
        assert_eq!(finish(&db.0,&json!({"sequence":second["sequence"],"finished_at":"2026-10-23T10:03:00","ok":true})).unwrap()["updated"],false);
    }

    #[test]
    fn concurrent_dispatchers_cannot_call_the_same_account_out_of_order() {
        use std::sync::{Arc, Barrier};
        let db = Database::new();
        let first = db.add("first", "execution.finished");
        db.add("second", "execution.finished");
        let barrier = Arc::new(Barrier::new(3));
        let workers = (0..2)
            .map(|_| {
                let path = db.0.clone();
                let barrier = barrier.clone();
                std::thread::spawn(move || {
                    barrier.wait();
                    claim(&path, &json!({"account_id":1})).unwrap()
                })
            })
            .collect::<Vec<_>>();
        barrier.wait();
        let results = workers
            .into_iter()
            .map(|worker| worker.join().unwrap())
            .filter(|row| !row.is_null())
            .collect::<Vec<_>>();
        assert_eq!(results.len(), 1);
        assert_eq!(results[0]["sequence"], first["sequence"]);
    }

    #[test]
    fn account_summary_uses_completion_cas_and_never_recreates_deleted_account() {
        let db = Database::new();
        let save = |execution: &str, time: &str, ok: bool| {
            record_account(&db.0,&json!({"account_id":1,"execution_id":execution,"event_type":"execution.finished","finished_at":time,"ok":ok,"error":if ok {None} else {Some("failed")}})).unwrap()
        };
        save("success", "2026-10-23T10:00:00", true);
        save("failure", "2026-10-23T11:00:00", false);
        save("old", "2026-10-23T09:00:00", true);
        assert_eq!(
            db.state(),
            json!({"success_at":"2026-10-23T10:00:00","execution_id":"success","attempt_at":"2026-10-23T11:00:00","attempt_execution_id":"failure","ok":false,"error":"failed"})
        );
        let connection = open_writer(Path::new(&db.0)).unwrap();
        connection
            .execute("DELETE FROM account WHERE id=1", [])
            .unwrap();
        assert_eq!(
            save("deleted", "2026-10-23T12:00:00", true)["updated"],
            false
        );
        assert_eq!(
            connection
                .query_row(
                    "SELECT count(*) FROM account_notification_state",
                    [],
                    |row| row.get::<_, i64>(0)
                )
                .unwrap(),
            0
        );
    }

    #[test]
    fn sent_success_stays_terminal_when_summary_write_fails() {
        let db = Database::new();
        let queued = db.add("sent", "execution.finished");
        claim(&db.0, &json!({"account_id":1})).unwrap();
        let connection = Connection::open(&db.0).unwrap();
        connection.execute_batch("CREATE TRIGGER reject_summary BEFORE INSERT ON account_notification_state BEGIN SELECT RAISE(ABORT,'summary unavailable'); END;").unwrap();
        let result = finish(
            &db.0,
            &json!({"sequence":queued["sequence"],"finished_at":"2026-10-23T11:00:00","ok":true}),
        )
        .unwrap();
        assert_eq!(result["status"], "succeeded");
        assert!(result["summary_error"]
            .as_str()
            .unwrap()
            .contains("summary unavailable"));
        assert_eq!(
            event(&connection, queued["sequence"].as_i64().unwrap())
                .unwrap()
                .unwrap()["status"],
            "succeeded"
        );
        assert!(claim(&db.0, &json!({"account_id":1})).unwrap().is_null());
    }

    #[test]
    fn recovery_preserves_newer_summaries_and_routes_warnings_to_system() {
        let db = Database::new();
        db.add("interrupted", "execution.finished");
        claim(&db.0, &json!({"account_id":1})).unwrap();
        record_account(&db.0,&json!({"account_id":1,"execution_id":"newer","event_type":"execution.finished","finished_at":"2026-10-23T12:00:00","ok":true})).unwrap();
        recover(&db.0, &json!({"finished_at":"2026-10-23T11:00:00"})).unwrap();
        assert_eq!(db.state()["attempt_execution_id"], "newer");
        db.add("warning", CONTROL_WARNING);
        claim(&db.0, &json!({"account_id":1})).unwrap();
        db.add("pending", "execution.finished");
        let result = recover(&db.0, &json!({"finished_at":"2026-10-23T13:00:00"})).unwrap();
        assert_eq!(result["account_ids"], json!([1]));
        let system = get_system(&db.0, &json!({})).unwrap();
        assert_eq!(system["event_type"], CONTROL_WARNING);
        assert!(system["ok"].is_null());
        assert_eq!(db.state()["attempt_execution_id"], "newer");
        record_system(
            &db.0,
            &json!({"finished_at":"2026-10-23T10:00:00","ok":true}),
        )
        .unwrap();
        assert_eq!(get_system(&db.0, &json!({})).unwrap(), system);
    }

    #[test]
    fn recovery_unknown_survives_summary_failure() {
        let db = Database::new();
        let warning = db.add("warning", CONTROL_WARNING);
        claim(&db.0, &json!({"account_id":1})).unwrap();
        let connection = Connection::open(&db.0).unwrap();
        connection
            .execute_batch("DROP TABLE system_notification_state;")
            .unwrap();
        let result = recover(&db.0, &json!({"finished_at":"2026-10-23T13:00:00"})).unwrap();
        assert_eq!(result["summary_errors"].as_array().unwrap().len(), 1);
        assert_eq!(
            event(&connection, warning["sequence"].as_i64().unwrap())
                .unwrap()
                .unwrap()["status"],
            "unknown"
        );
        assert!(claim(&db.0, &json!({"account_id":1})).unwrap().is_null());
    }

    #[test]
    fn changed_invalid_context_cannot_rollback_a_successful_send() {
        let db = Database::new();
        let queued = db.add("sent", "execution.finished");
        claim(&db.0, &json!({"account_id":1})).unwrap();
        let connection = Connection::open(&db.0).unwrap();
        connection
            .execute(
                "UPDATE notification_event SET context='invalid JSON' WHERE sequence=?1",
                [queued["sequence"].as_i64().unwrap()],
            )
            .unwrap();
        let result = finish(
            &db.0,
            &json!({"sequence":queued["sequence"],"finished_at":"2026-10-23T13:00:00","ok":true}),
        )
        .unwrap();
        assert_eq!(result["status"], "succeeded");
        assert!(result["summary_error"].is_string());
        assert_eq!(
            connection
                .query_row(
                    "SELECT status FROM notification_event WHERE sequence=?1",
                    [queued["sequence"].as_i64().unwrap()],
                    |row| row.get::<_, String>(0)
                )
                .unwrap(),
            "succeeded"
        );
    }

    #[test]
    fn enqueue_belongs_to_caller_transaction_and_retains_frozen_event() {
        let db = Database::new();
        let existing = db.add("frozen", "execution.finished");
        let connection = open_writer(Path::new(&db.0)).unwrap();
        connection.execute_batch("CREATE TRIGGER frozen BEFORE INSERT ON notification_event WHEN NEW.id='frozen' BEGIN SELECT RAISE(ABORT,'must not reinsert frozen event'); END;").unwrap();
        let request = json!({"id":"frozen","account_id":1,"created_at":"now","event_type":"execution.finished","context":{"edited":true}});
        assert_eq!(
            enqueue_in_connection(&connection, &request).unwrap(),
            existing
        );
        let mut conflict = request.clone();
        conflict["account_id"] = json!(2);
        assert!(enqueue_in_connection(&connection, &conflict).is_err());
        connection.execute_batch("BEGIN IMMEDIATE;").unwrap();
        enqueue_in_connection(&connection,&json!({"id":"rollback","account_id":1,"created_at":"now","event_type":"supplement.cancelled","context":{}})).unwrap();
        connection.execute_batch("ROLLBACK;").unwrap();
        assert_eq!(
            connection
                .query_row(
                    "SELECT count(*) FROM notification_event WHERE id='rollback'",
                    [],
                    |row| row.get::<_, i64>(0)
                )
                .unwrap(),
            0
        );
    }

    #[test]
    fn preview_prefers_frozen_outbox_without_reading_business_facts() {
        let db = Database::new();
        let saved = db.add("execution:preview", "execution.finished");
        let connection = Connection::open(&db.0).unwrap();
        connection
            .execute(
                "INSERT INTO executerecord VALUES(1,1,'preview','2026-10-23T10:00:00')",
                [],
            )
            .unwrap();
        let result = preview(&db.0, &json!({"account_id":1})).unwrap();
        assert_eq!(result["context"], saved["context"]);
        assert_eq!(result["record"]["execution_id"], "preview");
        assert!(result.get("detail").is_none());
        assert!(preview(&db.0, &json!({"account_id":2})).unwrap().is_null());
    }
}
