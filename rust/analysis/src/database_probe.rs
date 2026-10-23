//! 初始化向导的临时连接探测；允许建空库，不初始化业务 schema。

use std::time::Duration;

use rusqlite::{Connection, OpenFlags};
use serde_json::Value;

use crate::error::AnalysisError;

pub fn probe(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let uri = match request.get("uri") {
        None => false,
        Some(Value::Bool(value)) => *value,
        _ => {
            return Err(AnalysisError::new(
                "INVALID_REQUEST",
                "uri must be a boolean",
            ))
        }
    };
    let timeout = request
        .get("timeout")
        .map_or(Some(5.0), Value::as_f64)
        .filter(|value| {
            value.is_finite() && *value >= 0.0 && *value <= f64::from(i32::MAX) / 1000.0
        })
        .ok_or_else(|| AnalysisError::new("INVALID_REQUEST", "invalid database timeout"))?;
    let mut flags = OpenFlags::SQLITE_OPEN_READ_WRITE
        | OpenFlags::SQLITE_OPEN_CREATE
        | OpenFlags::SQLITE_OPEN_NO_MUTEX;
    if uri {
        flags |= OpenFlags::SQLITE_OPEN_URI;
    }
    let connection = Connection::open_with_flags(path, flags)
        .map_err(|error| AnalysisError::new("DATABASE_OPEN", error.to_string()))?;
    connection
        .busy_timeout(Duration::from_secs_f64(timeout))
        .map_err(|error| AnalysisError::new("DATABASE_CONFIG", error.to_string()))?;
    connection
        .query_row("SELECT 1", [], |row| row.get::<_, i64>(0))
        .map_err(|error| AnalysisError::new("DATABASE_QUERY", error.to_string()))?;
    Ok(Value::Null)
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    #[test]
    fn probe_creates_only_empty_database_and_honors_read_only_uri() {
        let path = std::env::temp_dir().join(format!("axile-probe-{}.sqlite", std::process::id()));
        let _ = std::fs::remove_file(&path);
        let path_str = path.to_str().unwrap();
        probe(path_str, &json!({})).unwrap();
        let connection = Connection::open(&path).unwrap();
        let tables: i64 = connection
            .query_row("SELECT count(*) FROM sqlite_master", [], |row| row.get(0))
            .unwrap();
        assert_eq!(tables, 0);
        drop(connection);
        probe(&format!("file:{path_str}?mode=ro"), &json!({"uri": true})).unwrap();
        std::fs::remove_file(&path).unwrap();
        assert!(probe(&format!("file:{path_str}?mode=ro"), &json!({"uri": true})).is_err());
        assert!(!path.exists());
        probe(":memory:", &json!({})).unwrap();
    }
}
