//! Bounded SQLite access for the native analysis process.
//!
//! Python owns source transactions and schema migrations. This worker opens a
//! live database read-only and keeps a single consistent read snapshot for each
//! request. No historical rows cross the Python/Rust process boundary.

pub mod costs;
pub mod error;
pub mod performance;
pub mod replay;
pub mod snapshots;

use std::path::Path;
use std::time::Duration;

use rusqlite::{Connection, OpenFlags};
use serde_json::{json, Value};

use crate::error::AnalysisError;

pub const PROTOCOL_VERSION: u32 = 1;
pub const MAX_REQUEST_BYTES: u64 = 1024 * 1024;

pub fn capabilities() -> Value {
    json!({
        "protocol": PROTOCOL_VERSION,
        "version": env!("CARGO_PKG_VERSION"),
        "commands": ["performance", "replay", "costs", "snapshot", "performance_result"],
        "read_only": true,
    })
}

/// Execute one complete analysis task against a consistent SQLite read view.
pub fn query(database: &Path, request: &Value) -> Result<Value, AnalysisError> {
    let command = request
        .as_object()
        .and_then(|request| request.get("command"))
        .and_then(Value::as_str)
        .ok_or_else(|| AnalysisError::new("INVALID_REQUEST", "command must be a string"))?;
    if !matches!(
        command,
        "performance" | "replay" | "costs" | "snapshot" | "performance_result"
    ) {
        return Err(AnalysisError::new(
            "INVALID_REQUEST",
            format!("unsupported command: {command}"),
        ));
    }

    // Read-only mode prevents accidental source writes, but deliberately avoids
    // immutable=1: live Python commits and WAL contents must remain visible.
    let connection = Connection::open_with_flags(
        database,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .map_err(|error| AnalysisError::new("DATABASE_OPEN", error.to_string()))?;
    connection
        .busy_timeout(Duration::from_secs(5))
        .map_err(|error| AnalysisError::new("DATABASE_CONFIG", error.to_string()))?;
    // Cap the worker page cache rather than relying on SQLite's build defaults.
    // Sorting can use a temporary file; mapped database pages are disabled so
    // process working-set comparisons include the actual read cache.
    connection
        .execute_batch(
            "PRAGMA query_only = ON;
             PRAGMA cache_size = -4096;
             PRAGMA mmap_size = 0;
             PRAGMA temp_store = FILE;
             BEGIN DEFERRED;",
        )
        .map_err(|error| AnalysisError::new("DATABASE_CONFIG", error.to_string()))?;

    let result = match command {
        "costs" => costs::query_costs(&connection, request),
        "snapshot" => snapshots::query(&connection, request),
        "performance_result" => snapshots::performance_result(&connection, request),
        _ => performance::handle(&connection, request),
    }
    .map_err(AnalysisError::from);

    // End the snapshot promptly. Drop still closes the transaction on errors,
    // including an error while rolling back a damaged connection.
    let closed = connection
        .execute_batch("ROLLBACK;")
        .map_err(|error| AnalysisError::new("DATABASE_CLOSE", error.to_string()));
    match result {
        Ok(value) => closed.map(|()| value),
        Err(error) => Err(error),
    }
}

#[cfg(test)]
mod tests {
    use std::path::Path;

    use serde_json::json;

    #[test]
    fn invalid_commands_do_not_open_a_database() {
        let result = super::query(
            Path::new("/nonexistent.sqlite"),
            &json!({"command": "write"}),
        );
        assert_eq!(result.unwrap_err().code, "INVALID_REQUEST");
    }

    #[test]
    fn missing_database_is_not_created() {
        let result = super::query(
            Path::new("/nonexistent-directory/analysis.sqlite"),
            &json!({"command": "costs"}),
        );
        assert_eq!(result.unwrap_err().code, "DATABASE_OPEN");
    }
}
