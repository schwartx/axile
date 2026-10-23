//! Shared writer-transaction scope for operation-level SQLite ownership.
//!
//! Operation entry points (publish_store, business facts ops) open one writer
//! connection and run their whole operation inside a single BEGIN IMMEDIATE,
//! so no partial state crosses the language boundary.

use std::path::Path;
use std::time::Duration;

use rusqlite::{Connection, OpenFlags};

use crate::error::AnalysisError;

/// Open the single writer connection shared by all write-side operations.
pub(crate) fn open_writer(database: &Path) -> Result<Connection, AnalysisError> {
    // Deliberately omit CREATE: an invalid path must never create a business DB.
    let connection = Connection::open_with_flags(
        database,
        OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .map_err(|error| AnalysisError::new("DATABASE_OPEN", error.to_string()))?;
    connection
        .busy_timeout(Duration::from_secs(5))
        .map_err(|error| AnalysisError::new("DATABASE_CONFIG", error.to_string()))?;
    connection
        .execute_batch(
            "PRAGMA foreign_keys=ON;
         PRAGMA cache_size=-4096;
         PRAGMA mmap_size=0;
         PRAGMA temp_store=FILE;",
        )
        .map_err(|error| AnalysisError::new("DATABASE_CONFIG", error.to_string()))?;
    Ok(connection)
}

/// Run `work` inside one BEGIN IMMEDIATE transaction, committing on success
/// and rolling back on any error.
pub(crate) fn immediate<T>(
    connection: &Connection,
    work: impl FnOnce() -> Result<T, AnalysisError>,
) -> Result<T, AnalysisError> {
    connection
        .execute_batch("BEGIN IMMEDIATE;")
        .map_err(|error| AnalysisError::new("PUBLICATION_WRITE", error.to_string()))?;
    match work() {
        Ok(value) => {
            if let Err(error) = connection.execute_batch("COMMIT;") {
                let _ = connection.execute_batch("ROLLBACK;");
                return Err(AnalysisError::new("PUBLICATION_WRITE", error.to_string()));
            }
            Ok(value)
        }
        Err(error) => {
            let _ = connection.execute_batch("ROLLBACK;");
            Err(error)
        }
    }
}
