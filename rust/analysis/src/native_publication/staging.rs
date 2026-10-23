//! Consume bounded in-process publication frames into SQLite's temporary file.

use std::collections::{BTreeMap, BTreeSet};

use rusqlite::{params_from_iter, types::Value as SqlValue, Connection};
use serde_json::Value;

use crate::error::AnalysisError;

const STAGE: &str = "native_stage";

// The names are trusted identifiers; frames never supply SQL identifiers.
pub(super) const RESULT_TABLES: &[&str] = &[
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
    tables: BTreeMap<&'static str, Vec<Column>>,
    counts: BTreeMap<String, usize>,
}

fn invalid(message: impl Into<String>) -> AnalysisError {
    AnalysisError::new("INVALID_NATIVE_PUBLICATION", message)
}

fn sql(error: rusqlite::Error) -> AnalysisError {
    AnalysisError::new("PUBLICATION_STAGE", error.to_string())
}

impl<'a> Staging<'a> {
    pub(super) fn new(connection: &'a Connection) -> Result<Self, AnalysisError> {
        // Empty filenames create private disk-backed databases which SQLite
        // unlinks automatically, including when the process is killed.
        connection
            .execute_batch(
                "ATTACH DATABASE '' AS native_stage;
                 PRAGMA native_stage.cache_size = -4096;
                 PRAGMA native_stage.mmap_size = 0;
                 PRAGMA native_stage.journal_mode = DELETE;",
            )
            .map_err(sql)?;
        let mut tables = BTreeMap::new();
        for &name in RESULT_TABLES {
            let columns = table_columns(connection, name)?;
            create_table(connection, name, &columns)?;
            tables.insert(name, columns);
        }
        connection.execute_batch("BEGIN DEFERRED;").map_err(sql)?;
        Ok(Self {
            connection,
            tables,
            counts: BTreeMap::new(),
        })
    }

    pub(super) fn insert_rows(
        &mut self,
        name: &str,
        columns: &[&str],
        rows: &[Vec<Value>],
    ) -> Result<(), AnalysisError> {
        let table = self
            .tables
            .get(name)
            .ok_or_else(|| invalid("unknown result table"))?;
        let columns = checked_columns(columns, table)?;
        if rows.is_empty() || rows.len() > 256 {
            return Err(invalid("invalid publication batch size"));
        }
        let names = columns
            .iter()
            .map(|column| format!("\"{}\"", column.name))
            .collect::<Vec<_>>()
            .join(",");
        let parameters = vec!["?"; columns.len()].join(",");
        let query = format!("INSERT INTO {STAGE}.\"{name}\" ({names}) VALUES ({parameters})");
        let mut statement = self.connection.prepare_cached(&query).map_err(sql)?;
        for row in rows {
            if row.len() != columns.len() {
                return Err(invalid("result row width mismatch"));
            }
            let values = columns
                .iter()
                .zip(row)
                .map(|(column, value)| cell(column, value))
                .collect::<Result<Vec<_>, _>>()?;
            statement
                .execute(params_from_iter(values.iter()))
                .map_err(sql)?;
        }
        *self.counts.entry(name.to_owned()).or_default() += rows.len();
        Ok(())
    }

    pub(super) fn finish(
        self,
        complete: Value,
        account_id: i64,
        source_version: i64,
    ) -> Result<Value, AnalysisError> {
        validate_complete(&complete, &self.counts, account_id, source_version)?;
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
        Ok(complete)
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

fn checked_columns<'a>(
    names: &[&str],
    table: &'a [Column],
) -> Result<Vec<&'a Column>, AnalysisError> {
    let mut seen = BTreeSet::new();
    let mut columns = Vec::new();
    for &name in names {
        if !seen.insert(name) {
            return Err(invalid("duplicate result column"));
        }
        let column = table
            .iter()
            .find(|column| column.name == name)
            .ok_or_else(|| invalid("unknown result column"))?;
        columns.push(column);
    }
    if columns.len() != table.len() {
        return Err(invalid("result columns differ from migrated schema"));
    }
    Ok(columns)
}

fn cell(column: &Column, value: &Value) -> Result<SqlValue, AnalysisError> {
    if column.name == "range_key" && !matches!(value.as_str(), Some("30" | "90" | "all")) {
        return Err(invalid("unknown result range"));
    }
    if value.is_null() {
        return if column.required {
            Err(invalid(format!(
                "missing required result value {}",
                column.name
            )))
        } else {
            Ok(SqlValue::Null)
        };
    }
    let parsed = match column.kind.as_str() {
        "TEXT" => value.as_str().map(|value| SqlValue::Text(value.to_owned())),
        "JSON" if column.name == "payload" && value.as_str() == Some("{}") => {
            Some(SqlValue::Text("{}".into()))
        }
        "INTEGER" => value.as_i64().map(SqlValue::Integer),
        "BOOLEAN" => value
            .as_bool()
            .map(|value| SqlValue::Integer(i64::from(value))),
        "FLOAT" | "REAL" => value
            .as_f64()
            .filter(|value| value.is_finite())
            .map(SqlValue::Real),
        _ => None,
    };
    parsed.ok_or_else(|| invalid(format!("invalid result value type {}", column.name)))
}

fn validate_complete(
    complete: &Value,
    counts: &BTreeMap<String, usize>,
    account_id: i64,
    source_version: i64,
) -> Result<(), AnalysisError> {
    if complete["account_id"].as_i64() != Some(account_id)
        || complete["source_version"].as_i64() != Some(source_version)
    {
        return Err(invalid("publication source identity changed"));
    }
    let reported = complete["row_counts"]
        .as_object()
        .ok_or_else(|| invalid("missing result row counts"))?;
    let mut actual = BTreeMap::new();
    for (name, count) in reported {
        let count = count
            .as_u64()
            .ok_or_else(|| invalid("invalid result count"))?;
        if !RESULT_TABLES.contains(&name.as_str()) {
            return Err(invalid("unknown counted result table"));
        }
        if count > 0 {
            actual.insert(name.clone(), count as usize);
        }
    }
    if &actual != counts {
        return Err(invalid("publication row count mismatch"));
    }
    if !complete["data_until"].is_null() && !complete["data_until"].is_string() {
        return Err(invalid("invalid data_until"));
    }
    Ok(())
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
    use serde_json::json;

    #[test]
    fn payload_accepts_only_empty_compatibility_object() {
        let column = Column {
            name: "payload".into(),
            kind: "JSON".into(),
            required: true,
            key: 0,
        };
        assert_eq!(
            cell(&column, &json!("{}")).unwrap(),
            SqlValue::Text("{}".into())
        );
        assert!(cell(&column, &json!("{\"history\":[]} ")).is_err());
        assert!(cell(&column, &json!({})).is_err());
    }

    fn fixture() -> Connection {
        let connection = Connection::open_in_memory().unwrap();
        for name in RESULT_TABLES {
            connection.execute_batch(&format!(
                "CREATE TABLE {name}(snapshot_id TEXT NOT NULL,range_key TEXT NOT NULL,value INTEGER);"
            )).unwrap();
        }
        connection
    }

    #[test]
    fn typed_insert_checks_identifiers_width_types_and_batch_limits() {
        let connection = fixture();
        let mut staged = Staging::new(&connection).unwrap();
        let table = "performance_result_range";
        let columns = ["range_key", "value"];
        let row = vec![json!("all"), Value::Null];
        for (name, names, rows) in [
            ("unknown", columns.as_slice(), vec![row.clone()]),
            (table, &["range_key", "unknown"], vec![row.clone()]),
            (table, &["range_key", "range_key"], vec![row.clone()]),
            (table, &["range_key"], vec![row.clone()]),
            (table, columns.as_slice(), vec![vec![json!("all")]]),
            (
                table,
                columns.as_slice(),
                vec![vec![json!("all"), json!(true)]],
            ),
            (table, columns.as_slice(), vec![vec![Value::Null, json!(1)]]),
            (
                table,
                columns.as_slice(),
                vec![vec![json!("invalid"), json!(1)]],
            ),
            (table, columns.as_slice(), vec![]),
            (table, columns.as_slice(), vec![row.clone(); 257]),
        ] {
            assert_eq!(
                staged.insert_rows(name, names, &rows).unwrap_err().code,
                "INVALID_NATIVE_PUBLICATION"
            );
        }
        staged
            .insert_rows(table, &columns, &vec![row; 256])
            .unwrap();
        assert_eq!(staged.counts[table], 256);
        assert_eq!(connection.query_row(
            "SELECT COUNT(*) FROM native_stage.performance_result_range WHERE value IS NULL",
            [], |row| row.get::<_, usize>(0)
        ).unwrap(), 256);
    }

    #[test]
    fn finish_validates_identity_counts_and_three_ranges_before_commit() {
        for case in ["valid", "identity", "count", "ranges"] {
            let connection = fixture();
            let mut staged = Staging::new(&connection).unwrap();
            let ranges = if case == "ranges" {
                vec!["30", "90", "90"]
            } else {
                vec!["30", "90", "all"]
            };
            staged
                .insert_rows(
                    "performance_result_range",
                    &["range_key", "value"],
                    &ranges
                        .into_iter()
                        .map(|range| vec![json!(range), Value::Null])
                        .collect::<Vec<_>>(),
                )
                .unwrap();
            let complete = json!({"type":"complete", "account_id":1,
                "source_version": if case == "identity" { 3 } else { 2 },
                "data_until":null, "settings":{"backtest_fee_rate":0.001},
                "row_counts":{"performance_result_range":if case == "count" { 2 } else { 3 }}});
            let result = staged.finish(complete.clone(), 1, 2);
            if case == "valid" {
                assert_eq!(result.unwrap(), complete);
                assert!(connection.is_autocommit());
            } else {
                assert_eq!(result.unwrap_err().code, "INVALID_NATIVE_PUBLICATION");
                assert!(!connection.is_autocommit());
            }
        }
    }

    #[test]
    fn scalar_validation_preserves_null_and_rejects_coercion() {
        let mut column = Column {
            name: "value".into(),
            kind: "FLOAT".into(),
            required: false,
            key: 0,
        };
        assert_eq!(cell(&column, &Value::Null).unwrap(), SqlValue::Null);
        assert_eq!(cell(&column, &json!(0)).unwrap(), SqlValue::Real(0.0));
        assert!(cell(&column, &json!("0")).is_err());
        column.kind = "INTEGER".into();
        assert!(cell(&column, &json!(true)).is_err());
        assert!(cell(&column, &json!(1.0)).is_err());
        column.required = true;
        assert!(cell(&column, &Value::Null).is_err());
    }

    #[test]
    fn complete_rejects_wrong_version_and_missing_rows() {
        let counts = BTreeMap::from([("performance_result_range".into(), 3)]);
        let mut frame = json!({"account_id":1,"source_version":2,"data_until":null,"row_counts":{"performance_result_range":3}});
        validate_complete(&frame, &counts, 1, 2).unwrap();
        assert!(validate_complete(&frame, &counts, 1, 3).is_err());
        frame["row_counts"]["performance_result_range"] = json!(2);
        assert!(validate_complete(&frame, &counts, 1, 2).is_err());
    }
}
