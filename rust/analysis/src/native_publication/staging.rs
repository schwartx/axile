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
    "performance_execution_summary",
    "performance_execution_fee",
];

#[derive(Debug)]
struct Column {
    name: String,
    kind: String,
    required: bool,
    key: i64,
}

#[derive(Debug)]
struct Table {
    columns: Vec<Column>,
}

pub(super) struct Staging<'a> {
    connection: &'a Connection,
    tables: BTreeMap<&'static str, Table>,
    counts: BTreeMap<String, usize>,
    complete: Option<Value>,
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
            tables.insert(name, Table { columns });
        }
        connection.execute_batch("BEGIN DEFERRED;").map_err(sql)?;
        Ok(Self {
            connection,
            tables,
            counts: BTreeMap::new(),
            complete: None,
        })
    }

    pub(super) fn accept(&mut self, frame: &Value) -> Result<(), AnalysisError> {
        if self.complete.is_some() {
            return Err(invalid("frame follows complete"));
        }
        match frame.get("type").and_then(Value::as_str) {
            Some("rows") => self.insert_rows(frame),
            Some("complete") => {
                self.complete = Some(frame.clone());
                Ok(())
            }
            _ => Err(invalid("unknown publication frame")),
        }
    }

    fn insert_rows(&mut self, frame: &Value) -> Result<(), AnalysisError> {
        let name = frame["table"]
            .as_str()
            .ok_or_else(|| invalid("missing result table"))?;
        let table = self
            .tables
            .get(name)
            .ok_or_else(|| invalid("unknown result table"))?;
        let columns = checked_columns(frame, table)?;
        let rows = frame["rows"]
            .as_array()
            .filter(|rows| !rows.is_empty() && rows.len() <= 256)
            .ok_or_else(|| invalid("invalid publication batch size"))?;
        let names = columns
            .iter()
            .map(|column| format!("\"{}\"", column.name))
            .collect::<Vec<_>>()
            .join(",");
        let parameters = vec!["?"; columns.len()].join(",");
        let query = format!("INSERT INTO {STAGE}.\"{name}\" ({names}) VALUES ({parameters})");
        let mut statement = self.connection.prepare_cached(&query).map_err(sql)?;
        for row in rows {
            let cells = row
                .as_array()
                .filter(|cells| cells.len() == columns.len())
                .ok_or_else(|| invalid("result row width mismatch"))?;
            let values = columns
                .iter()
                .zip(cells)
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
        account_id: i64,
        source_version: i64,
    ) -> Result<Value, AnalysisError> {
        let complete = self
            .complete
            .ok_or_else(|| invalid("publication ended without complete"))?;
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
                "TEXT" | "INTEGER" | "FLOAT" | "REAL" | "BOOLEAN"
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

fn checked_columns<'a>(frame: &Value, table: &'a Table) -> Result<Vec<&'a Column>, AnalysisError> {
    let names = frame["columns"]
        .as_array()
        .ok_or_else(|| invalid("missing result columns"))?;
    let mut seen = BTreeSet::new();
    let mut columns = Vec::new();
    for name in names {
        let name = name
            .as_str()
            .ok_or_else(|| invalid("invalid result column"))?;
        if !seen.insert(name) {
            return Err(invalid("duplicate result column"));
        }
        let column = table
            .columns
            .iter()
            .find(|column| column.name == name)
            .ok_or_else(|| invalid("unknown result column"))?;
        columns.push(column);
    }
    if columns.len() != table.columns.len() {
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
