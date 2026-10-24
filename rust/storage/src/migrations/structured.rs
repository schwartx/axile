//! 0034—0041 转换；所有写入均留在迁移引擎持有的事务内。

use chrono::{DateTime, FixedOffset, NaiveDate, NaiveDateTime, TimeZone};
use rusqlite::{params_from_iter, types::Value as SqlValue, Connection, Params};
use serde_json::{Map, Value};

use crate::error::AnalysisError;
use crate::json_codec::number_value;

mod account_identity;
mod analysis;
mod business;
mod direct;
mod full_snapshots;
mod guards;
use crate::json_codec as json;
mod restore;
mod schema;

type Object = Map<String, Value>;
type Result<T> = std::result::Result<T, AnalysisError>;

pub(super) fn upgrade(connection: &Connection, revision: u32) -> Result<()> {
    match revision {
        34 => analysis::upgrade34(connection),
        35 => analysis::upgrade35(connection),
        36 => analysis::upgrade36(connection),
        37 => business::upgrade(connection),
        38 => guards::replace_summary(connection, true),
        39 => direct::upgrade(connection),
        40 => account_identity::upgrade(connection),
        41 => full_snapshots::upgrade(connection),
        _ => Err(error(format!("Unknown structured revision: {revision:04}"))),
    }
}

pub(super) fn downgrade(connection: &Connection, revision: u32) -> Result<()> {
    match revision {
        34 => analysis::downgrade34(connection),
        35 => analysis::downgrade35(connection),
        36 => analysis::downgrade36(connection),
        37 => business::downgrade(connection),
        38 => guards::replace_summary(connection, false),
        39 => direct::downgrade(connection),
        40 => account_identity::downgrade(connection),
        41 => full_snapshots::downgrade(connection),
        _ => Err(error(format!("Unknown structured revision: {revision:04}"))),
    }
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

fn from_sql(value: SqlValue) -> Result<Value> {
    Ok(match value {
        SqlValue::Null => Value::Null,
        SqlValue::Integer(value) => Value::from(value),
        SqlValue::Real(value) => number_value(value),
        SqlValue::Text(value) => Value::String(value),
        SqlValue::Blob(value) => blob_value(value),
    })
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

fn snapshot_ids(connection: &Connection, after: Option<&str>) -> Result<Vec<String>> {
    let found = rows(
        connection,
        "SELECT id FROM performance_snapshot WHERE (?1 IS NULL OR id>?1) ORDER BY id LIMIT 100",
        [after],
    )?;
    found
        .iter()
        .map(|row| text(row, "id").map(str::to_owned))
        .collect()
}

fn savepoint<T>(connection: &Connection, work: impl FnOnce() -> Result<T>) -> Result<T> {
    execute(connection, "SAVEPOINT structured_conversion")?;
    match work() {
        Ok(result) => {
            execute(connection, "RELEASE structured_conversion")?;
            Ok(result)
        }
        Err(failure) => {
            execute(
                connection,
                "ROLLBACK TO structured_conversion; RELEASE structured_conversion",
            )?;
            Err(failure)
        }
    }
}

fn millis(source: &str, shanghai: bool) -> Result<f64> {
    let mut normalized = source.to_owned();
    if let Some((offset, separator)) = source.char_indices().nth(10) {
        if source.as_bytes().get(4) == Some(&b'-')
            && source.as_bytes().get(7) == Some(&b'-')
            && separator != 'T'
        {
            normalized.replace_range(offset..offset + separator.len_utf8(), "T");
        }
    }
    let aware = DateTime::parse_from_rfc3339(&normalized).ok().or_else(|| {
        [
            "%Y-%m-%dT%H:%M:%S%.f%#z",
            "%Y-%m-%dT%H:%M%#z",
            "%Y%m%dT%H%M%S%.f%#z",
        ]
        .iter()
        .find_map(|format| DateTime::parse_from_str(&normalized, format).ok())
    });
    if let Some(parsed) = aware {
        return Ok((parsed.timestamp() as f64
            + f64::from(parsed.timestamp_subsec_micros()) / 1_000_000.0)
            * 1000.0);
    }
    let parsed = ["%Y-%m-%dT%H:%M:%S%.f", "%Y-%m-%dT%H:%M", "%Y%m%dT%H%M%S%.f"]
        .iter()
        .find_map(|format| NaiveDateTime::parse_from_str(&normalized, format).ok())
        .or_else(|| {
            NaiveDate::parse_from_str(&normalized, "%Y-%m-%d")
                .ok()
                .and_then(|day| day.and_hms_opt(0, 0, 0))
        })
        .ok_or_else(|| error("Invalid historical timestamp"))?;
    let instant = if shanghai {
        chrono_tz::Asia::Shanghai
            .from_local_datetime(&parsed)
            .earliest()
            .map(|date| date.fixed_offset())
            .or_else(|| {
                let previous = parsed.checked_sub_signed(chrono::Duration::days(1))?;
                let offset = chrono_tz::Asia::Shanghai
                    .from_local_datetime(&previous)
                    .earliest()?
                    .fixed_offset();
                offset.offset().from_local_datetime(&parsed).single()
            })
    } else {
        FixedOffset::east_opt(8 * 3600)
            .and_then(|offset| offset.from_local_datetime(&parsed).single())
    }
    .ok_or_else(|| error("Invalid historical local timestamp"))?;
    Ok(
        (instant.timestamp() as f64 + f64::from(instant.timestamp_subsec_micros()) / 1_000_000.0)
            * 1000.0,
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn historical_timestamp_modes_keep_shanghai_and_fixed_offset_distinct() {
        assert_eq!(
            millis("2026-10-23T08:00:00", false).unwrap(),
            millis("2026-10-23T00:00:00Z", false).unwrap()
        );
        assert_ne!(
            millis("1990-07-01T08:00:00", true).unwrap(),
            millis("1990-07-01T08:00:00", false).unwrap()
        );
        assert_eq!(
            millis("2026-10-23 08:00:00+0800", false).unwrap(),
            millis("2026-10-23T00:00:00Z", false).unwrap()
        );
        assert_eq!(
            millis("1990-04-15T02:30:00", true).unwrap(),
            millis("1990-04-15T02:30:00+08:00", false).unwrap()
        );
    }
}

pub(super) fn preflight_downgrade_0039(connection: &Connection) -> Result<()> {
    direct::memory_preflight(connection)
}
