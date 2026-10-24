//! Shared writer-transaction scope for operation-level SQLite ownership.
//!
//! Operation entry points (publish_store, business facts ops) open one writer
//! connection and run their whole operation inside a single BEGIN IMMEDIATE,
//! so no partial state crosses the language boundary.

use std::path::Path;

use rusqlite::Connection;

use crate::error::AnalysisError;
use crate::storage_connection::ConnectionLease;

/// Open the single writer connection shared by all write-side operations.
pub(crate) fn open_writer(database: &Path) -> Result<ConnectionLease, AnalysisError> {
    crate::storage_connection::open_writer(database, true)
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn immediate_commits_before_return_and_rolls_back_failed_work() {
        let directory =
            std::env::temp_dir().join(format!("axile-writer-scope-{}", std::process::id()));
        std::fs::create_dir(&directory).unwrap();
        let path = directory.join("writer.sqlite");
        Connection::open(&path)
            .unwrap()
            .execute_batch("CREATE TABLE entries(value INTEGER NOT NULL);")
            .unwrap();
        let writer = open_writer(&path).unwrap();
        immediate(&writer, || {
            writer
                .execute("INSERT INTO entries VALUES (1)", [])
                .map_err(|error| AnalysisError::new("TEST_WRITE", error.to_string()))?;
            Ok(())
        })
        .unwrap();
        let reader = crate::storage_connection::open_reader(&path).unwrap();
        assert_eq!(
            reader
                .query_row("SELECT value FROM entries", [], |row| row.get::<_, i64>(0))
                .unwrap(),
            1
        );

        let error = immediate(&writer, || {
            writer
                .execute("INSERT INTO entries VALUES (2)", [])
                .unwrap();
            Err::<(), _>(AnalysisError::new("TEST_ABORT", "abort after write"))
        })
        .unwrap_err();
        assert_eq!(error.code, "TEST_ABORT");
        assert!(writer.is_autocommit());
        drop((reader, writer));
        let reopened = crate::storage_connection::open_reader(&path).unwrap();
        assert_eq!(
            reopened
                .query_row("SELECT COUNT(*) FROM entries", [], |row| row
                    .get::<_, i64>(0))
                .unwrap(),
            1
        );
        drop(reopened);
        drop(crate::storage_connection::migration_guard(&path).unwrap());
        std::fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn failed_commit_rolls_back_and_releases_the_transaction() {
        let connection = Connection::open_in_memory().unwrap();
        connection
            .execute_batch(
                "PRAGMA foreign_keys=ON;
                 CREATE TABLE parent(id INTEGER PRIMARY KEY);
                 CREATE TABLE child(parent_id INTEGER REFERENCES parent(id)
                     DEFERRABLE INITIALLY DEFERRED);",
            )
            .unwrap();
        let error = immediate(&connection, || {
            connection
                .execute("INSERT INTO child VALUES (1)", [])
                .unwrap();
            Ok(())
        })
        .unwrap_err();
        assert_eq!(error.code, "PUBLICATION_WRITE");
        assert!(connection.is_autocommit());
        assert_eq!(
            connection
                .query_row("SELECT COUNT(*) FROM child", [], |row| row.get::<_, i64>(0))
                .unwrap(),
            0
        );
        immediate(&connection, || {
            connection
                .execute("INSERT INTO parent VALUES (1)", [])
                .unwrap();
            connection
                .execute("INSERT INTO child VALUES (1)", [])
                .unwrap();
            Ok(())
        })
        .unwrap();
    }
}
