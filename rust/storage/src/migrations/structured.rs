//! 从已发布的 0033 直接迁至最终结构；所有转换由外层事务原子提交。

use crate::error::AnalysisError;
use crate::json_codec::{self as json, number_value};
use rusqlite::{params_from_iter, types::Value as SqlValue, Connection, Params};
use serde_json::{Map, Value};

mod account_identity;
mod business;
mod guards;
mod schema;

type Object = Map<String, Value>;
type Result<T> = std::result::Result<T, AnalysisError>;

pub(super) fn upgrade(connection: &Connection) -> Result<()> {
    reset_analysis(connection)?;
    for table in [
        "performance_cost_trade",
        "performance_cost_execution",
        "performance_snapshot",
    ] {
        execute(connection, &format!("DROP TABLE {table}"))?;
    }
    execute(
        connection,
        "ALTER TABLE account_analysis ADD COLUMN running_token TEXT",
    )?;
    execute(connection, include_str!("structured/analysis.sql"))?;
    business::upgrade(connection)?;
    account_identity::upgrade(connection)?;
    guards::install_analysis(connection)
}

pub(super) fn downgrade(connection: &Connection) -> Result<()> {
    business::downgrade(connection)?;
    account_identity::downgrade(connection)?;
    reset_analysis(connection)?;
    execute(
        connection,
        "DROP TRIGGER analysis_account_channel; DROP TRIGGER analysis_snapshot_delete",
    )?;
    execute(
        connection,
        "ALTER TABLE account_analysis DROP COLUMN running_token",
    )?;
    for table in schema::ANALYSIS_TABLES {
        execute(connection, &format!("DROP TABLE {}", quote(table)))?;
    }
    let legacy = include_str!("legacy/0012.sql");
    execute(
        connection,
        &legacy[legacy
            .find("CREATE TABLE performance_snapshot")
            .expect("frozen cache schema")..],
    )
}

fn reset_analysis(connection: &Connection) -> Result<()> {
    execute(
        connection,
        "UPDATE account_analysis SET
        source_version=source_version+1,published_version=0,
        requested=(requested OR current_snapshot IS NOT NULL OR running_version IS NOT NULL),
        current_snapshot=NULL,previous_snapshot=NULL,running_version=NULL,
        failures=0,retry_at=0,error=NULL,logic_version=NULL,engine_version=NULL",
    )
}

pub(super) fn preflight_downgrade(connection: &Connection) -> Result<()> {
    business::preflight_downgrade(connection)
}

fn error(message: impl Into<String>) -> AnalysisError {
    AnalysisError::new("MIGRATION_ERROR", message)
}

fn sql(error: rusqlite::Error) -> AnalysisError {
    AnalysisError::new("MIGRATION_SQL", error.to_string())
}

fn quote(name: &str) -> String {
    format!("\"{}\"", name.replace('"', "\"\""))
}

fn execute(connection: &Connection, statement: &str) -> Result<()> {
    connection.execute_batch(statement).map_err(sql)
}

fn rows(connection: &Connection, statement: &str, parameters: impl Params) -> Result<Vec<Object>> {
    let mut query = connection.prepare(statement).map_err(sql)?;
    let names: Vec<String> = query
        .column_names()
        .iter()
        .map(|name| (*name).to_owned())
        .collect();
    let mapped = query
        .query_map(parameters, |row| {
            let mut result = Object::new();
            for (index, name) in names.iter().enumerate() {
                let value = match row.get::<_, SqlValue>(index)? {
                    SqlValue::Null => Value::Null,
                    SqlValue::Integer(value) => Value::from(value),
                    SqlValue::Real(value) => number_value(value),
                    SqlValue::Text(value) => Value::String(value),
                    SqlValue::Blob(value) => blob_value(value),
                };
                result.insert(name.clone(), value);
            }
            Ok(result)
        })
        .map_err(sql)?;
    mapped.collect::<std::result::Result<_, _>>().map_err(sql)
}

fn one(connection: &Connection, statement: &str, parameters: impl Params) -> Result<Object> {
    let mut result = rows(connection, statement, parameters)?;
    if result.len() != 1 {
        return Err(error("Expected exactly one historical row"));
    }
    Ok(result.remove(0))
}

fn value<'a>(row: &'a Object, name: &str) -> &'a Value {
    row.get(name).unwrap_or(&Value::Null)
}

fn text<'a>(row: &'a Object, name: &str) -> Result<&'a str> {
    value(row, name)
        .as_str()
        .ok_or_else(|| error(format!("Missing historical text: {name}")))
}

fn integer(row: &Object, name: &str) -> Result<i64> {
    value(row, name)
        .as_i64()
        .ok_or_else(|| error(format!("Missing historical integer: {name}")))
}

fn json_value(value: &Value) -> Result<Value> {
    match value {
        Value::String(source) => json::parse(source).map_err(error),
        Value::Object(source)
            if source.len() == 1 && source.contains_key("$historical_sqlite_blob") =>
        {
            use base64::Engine;
            let encoded = value
                .get("$historical_sqlite_blob")
                .and_then(Value::as_str)
                .ok_or_else(|| error("Invalid historical SQLite BLOB"))?;
            let bytes = base64::engine::general_purpose::STANDARD
                .decode(encoded)
                .map_err(|_| error("Invalid historical SQLite BLOB"))?;
            json::parse_bytes(&bytes).map_err(error)
        }
        other => Ok(other.clone()),
    }
}

fn blob_value(value: Vec<u8>) -> Value {
    use base64::Engine;
    serde_json::json!({"$historical_sqlite_blob":base64::engine::general_purpose::STANDARD.encode(value)})
}

fn db_value(value: &Value) -> Result<SqlValue> {
    Ok(match value {
        Value::Null => SqlValue::Null,
        Value::Bool(value) => SqlValue::Integer(i64::from(*value)),
        Value::Number(value) => match value.as_i64() {
            Some(value) => SqlValue::Integer(value),
            None => SqlValue::Real(
                value
                    .as_f64()
                    .or_else(|| value.to_string().parse::<f64>().ok())
                    .ok_or_else(|| error("Invalid historical number"))?,
            ),
        },
        Value::String(value) => SqlValue::Text(value.clone()),
        other => SqlValue::Text(serde_json::to_string(other).map_err(|e| error(e.to_string()))?),
    })
}

fn insert(connection: &Connection, table: &str, row: &Object) -> Result<()> {
    let columns = row
        .keys()
        .map(|key| quote(key))
        .collect::<Vec<_>>()
        .join(",");
    let marks = vec!["?"; row.len()].join(",");
    let parameters = row.values().map(db_value).collect::<Result<Vec<_>>>()?;
    connection
        .execute(
            &format!("INSERT INTO {} ({columns}) VALUES ({marks})", quote(table)),
            params_from_iter(parameters),
        )
        .map_err(sql)?;
    Ok(())
}

fn update(
    connection: &Connection,
    table: &str,
    row: &Object,
    predicate: &str,
    keys: &[Value],
) -> Result<()> {
    let assignments = row
        .keys()
        .map(|key| format!("{}=?", quote(key)))
        .collect::<Vec<_>>()
        .join(",");
    let mut parameters = row.values().map(db_value).collect::<Result<Vec<_>>>()?;
    parameters.extend(keys.iter().map(db_value).collect::<Result<Vec<_>>>()?);
    connection
        .execute(
            &format!(
                "UPDATE {} SET {assignments} WHERE {predicate}",
                quote(table)
            ),
            params_from_iter(parameters),
        )
        .map_err(sql)?;
    Ok(())
}

fn page(connection: &Connection, table: &str, after: i64) -> Result<Vec<Object>> {
    rows(
        connection,
        &format!(
            "SELECT * FROM {} WHERE id>? ORDER BY id LIMIT 100",
            quote(table)
        ),
        [after],
    )
}
