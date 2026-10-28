//! Own the disk-backed temporary database and validate completion before publication.

use rusqlite::Connection;

use crate::error::AnalysisError;

const STAGE: &str = "native_stage";

// Only migrated result tables participate in staging and publication.
pub(crate) const RESULT_TABLES: &[&str] = &[
    "performance_contribution",
    "performance_result_daily_fee",
    "performance_result_daily_cost",
    "performance_result_event",
    "performance_result_calendar_range",
    "performance_result_binding",
    "performance_result_fallback",
    "performance_result_skip",
    "performance_result_point",
    "performance_result_range",
    "performance_execution_fee",
    "performance_cost_execution",
    "performance_cost_trade",
    "performance_cost_execution_symbol",
    "performance_cost_position",
    "performance_cost_attempt",
    "performance_cost_transaction",
    "performance_cost_transaction_fee",
];

#[derive(Debug)]
struct Column {
    name: String,
    kind: String,
    required: bool,
    key: i64,
}

pub(super) struct Staging<'a> {
    connection: &'a Connection,
}

impl Drop for Staging<'_> {
    fn drop(&mut self) {
        if !self.connection.is_autocommit() {
            let _ = self.connection.execute_batch("ROLLBACK;");
        }
        // Cached inserts must release the stage schema before it is detached.
        self.connection.flush_prepared_statement_cache();
        let _ = self
            .connection
            .execute_batch("DETACH DATABASE native_stage;");
    }
}

fn invalid(message: impl Into<String>) -> AnalysisError {
    AnalysisError::new("INVALID_NATIVE_PUBLICATION", message)
}

fn sql(error: rusqlite::Error) -> AnalysisError {
    AnalysisError::caused_by("PUBLICATION_STAGE", error)
}

impl<'a> Staging<'a> {
    pub(super) fn new(connection: &'a Connection) -> Result<Self, AnalysisError> {
        Self::new_at(connection, "")
    }

    pub(crate) fn new_at(
        connection: &'a Connection,
        filename: &str,
    ) -> Result<Self, AnalysisError> {
        connection
            .execute("ATTACH DATABASE ?1 AS native_stage", [filename])
            .map_err(sql)?;
        let staged = Self { connection };
        connection
            .execute_batch(
                "PRAGMA native_stage.cache_size = -4096;
                 PRAGMA native_stage.mmap_size = 0;
                 PRAGMA native_stage.journal_mode = DELETE;",
            )
            .map_err(sql)?;
        for &name in RESULT_TABLES {
            connection
                .execute_batch(&format!("DROP TABLE IF EXISTS {STAGE}.\"{name}\";"))
                .map_err(sql)?;
            let columns = table_columns(connection, name)?;
            create_table(connection, name, &columns)?;
        }
        connection.execute_batch("BEGIN DEFERRED;").map_err(sql)?;
        Ok(staged)
    }

    pub(super) fn finish(&self) -> Result<(), AnalysisError> {
        let mut statement = self
            .connection
            .prepare(
                "SELECT range_key FROM native_stage.performance_result_range ORDER BY range_key",
            )
            .map_err(sql)?;
        let ranges = statement
            .query_map([], |row| row.get::<_, String>(0))
            .map_err(sql)?
            .collect::<Result<Vec<_>, _>>()
            .map_err(sql)?;
        if ranges != ["30", "90", "all"] {
            return Err(invalid("publication must contain three complete ranges"));
        }
        self.connection.execute_batch("COMMIT;").map_err(sql)?;
        Ok(())
    }
}

fn table_columns(connection: &Connection, name: &str) -> Result<Vec<Column>, AnalysisError> {
    let mut statement = connection
        .prepare(&format!("PRAGMA main.table_info(\"{name}\")"))
        .map_err(sql)?;
    let columns = statement
        .query_map([], |row| {
            Ok(Column {
                name: row.get(1)?,
                kind: row.get(2)?,
                required: row.get::<_, i64>(3)? != 0,
                key: row.get(5)?,
            })
        })
        .map_err(sql)?
        .collect::<Result<Vec<_>, _>>()
        .map_err(sql)?;
    if !columns.iter().any(|column| column.name == "snapshot_id") {
        return Err(AnalysisError::new(
            "SCHEMA_INCOMPATIBLE",
            format!("missing migrated analysis table {name}"),
        ));
    }
    Ok(columns
        .into_iter()
        .filter(|column| column.name != "snapshot_id")
        .collect())
}

fn create_table(
    connection: &Connection,
    name: &str,
    columns: &[Column],
) -> Result<(), AnalysisError> {
    let mut definitions = Vec::new();
    let mut keys = Vec::new();
    for column in columns {
        // Names/types come from our migrated schema, never the request/frame.
        if !column
            .name
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'_')
            || !matches!(
                column.kind.as_str(),
                "TEXT" | "INTEGER" | "FLOAT" | "REAL" | "BOOLEAN" | "JSON"
            )
        {
            return Err(invalid("unsupported result schema column"));
        }
        let required = if column.required { " NOT NULL" } else { "" };
        definitions.push(format!("\"{}\" {}{required}", column.name, column.kind));
        if column.key > 0 {
            keys.push((column.key, format!("\"{}\"", column.name)));
        }
    }
    if !keys.is_empty() {
        keys.sort_unstable();
        definitions.push(format!(
            "PRIMARY KEY ({})",
            keys.into_iter()
                .map(|(_, name)| name)
                .collect::<Vec<_>>()
                .join(",")
        ));
    }
    connection
        .execute_batch(&format!(
            "CREATE TABLE {STAGE}.\"{name}\" ({})",
            definitions.join(",")
        ))
        .map_err(sql)
}

pub(super) fn copy_results(
    connection: &Connection,
    snapshot_id: &str,
) -> Result<(), AnalysisError> {
    for &name in RESULT_TABLES {
        let columns = table_columns(connection, name)?;
        let columns = columns
            .iter()
            .map(|column| format!("\"{}\"", column.name))
            .collect::<Vec<_>>()
            .join(",");
        let query = format!(
            "INSERT INTO main.\"{name}\" (snapshot_id,{columns}) SELECT ?1,{columns} FROM {STAGE}.\"{name}\""
        );
        connection.execute(&query, [snapshot_id]).map_err(sql)?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture() -> Connection {
        let connection = Connection::open_in_memory().unwrap();
        for name in RESULT_TABLES {
            connection.execute_batch(&format!(
                "CREATE TABLE {name}(snapshot_id TEXT NOT NULL,range_key TEXT NOT NULL,value INTEGER);"
            )).unwrap();
        }
        connection
    }

    fn assert_detached(connection: &Connection) {
        assert!(connection.is_autocommit());
        assert_eq!(
            connection
                .query_row(
                    "SELECT COUNT(*) FROM pragma_database_list WHERE name='native_stage'",
                    [],
                    |row| row.get::<_, i64>(0)
                )
                .unwrap(),
            0
        );
    }

    #[test]
    fn schema_failure_detaches_partially_initialized_stage() {
        let connection = fixture();
        connection
            .execute_batch("DROP TABLE performance_cost_transaction_fee;")
            .unwrap();
        for _ in 0..2 {
            assert_eq!(
                Staging::new(&connection).err().unwrap().code,
                "SCHEMA_INCOMPATIBLE"
            );
            assert_detached(&connection);
        }
    }

    #[test]
    fn finish_checks_ranges_and_preserves_staging_until_copy() {
        for valid in [true, false] {
            let connection = fixture();
            let staged = Staging::new(&connection).unwrap();
            let last = if valid { "all" } else { "90" };
            for key in ["30", "90", last] {
                connection
                    .execute(
                        "INSERT INTO native_stage.performance_result_range VALUES(?1,NULL)",
                        [key],
                    )
                    .unwrap();
            }
            let result = staged.finish();
            if valid {
                result.unwrap();
                assert_eq!(
                    connection
                        .query_row(
                            "SELECT COUNT(*) FROM main.performance_result_range",
                            [],
                            |row| row.get::<_, i64>(0)
                        )
                        .unwrap(),
                    0
                );
                copy_results(&connection, "snapshot").unwrap();
                assert_eq!(connection.query_row("SELECT COUNT(*) FROM main.performance_result_range WHERE snapshot_id='snapshot'", [], |row| row.get::<_,i64>(0)).unwrap(), 3);
            } else {
                assert_eq!(result.unwrap_err().code, "INVALID_NATIVE_PUBLICATION");
            }
            drop(staged);
            assert_detached(&connection);
        }
    }

    #[test]
    fn dropping_stage_rolls_back_inserts_and_releases_cached_statements() {
        let connection = fixture();
        {
            let _staged = Staging::new(&connection).unwrap();
            connection
                .prepare_cached(
                    "INSERT INTO native_stage.performance_result_range VALUES('all',NULL)",
                )
                .unwrap()
                .execute([])
                .unwrap();
        }
        assert_detached(&connection);
        assert_eq!(
            connection
                .query_row(
                    "SELECT COUNT(*) FROM main.performance_result_range",
                    [],
                    |row| row.get::<_, i64>(0)
                )
                .unwrap(),
            0
        );
        drop(Staging::new(&connection).unwrap());
        assert_detached(&connection);
    }
}
