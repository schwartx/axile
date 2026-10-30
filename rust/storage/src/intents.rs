//! 执行票的持久化边界；补发关联与终态通知沿用调用方事务。

use std::path::Path;

use rusqlite::{
    params, params_from_iter, types::Value as SqlValue, Connection, OptionalExtension, Row,
};
use serde_json::{json, Map, Value};

use crate::error::AnalysisError;
use crate::writer_scope::{immediate, open_writer};

const COLUMNS: &str = "execution_id,account_id,kind,trigger_source,status,channel,algorithm,payload,created_at_us,started_at_us,finished_at_us,error,cancel_requested_at_us,cancel_reason,terminate_mode";
const TERMINAL: [&str; 5] = ["TERMINATED", "SUCCEEDED", "FAILED", "SKIPPED", "PARTIAL"];
const PROCESS_INTERRUPTED_REASON: &str = "上次执行中断，未自动续跑";
const FIELDS: [&str; 6] = [
    "started_at",
    "finished_at",
    "error",
    "cancel_requested_at",
    "cancel_reason",
    "terminate_mode",
];

fn database(error: rusqlite::Error) -> AnalysisError {
    let conflict = matches!(
        &error,
        rusqlite::Error::SqliteFailure(_, Some(message))
            if message == "UNIQUE constraint failed: execution_intent.account_id"
    );
    AnalysisError::new(
        if conflict {
            "INTENT_CONFLICT"
        } else {
            "DATABASE_INTENT"
        },
        error.to_string(),
    )
}

fn text<'a>(request: &'a Value, key: &str) -> Result<&'a str, AnalysisError> {
    request[key]
        .as_str()
        .filter(|value| !value.is_empty())
        .ok_or_else(|| {
            AnalysisError::new(
                "INVALID_REQUEST",
                format!("intent {key} must be a nonempty string"),
            )
        })
}

fn moment(value: &str) -> Result<i64, AnalysisError> {
    crate::time_us::parse(value).map_err(|message| AnalysisError::new("INVALID_REQUEST", message))
}

fn account_id(request: &Value) -> Result<i64, AnalysisError> {
    request["account_id"].as_i64().ok_or_else(|| {
        AnalysisError::new("INVALID_REQUEST", "intent account_id must be an integer")
    })
}

fn nullable_text<'a>(request: &'a Value, key: &str) -> Result<Option<&'a str>, AnalysisError> {
    let value = &request[key];
    if value.is_null() {
        return Ok(None);
    }
    value.as_str().map(Some).ok_or_else(|| {
        AnalysisError::new(
            "INVALID_REQUEST",
            format!("intent {key} must be a string or null"),
        )
    })
}

fn status<'a>(request: &'a Value, key: &str) -> Result<&'a str, AnalysisError> {
    let value = text(request, key)?;
    if !TERMINAL.contains(&value) && !["QUEUED", "RUNNING", "TERMINATING"].contains(&value) {
        return Err(AnalysisError::new(
            "INVALID_REQUEST",
            "unknown intent status",
        ));
    }
    Ok(value)
}

fn decode(row: &Row<'_>) -> rusqlite::Result<Value> {
    let payload: String = row.get(7)?;
    let payload: Value = serde_json::from_str(&payload).map_err(|error| {
        rusqlite::Error::FromSqlConversionFailure(7, rusqlite::types::Type::Text, Box::new(error))
    })?;
    if !payload.is_object() {
        return Err(rusqlite::Error::InvalidColumnType(
            7,
            "payload".into(),
            rusqlite::types::Type::Text,
        ));
    }
    Ok(json!({
        "execution_id": row.get::<_, String>(0)?,
        "account_id": row.get::<_, i64>(1)?,
        "kind": row.get::<_, String>(2)?,
        "trigger_source": row.get::<_, String>(3)?,
        "status": row.get::<_, String>(4)?,
        "channel": row.get::<_, Option<String>>(5)?,
        "algorithm": row.get::<_, Option<String>>(6)?,
        "payload": payload,
        "created_at": crate::time_us::read(row, 8)?,
        "started_at": crate::time_us::read_optional(row, 9)?,
        "finished_at": crate::time_us::read_optional(row, 10)?,
        "error": row.get::<_, Option<String>>(11)?,
        "cancel_requested_at": crate::time_us::read_optional(row, 12)?,
        "cancel_reason": row.get::<_, Option<String>>(13)?,
        "terminate_mode": row.get::<_, Option<String>>(14)?,
    }))
}

pub(crate) fn get_in_connection(
    connection: &Connection,
    execution_id: &str,
) -> Result<Option<Value>, AnalysisError> {
    connection
        .query_row(
            &format!("SELECT {COLUMNS} FROM execution_intent WHERE execution_id=?1"),
            [execution_id],
            decode,
        )
        .optional()
        .map_err(database)
}

pub fn intent_get(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let execution_id = text(request, "execution_id")?;
    crate::with_read_snapshot(Path::new(path), |connection| {
        Ok(get_in_connection(connection, execution_id)?.unwrap_or(Value::Null))
    })
}

fn list_in_connection(
    connection: &Connection,
    filter: &str,
    values: &[SqlValue],
) -> Result<Vec<Value>, AnalysisError> {
    let mut statement = connection
        .prepare(&format!(
            "SELECT {COLUMNS} FROM execution_intent WHERE {filter} ORDER BY id"
        ))
        .map_err(database)?;
    let rows = statement
        .query_map(params_from_iter(values), decode)
        .map_err(database)?;
    rows.collect::<Result<Vec<_>, _>>().map_err(database)
}

pub fn intent_active(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let account = account_id(request)?;
    crate::with_read_snapshot(Path::new(path), |connection| {
        let rows = list_in_connection(
            connection,
            "account_id=?1 AND status IN ('QUEUED','RUNNING','TERMINATING')",
            &[SqlValue::Integer(account)],
        )?;
        Ok(json!({
            "running": rows.iter().find(|row| row["status"] != "QUEUED"),
            "queued": rows.iter().find(|row| row["status"] == "QUEUED"),
        }))
    })
}

pub fn intent_list(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let requested = request["statuses"]
        .as_array()
        .ok_or_else(|| AnalysisError::new("INVALID_REQUEST", "intent statuses must be an array"))?;
    if requested.is_empty() {
        return Ok(json!([]));
    }
    let mut values = Vec::with_capacity(requested.len());
    for value in requested {
        let value = status(&json!({"status": value}), "status")?.to_owned();
        values.push(SqlValue::Text(value));
    }
    let placeholders = vec!["?"; values.len()].join(",");
    crate::with_read_snapshot(Path::new(path), |connection| {
        Ok(json!(list_in_connection(
            connection,
            &format!("status IN ({placeholders})"),
            &values
        )?))
    })
}

fn recovery_in_connection(connection: &Connection) -> Result<Value, AnalysisError> {
    let rows = list_in_connection(
        connection,
        "status IN ('QUEUED','RUNNING','TERMINATING')",
        &[],
    )?;
    let repair = list_in_connection(
        connection,
        "status='FAILED' AND error=?1 AND NOT EXISTS (SELECT 1 FROM executerecord r WHERE r.execution_id=execution_intent.execution_id)",
        &[SqlValue::Text(PROCESS_INTERRUPTED_REASON.into())],
    )?;
    Ok(json!({
        "interrupted": rows.iter().filter(|row| row["status"] != "QUEUED").collect::<Vec<_>>(),
        "queued": rows.iter().filter(|row| row["status"] == "QUEUED").collect::<Vec<_>>(),
        "repair": repair,
    }))
}

pub fn intent_recovery(path: &str, _request: &Value) -> Result<Value, AnalysisError> {
    crate::with_read_snapshot(Path::new(path), recovery_in_connection)
}

fn insert_in_connection(connection: &Connection, request: &Value) -> Result<Value, AnalysisError> {
    let execution_id = text(request, "execution_id")?;
    let account = account_id(request)?;
    let kind = text(request, "kind")?;
    if !["rebalance", "clear_positions"].contains(&kind) || !request["payload"].is_object() {
        return Err(AnalysisError::new(
            "INVALID_REQUEST",
            "invalid intent kind or payload",
        ));
    }
    let payload = serde_json::to_string(&request["payload"])
        .map_err(|error| AnalysisError::new("INVALID_REQUEST", error.to_string()))?;
    connection.execute(
        "INSERT INTO execution_intent(execution_id,account_id,kind,trigger_source,status,channel,algorithm,payload,created_at_us) VALUES(?1,?2,?3,?4,'QUEUED',?5,?6,?7,?8)",
        params![execution_id, account, kind, text(request,"trigger_source")?, nullable_text(request,"channel")?, nullable_text(request,"algorithm")?, payload, moment(text(request,"created_at")?)?],
    ).map_err(database)?;
    crate::supplements::attach_step(
        connection,
        execution_id,
        nullable_text(&request["payload"], "supplement_step_id")?,
        request,
    )?;
    Ok(get_in_connection(connection, execution_id)?.expect("inserted intent"))
}

pub fn intent_insert(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let connection = open_writer(Path::new(path))?;
    immediate(&connection, || insert_in_connection(&connection, request))
}

pub fn intent_coalesce(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let execution_id = text(request, "execution_id")?;
    let account = account_id(request)?;
    if !request["payload"].is_object() {
        return Err(AnalysisError::new(
            "INVALID_REQUEST",
            "intent payload must be an object",
        ));
    }
    let connection = open_writer(Path::new(path))?;
    immediate(&connection, || {
        let row = get_in_connection(&connection, execution_id)?.ok_or_else(|| {
            AnalysisError::new("INTENT_CONFLICT", "queued intent no longer exists")
        })?;
        if row["account_id"] != account {
            return Err(AnalysisError::new(
                "INVALID_REQUEST",
                "intent belongs to another account",
            ));
        }
        if row["status"] != "QUEUED" {
            return Err(AnalysisError::new(
                "INTENT_CONFLICT",
                "intent is no longer queued for this account",
            ));
        }
        crate::supplements::attach_step(
            &connection,
            execution_id,
            nullable_text(&request["payload"], "supplement_step_id")?,
            request,
        )?;
        Ok(get_in_connection(&connection, execution_id)?.expect("coalesced intent"))
    })
}

fn update_in_connection(
    connection: &Connection,
    execution_id: &str,
    expected: &str,
    target: &str,
    fields: &Map<String, Value>,
) -> Result<Option<Value>, AnalysisError> {
    if TERMINAL.contains(&expected) {
        return Ok(None);
    }
    let mut assignments = vec!["status=?".to_owned()];
    let mut values = vec![SqlValue::Text(target.to_owned())];
    for (field, value) in fields {
        if !FIELDS.contains(&field.as_str()) || !(value.is_null() || value.is_string()) {
            return Err(AnalysisError::new(
                "INVALID_REQUEST",
                "invalid intent update field",
            ));
        }
        if field == "terminate_mode"
            && !value.is_null()
            && ![Some("graceful"), Some("cancel_pending")].contains(&value.as_str())
        {
            return Err(AnalysisError::new(
                "INVALID_REQUEST",
                "unknown termination mode",
            ));
        }
        if ["started_at", "finished_at", "cancel_requested_at"].contains(&field.as_str()) {
            assignments.push(format!("{field}_us=?"));
            values.push(match value.as_str() {
                Some(value) => SqlValue::Integer(moment(value)?),
                None => SqlValue::Null,
            });
        } else {
            assignments.push(format!("{field}=?"));
            values.push(
                value
                    .as_str()
                    .map_or(SqlValue::Null, |value| SqlValue::Text(value.to_owned())),
            );
        }
    }
    values.push(SqlValue::Text(execution_id.to_owned()));
    values.push(SqlValue::Text(expected.to_owned()));
    let changed = connection
        .execute(
            &format!(
                "UPDATE execution_intent SET {} WHERE execution_id=? AND status=?",
                assignments.join(",")
            ),
            params_from_iter(values),
        )
        .map_err(database)?;
    if changed == 0 {
        return Ok(None);
    }
    let row = get_in_connection(connection, execution_id)?.expect("updated intent");
    Ok(Some(row))
}

pub fn intent_cas(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let execution_id = text(request, "execution_id")?;
    let expected = status(request, "expected")?;
    let target = status(request, "target")?;
    let fields = request["fields"]
        .as_object()
        .ok_or_else(|| AnalysisError::new("INVALID_REQUEST", "intent fields must be an object"))?;
    let connection = open_writer(Path::new(path))?;
    immediate(&connection, || {
        let row = update_in_connection(&connection, execution_id, expected, target, fields)?;
        if let Some(row) = row.as_ref().filter(|_| TERMINAL.contains(&target)) {
            crate::supplements::finish_supplement_execution(&connection, row, request)?;
        }
        Ok(json!({"changed": row.is_some(), "intent": row}))
    })
}

pub(crate) fn finish_in_connection(
    connection: &Connection,
    request: &Value,
) -> Result<Value, AnalysisError> {
    let execution_id = text(request, "execution_id")?;
    let target = status(request, "status")?;
    if !TERMINAL.contains(&target) {
        return Err(AnalysisError::new(
            "INVALID_REQUEST",
            "finish requires a terminal status",
        ));
    }
    let Some(current) = get_in_connection(connection, execution_id)? else {
        return Ok(json!({"changed": false, "intent": null}));
    };
    if request.get("account_id").is_some() && account_id(request)? != account_id(&current)? {
        return Err(AnalysisError::new(
            "INVALID_REQUEST",
            "执行标识属于其他账户",
        ));
    }
    let mut fields = Map::new();
    fields.insert("finished_at".into(), json!(text(request, "finished_at")?));
    if !request["error"].is_null() {
        fields.insert("error".into(), request["error"].clone());
    }
    let updated = update_in_connection(
        connection,
        execution_id,
        current["status"].as_str().expect("stored status"),
        target,
        &fields,
    )?;
    if let Some(row) = &updated {
        crate::supplements::finish_supplement_execution(connection, row, request)?;
    }
    Ok(json!({"changed": updated.is_some(), "intent": updated.unwrap_or(current)}))
}

pub fn intent_finish(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let connection = open_writer(Path::new(path))?;
    immediate(&connection, || finish_in_connection(&connection, request))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn database() -> Connection {
        let connection = Connection::open_in_memory().unwrap();
        connection.execute_batch(
            "CREATE TABLE execution_intent (
                id INTEGER PRIMARY KEY, execution_id TEXT UNIQUE NOT NULL,
                account_id INTEGER NOT NULL, kind TEXT NOT NULL, trigger_source TEXT NOT NULL,
                status TEXT NOT NULL, channel TEXT, algorithm TEXT, payload TEXT NOT NULL,
                created_at_us INTEGER NOT NULL, started_at_us INTEGER, finished_at_us INTEGER, error TEXT,
                cancel_requested_at_us INTEGER, cancel_reason TEXT, terminate_mode TEXT
             );
             CREATE UNIQUE INDEX uq_intent_account_queued ON execution_intent(account_id) WHERE status='QUEUED';
             CREATE UNIQUE INDEX uq_intent_account_running ON execution_intent(account_id) WHERE status IN ('RUNNING','TERMINATING');
             CREATE TABLE supplement_step (id TEXT PRIMARY KEY, group_id TEXT, [index] INTEGER,
                scheduled_at_us INTEGER, status TEXT, execution_id TEXT);
             CREATE TABLE executerecord (id INTEGER PRIMARY KEY, execution_id TEXT UNIQUE);
             CREATE TABLE notification_event (id TEXT PRIMARY KEY);",
        ).unwrap();
        connection
    }

    fn request(execution_id: &str) -> Value {
        json!({
            "execution_id": execution_id, "account_id": 1, "kind": "rebalance",
            "trigger_source": "manual", "channel": "ctp", "algorithm": "SINGLE-MAKER",
            "payload": {}, "created_at": "2026-10-23T10:00:00+08:00",
            "now": "2026-10-23T10:00:00+08:00"
        })
    }

    #[test]
    fn account_has_one_queued_and_one_running_intent() {
        let connection = database();
        immediate(&connection, || {
            insert_in_connection(&connection, &request("one"))
        })
        .unwrap();
        let error = immediate(&connection, || {
            insert_in_connection(&connection, &request("two"))
        })
        .unwrap_err();
        assert_eq!(error.code, "INTENT_CONFLICT");
        let empty = Map::new();
        immediate(&connection, || {
            update_in_connection(&connection, "one", "QUEUED", "RUNNING", &empty)
        })
        .unwrap();
        immediate(&connection, || {
            insert_in_connection(&connection, &request("two"))
        })
        .unwrap();
        let error = immediate(&connection, || {
            update_in_connection(&connection, "two", "QUEUED", "RUNNING", &empty)
        })
        .unwrap_err();
        assert_eq!(error.code, "INTENT_CONFLICT");
        assert_eq!(
            get_in_connection(&connection, "two").unwrap().unwrap()["status"],
            "QUEUED"
        );
    }

    #[test]
    fn invalid_supplement_rolls_back_the_new_intent() {
        let connection = database();
        let mut request = request("invalid");
        request["payload"] = json!({"supplement_step_id": "missing-step"});
        let error =
            immediate(&connection, || insert_in_connection(&connection, &request)).unwrap_err();
        assert_eq!(error.code, "SUPPLEMENT_NOT_RUNNABLE");
        assert!(get_in_connection(&connection, "invalid").unwrap().is_none());
    }

    #[test]
    fn first_terminal_status_and_fields_win() {
        let connection = database();
        immediate(&connection, || {
            insert_in_connection(&connection, &request("one"))
        })
        .unwrap();
        let finish = json!({"execution_id":"one","status":"FAILED","error":"first",
            "finished_at":"2026-10-23T10:01:00+08:00","now":"2026-10-23T10:01:00+08:00"});
        assert_eq!(
            immediate(&connection, || finish_in_connection(&connection, &finish)).unwrap()
                ["changed"],
            true
        );
        let mut later = finish.clone();
        later["status"] = json!("SUCCEEDED");
        later["error"] = json!("second");
        later["finished_at"] = json!("2026-10-23T10:02:00+08:00");
        let receipt = immediate(&connection, || finish_in_connection(&connection, &later)).unwrap();
        assert_eq!(receipt["changed"], false);
        assert_eq!(receipt["intent"]["status"], "FAILED");
        assert_eq!(receipt["intent"]["error"], "first");
        assert_eq!(
            receipt["intent"]["finished_at"],
            crate::time_us::format(moment(finish["finished_at"].as_str().unwrap()).unwrap())
                .unwrap()
        );
        assert!(
            update_in_connection(&connection, "one", "FAILED", "RUNNING", &Map::new())
                .unwrap()
                .is_none()
        );
    }

    #[test]
    fn integer_time_columns_preserve_microseconds_without_touching_payload_times() {
        let connection = database();
        let mut request = request("precise");
        request["created_at"] = json!("2026-10-23T10:00:00.123456+08:00");
        request["payload"] = json!({"created_at": "historical local text"});
        let inserted = insert_in_connection(&connection, &request).unwrap();
        assert_eq!(inserted["created_at"], "2026-10-23T02:00:00.123456Z");
        assert_eq!(inserted["payload"]["created_at"], "historical local text");
        let (kind, micros): (String, i64) = connection
            .query_row(
                "SELECT typeof(created_at_us),created_at_us FROM execution_intent",
                [],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .unwrap();
        assert_eq!(kind, "integer");
        assert_eq!(micros.rem_euclid(1_000_000), 123456);
        let fields =
            json!({"started_at": "2026-10-23T02:00:00.123457Z", "cancel_requested_at": null});
        let updated = update_in_connection(
            &connection,
            "precise",
            "QUEUED",
            "RUNNING",
            fields.as_object().unwrap(),
        )
        .unwrap()
        .unwrap();
        assert_eq!(updated["started_at"], "2026-10-23T02:00:00.123457Z");
        assert!(updated["cancel_requested_at"].is_null());
        let elapsed: i64 = connection
            .query_row(
                "SELECT started_at_us-created_at_us FROM execution_intent",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(elapsed, 1);
    }

    #[test]
    fn recovery_only_repairs_old_interrupted_failures_without_records() {
        let connection = database();
        for (execution_id, status, error) in [
            ("old-gap", "FAILED", PROCESS_INTERRUPTED_REASON),
            ("record-exists", "FAILED", PROCESS_INTERRUPTED_REASON),
            ("ordinary-failure", "FAILED", "another failure"),
            ("succeeded", "SUCCEEDED", PROCESS_INTERRUPTED_REASON),
            ("still-running", "RUNNING", PROCESS_INTERRUPTED_REASON),
        ] {
            connection.execute(
                "INSERT INTO execution_intent(execution_id,account_id,kind,trigger_source,status,payload,created_at_us,error) VALUES(?1,1,'rebalance','scheduler',?2,'{}',1767232800000000,?3)",
                params![execution_id, status, error],
            ).unwrap();
        }
        connection
            .execute(
                "INSERT INTO executerecord(execution_id) VALUES('record-exists')",
                [],
            )
            .unwrap();
        connection
            .execute(
                "INSERT INTO notification_event(id) VALUES('execution:old-gap')",
                [],
            )
            .unwrap();
        let recovered = recovery_in_connection(&connection).unwrap();
        assert_eq!(recovered["repair"].as_array().unwrap().len(), 1);
        assert_eq!(recovered["repair"][0]["execution_id"], "old-gap");
        assert_eq!(recovered["interrupted"][0]["execution_id"], "still-running");
        assert_eq!(recovery_in_connection(&connection).unwrap(), recovered);
        assert_eq!(
            connection
                .query_row("SELECT COUNT(*) FROM notification_event", [], |row| row
                    .get::<_, i64>(0))
                .unwrap(),
            1
        );
        connection
            .execute(
                "INSERT INTO executerecord(execution_id) VALUES('old-gap')",
                [],
            )
            .unwrap();
        assert_eq!(
            recovery_in_connection(&connection).unwrap()["repair"],
            json!([])
        );
    }
}
