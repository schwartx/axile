//! Axile SQLite business operations and consistent analysis queries.

pub(crate) mod accounts;
pub(crate) mod analysis_ops;
pub mod analysis_queue;
pub(crate) mod asset_ops;
pub mod assets;
pub(crate) mod audit_ops;
pub(crate) mod business_detail;
pub(crate) mod business_facts_ops;
pub mod calendar;
pub(crate) mod connection_state;
pub(crate) mod control_audit;
pub(crate) mod control_monitor;
pub(crate) mod control_store;
pub mod costs;
pub mod dashboard;
pub(crate) mod database_probe;
pub mod error;
pub(crate) mod evidence_ops;
pub(crate) mod execution_ops;
pub(crate) mod execution_reads;
pub(crate) mod execution_summary;
pub mod executions;
pub(crate) mod fact_attempts;
pub(crate) mod fact_cost_rows;
pub(crate) mod fact_events;
pub(crate) mod fact_headers;
pub(crate) mod fact_integrity;
pub(crate) mod fact_observation;
pub(crate) mod fact_positions;
pub(crate) mod fact_prices;
pub(crate) mod fact_sizing;
pub(crate) mod fact_ticks;
pub(crate) mod fact_trades;
pub(crate) mod fact_transactions;
pub(crate) mod intents;
pub mod journal;
pub(crate) mod maintenance_ops;
pub(crate) mod migrations;
pub mod native_publication;
pub(crate) mod notifications;
pub mod performance;
pub(crate) mod portfolio_ops;
pub mod publication;
#[cfg(feature = "python")]
pub mod python;
pub mod replay;
pub(crate) mod result_reader;
pub(crate) mod runtime_sync;
pub mod snapshots;
pub(crate) mod storage_connection;
pub mod submissions;
pub(crate) mod supplements;
pub mod symbols;
pub(crate) mod target_ops;
pub(crate) mod time_check;
pub mod views;
pub(crate) mod wire_result;
pub(crate) mod writer_scope;

use rusqlite::Connection;
use serde_json::{json, Value};
use std::path::Path;

use crate::error::AnalysisError;

pub const PROTOCOL_VERSION: u32 = 1;
pub const MAX_REQUEST_BYTES: u64 = 1024 * 1024;

pub fn capabilities() -> Value {
    json!({
        "protocol": PROTOCOL_VERSION,
        "analysis_fact_schema": 40,
        "public_head": migrations::PUBLIC_HEAD,
        "version": env!("CARGO_PKG_VERSION"),
        "commands": ["costs", "snapshot", "performance_result", "publish_store"],
        "read_only": false,
        "write_commands": ["publish_store"],
    })
}

/// Execute one complete analysis task against a consistent SQLite read view.
pub fn query(database: &Path, request: &Value) -> Result<Value, AnalysisError> {
    let command = request_command(request)?;
    if command == "publish_store" {
        return native_publication::publish(database, request);
    }
    if !matches!(command, "costs" | "snapshot" | "performance_result") {
        return Err(AnalysisError::new(
            "INVALID_REQUEST",
            format!("unsupported command: {command}"),
        ));
    }

    with_read_snapshot(database, |connection| {
        match command {
            "costs" => costs::query_costs(connection, request),
            "snapshot" => snapshots::query(connection, request),
            _ => snapshots::performance_result(connection, request),
        }
        .map_err(AnalysisError::from)
    })
}

/// Preserve typed curve storage until serialization, after the read view closes.
pub fn query_wire(
    database: &Path,
    request: &Value,
) -> Result<wire_result::WireResult, AnalysisError> {
    let command = request_command(request)?;
    if matches!(command, "snapshot" | "performance_result") {
        return with_read_snapshot(database, |connection| {
            match command {
                "snapshot" => snapshots::query_wire(connection, request),
                _ => snapshots::performance_result_wire(connection, request),
            }
            .map_err(AnalysisError::from)
        });
    }
    query(database, request).map(|value| wire_result::WireResult {
        value,
        points: None,
        performance_only: false,
    })
}

fn request_command(request: &Value) -> Result<&str, AnalysisError> {
    request
        .as_object()
        .and_then(|request| request.get("command"))
        .and_then(Value::as_str)
        .ok_or_else(|| AnalysisError::new("INVALID_REQUEST", "command must be a string"))
}

fn with_read_snapshot<T>(
    database: &Path,
    task: impl FnOnce(&Connection) -> Result<T, AnalysisError>,
) -> Result<T, AnalysisError> {
    // Read-only mode prevents accidental source writes, but deliberately avoids
    // immutable=1: live Python commits and WAL contents must remain visible.
    let connection = storage_connection::open_reader(database)?;
    connection
        .execute_batch("BEGIN DEFERRED;")
        .map_err(|error| AnalysisError::new("DATABASE_CONFIG", error.to_string()))?;

    let result = task(&connection);

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
            &json!({"command": "publish"}),
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
