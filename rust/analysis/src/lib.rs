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
pub(crate) mod writer_scope;

use rusqlite::Connection;
use std::path::Path;

use crate::error::AnalysisError;

fn with_read_snapshot<T>(
    database: &Path,
    task: impl FnOnce(&Connection) -> Result<T, AnalysisError>,
) -> Result<T, AnalysisError> {
    with_read_snapshot_errors(database, task, ("DATABASE_CONFIG", "DATABASE_CLOSE"))
}

// Preserve each entrypoint's existing transaction error codes in one shared scope.
fn with_read_snapshot_errors<T>(
    database: &Path,
    task: impl FnOnce(&Connection) -> Result<T, AnalysisError>,
    (begin_error, close_error): (&str, &str),
) -> Result<T, AnalysisError> {
    // Read-only mode prevents accidental source writes, but deliberately avoids
    // immutable=1: live Python commits and WAL contents must remain visible.
    let connection = storage_connection::open_reader(database)?;
    connection
        .execute_batch("BEGIN DEFERRED;")
        .map_err(|error| AnalysisError::new(begin_error, error.to_string()))?;

    let result = task(&connection);

    // End the snapshot promptly; the lease discards a connection if cleanup fails.
    let closed = connection
        .execute_batch("ROLLBACK;")
        .map_err(|error| AnalysisError::new(close_error, error.to_string()));
    match result {
        Ok(value) => closed.map(|()| value),
        Err(error) => Err(error),
    }
}

#[cfg(test)]
mod tests {
    use std::path::Path;

    #[test]
    fn missing_database_is_not_created() {
        let result =
            super::with_read_snapshot(Path::new("/nonexistent-directory/analysis.sqlite"), |_| {
                Ok(())
            });
        assert_eq!(result.unwrap_err().code, "DATABASE_OPEN");
    }

    #[test]
    fn read_scope_preserves_errors_and_releases_failed_snapshots() {
        let directory =
            std::env::temp_dir().join(format!("axile-read-scope-{}", std::process::id()));
        std::fs::create_dir(&directory).unwrap();
        let database = directory.join("reader.sqlite");
        rusqlite::Connection::open(&database).unwrap();
        for codes in [
            ("DATABASE_CONFIG", "DATABASE_CLOSE"),
            ("DATABASE_BEGIN", "DATABASE_CLOSE"),
            ("BUSINESS_FACTS_SQL", "BUSINESS_FACTS_SQL"),
        ] {
            let error = super::with_read_snapshot_errors(
                &database,
                |connection| {
                    connection.execute_batch("ROLLBACK;").unwrap();
                    Ok(())
                },
                codes,
            )
            .unwrap_err();
            assert_eq!(error.code, codes.1);
            let error = super::with_read_snapshot_errors(
                &database,
                |connection| {
                    connection.execute_batch("ROLLBACK;").unwrap();
                    Err::<(), _>(super::AnalysisError::new("TASK_ERROR", "original failure"))
                },
                codes,
            )
            .unwrap_err();
            assert_eq!(error.code, "TASK_ERROR");
            assert_eq!(error.error, "original failure");
            assert_eq!(
                super::with_read_snapshot(&database, |connection| {
                    assert!(!connection.is_autocommit());
                    assert!(connection
                        .execute_batch("CREATE TABLE forbidden(id INTEGER)")
                        .is_err());
                    Ok(connection
                        .query_row("SELECT 1", [], |row| row.get::<_, i64>(0))
                        .unwrap())
                })
                .unwrap(),
                1
            );
        }
        drop(super::storage_connection::migration_guard(&database).unwrap());
        std::fs::remove_dir_all(directory).unwrap();
    }
}
