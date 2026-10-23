//! 时间检查单例的持久化；时间、NTP 和调度仍由 Python 提供。

use std::path::Path;

use rusqlite::{params, Connection, OptionalExtension};
use serde_json::{json, Map, Value};

use crate::error::AnalysisError;
use crate::writer_scope::{immediate, open_writer};

fn database_error(error: rusqlite::Error) -> AnalysisError {
    AnalysisError::new("DATABASE_QUERY", error.to_string())
}

fn object<'a>(value: &'a Value, name: &str) -> Result<&'a Map<String, Value>, AnalysisError> {
    value
        .as_object()
        .ok_or_else(|| AnalysisError::new("INVALID_REQUEST", format!("{name} must be an object")))
}

fn decode_object(encoded: &str) -> Result<Value, AnalysisError> {
    let value = serde_json::from_str(encoded)
        .map_err(|error| AnalysisError::new("DATABASE_READ", error.to_string()))?;
    object(&value, "stored time-check data")?;
    Ok(value)
}

fn read_settings(connection: &Connection) -> Result<Option<Value>, AnalysisError> {
    let row: Option<(i64, String)> = connection
        .query_row(
            "SELECT version, data FROM time_check_settings WHERE id=1",
            [],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .optional()
        .map_err(database_error)?;
    row.map(|(version, encoded)| {
        let mut value = decode_object(&encoded)?;
        value["version"] = json!(version);
        Ok(value)
    })
    .transpose()
}

fn read_state(connection: &Connection) -> Result<Option<Value>, AnalysisError> {
    let encoded: Option<String> = connection
        .query_row("SELECT data FROM time_check_state WHERE id=1", [], |row| {
            row.get(0)
        })
        .optional()
        .map_err(database_error)?;
    encoded.map(|value| decode_object(&value)).transpose()
}

fn write_state(connection: &Connection, state: &Value) -> Result<(), AnalysisError> {
    connection
        .execute(
            "INSERT INTO time_check_state(id,data) VALUES(1,?1)
             ON CONFLICT(id) DO UPDATE SET data=excluded.data",
            [state.to_string()],
        )
        .map_err(database_error)?;
    Ok(())
}

fn default_state() -> Value {
    json!({"alerts": {}, "first_claimed": false})
}

fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(value) => *value,
        Value::Number(value) => value.as_f64() != Some(0.0),
        Value::String(value) => !value.is_empty(),
        Value::Array(value) => !value.is_empty(),
        Value::Object(value) => !value.is_empty(),
    }
}

fn recover_state(mut state: Value, checked_at: &str) -> Result<Value, AnalysisError> {
    let mut alerts = state.get("alerts").cloned().unwrap_or_else(|| json!({}));
    object(&alerts, "stored time-check alerts")?;
    alerts.as_object_mut().unwrap().remove("system");
    if let Some(result) = state.get_mut("result").filter(|value| truthy(value)) {
        object(result, "stored time-check result")?;
        result.as_object_mut().unwrap().remove("system");
    }
    if state.get("running").is_some_and(truthy) {
        state["running"] = json!(false);
        alerts["interrupted"] = json!({
            "message": "上次检查因服务退出而中断",
            "checked_at": checked_at,
        });
    }
    state["alerts"] = alerts;
    Ok(state)
}

pub fn start(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    object(&request["settings"], "settings")?;
    let checked_at = request["checked_at"]
        .as_str()
        .ok_or_else(|| AnalysisError::new("INVALID_REQUEST", "checked_at must be a string"))?;
    let connection = open_writer(Path::new(path))?;
    immediate(&connection, || {
        connection
            .execute(
                "INSERT INTO time_check_settings(id,version,data) VALUES(1,0,?1)
                 ON CONFLICT(id) DO NOTHING",
                [request["settings"].to_string()],
            )
            .map_err(database_error)?;
        let settings = read_settings(&connection)?;
        let state = recover_state(
            read_state(&connection)?.unwrap_or_else(default_state),
            checked_at,
        )?;
        write_state(&connection, &state)?;
        Ok(json!({"settings": settings, "state": state}))
    })
}

pub fn get(path: &str, _request: &Value) -> Result<Value, AnalysisError> {
    let mut connection = crate::storage_connection::open_reader(Path::new(path))?;
    let transaction = connection.transaction().map_err(database_error)?;
    let result = json!({
        "settings": read_settings(&transaction)?,
        "state": read_state(&transaction)?,
    });
    transaction.rollback().map_err(database_error)?;
    Ok(result)
}

pub fn persist(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    object(&request["state"], "state")?;
    let connection = open_writer(Path::new(path))?;
    immediate(&connection, || {
        write_state(&connection, &request["state"])?;
        Ok(Value::Null)
    })
}

pub fn save(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    object(&request["settings"], "settings")?;
    let version = request["version"]
        .as_i64()
        .filter(|version| *version >= 0 && *version < i64::MAX)
        .ok_or_else(|| AnalysisError::new("INVALID_REQUEST", "invalid settings version"))?;
    let connection = open_writer(Path::new(path))?;
    immediate(&connection, || {
        let changed = connection
            .execute(
                "INSERT INTO time_check_settings(id,version,data) VALUES(1,?1,?2)
                 ON CONFLICT(id) DO UPDATE SET version=excluded.version,data=excluded.data
                 WHERE time_check_settings.version=?3",
                params![version + 1, request["settings"].to_string(), version],
            )
            .map_err(database_error)?;
        Ok(json!(changed == 1))
    })
}

pub fn claim(path: &str, _request: &Value) -> Result<Value, AnalysisError> {
    let connection = open_writer(Path::new(path))?;
    immediate(&connection, || {
        let mut state = read_state(&connection)?.unwrap_or_else(default_state);
        let claimed = !state.get("first_claimed").is_some_and(truthy);
        if claimed {
            state["first_claimed"] = json!(true);
            write_state(&connection, &state)?;
        }
        Ok(json!({"claimed": claimed, "state": state}))
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn database(name: &str) -> std::path::PathBuf {
        let path = std::env::temp_dir().join(format!(
            "axile-time-check-{}-{name}.sqlite",
            std::process::id()
        ));
        let _ = std::fs::remove_file(&path);
        Connection::open(&path)
            .unwrap()
            .execute_batch(
                "CREATE TABLE time_check_settings(id INTEGER PRIMARY KEY,version INTEGER NOT NULL,data JSON NOT NULL);
                 CREATE TABLE time_check_state(id INTEGER PRIMARY KEY,data JSON NOT NULL);",
            )
            .unwrap();
        path
    }

    #[test]
    fn startup_recovers_old_evidence_and_claim_is_durable() {
        let path = database("recovery");
        let path_str = path.to_str().unwrap();
        persist(
            path_str,
            &json!({"state": {
                "running": true, "alerts": {"system": {}, "offset": {"offset_ms": 600}},
                "result": {"system": {}, "ntp": {"status": "error"}}
            }}),
        )
        .unwrap();
        let restored = start(
            path_str,
            &json!({"settings": {"enabled": false}, "checked_at": "clock"}),
        )
        .unwrap();
        assert_eq!(restored["settings"]["version"], 0);
        assert_eq!(restored["state"]["running"], false);
        assert_eq!(
            restored["state"]["alerts"]["interrupted"]["checked_at"],
            "clock"
        );
        assert!(restored["state"]["alerts"].get("system").is_none());
        assert!(restored["state"]["result"].get("system").is_none());
        assert_eq!(restored["state"]["alerts"]["offset"]["offset_ms"], 600);
        assert_eq!(claim(path_str, &json!({})).unwrap()["claimed"], true);
        assert_eq!(claim(path_str, &json!({})).unwrap()["claimed"], false);
        assert_eq!(
            get(path_str, &json!({})).unwrap()["state"]["first_claimed"],
            true
        );
        std::fs::remove_file(path).unwrap();
    }

    #[test]
    fn stale_settings_do_not_overwrite_committed_version() {
        let path = database("version");
        let path_str = path.to_str().unwrap();
        start(path_str, &json!({"settings": {}, "checked_at": "clock"})).unwrap();
        assert_eq!(
            save(
                path_str,
                &json!({"version": 0, "settings": {"source": "tencent"}})
            )
            .unwrap(),
            true
        );
        assert_eq!(
            save(
                path_str,
                &json!({"version": 0, "settings": {"source": "aliyun"}})
            )
            .unwrap(),
            false
        );
        let stored = get(path_str, &json!({})).unwrap();
        assert_eq!(
            stored["settings"],
            json!({"source": "tencent", "version": 1})
        );
        assert!(persist(path_str, &json!({"state": []})).is_err());
        assert_eq!(get(path_str, &json!({})).unwrap()["state"], stored["state"]);
        std::fs::remove_file(path).unwrap();
    }
}
