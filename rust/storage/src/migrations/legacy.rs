use std::collections::BTreeSet;

use rusqlite::{types::Value as SqlValue, Connection, OptionalExtension, Row};
use serde_json::Value;

use crate::error::AnalysisError;

mod early;
mod errors;
mod later;
mod market_values;
mod settings;

pub(super) use errors::{
    invalidate, patch_document, patch_errors, path_get, path_set, public_codes, restore_errors,
    Change,
};

type Result<T> = std::result::Result<T, AnalysisError>;

fn error(message: impl Into<String>) -> AnalysisError {
    AnalysisError::new("MIGRATION_ERROR", message)
}

fn db(error: rusqlite::Error) -> AnalysisError {
    self::error(error.to_string())
}

fn sql(connection: &Connection, statement: &str) -> Result<()> {
    connection.execute_batch(statement).map_err(db)
}

fn query_rows<T>(
    connection: &Connection,
    statement: &str,
    parameters: impl rusqlite::Params,
    map: impl FnMut(&Row<'_>) -> rusqlite::Result<T>,
) -> Result<Vec<T>> {
    let mut query = connection.prepare(statement).map_err(db)?;
    let rows = query.query_map(parameters, map).map_err(db)?;
    rows.collect::<std::result::Result<_, _>>().map_err(db)
}

fn identifier(value: &str) -> Result<String> {
    if value.is_empty()
        || !value
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || c == b'_')
    {
        return Err(error("invalid migration identifier"));
    }
    Ok(format!("\"{value}\""))
}

fn table_exists(connection: &Connection, name: &str) -> Result<bool> {
    connection
        .query_row(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?1",
            [name],
            |_| Ok(()),
        )
        .optional()
        .map(|row| row.is_some())
        .map_err(db)
}

fn columns(connection: &Connection, table: &str) -> Result<BTreeSet<String>> {
    let mut query = connection
        .prepare(&format!("PRAGMA table_info({})", identifier(table)?))
        .map_err(db)?;
    let rows = query.query_map([], |row| row.get(1)).map_err(db)?;
    rows.collect::<std::result::Result<_, _>>().map_err(db)
}

fn value(row: &Row<'_>, column: &str) -> rusqlite::Result<Value> {
    Ok(match row.get::<_, SqlValue>(column)? {
        SqlValue::Null => Value::Null,
        SqlValue::Integer(value) => value.into(),
        SqlValue::Real(value) => serde_json::json!(value),
        SqlValue::Text(value) => Value::String(value),
        SqlValue::Blob(_) => Value::Null,
    })
}

fn decode(value: &Value) -> Result<Value> {
    match value {
        Value::String(value) => serde_json::from_str(value).map_err(|err| error(err.to_string())),
        _ => Ok(value.clone()),
    }
}

fn document(value: &Value) -> Option<Value> {
    decode(value).ok().filter(Value::is_object)
}

fn encoded(value: &Value) -> Result<String> {
    serde_json::to_string(value).map_err(|err| error(err.to_string()))
}

fn same_json(left: &Value, right: &Value) -> bool {
    if left == right {
        return true;
    }
    match (left, right) {
        (Value::Number(left), Value::Number(right)) => {
            if left.is_f64() || right.is_f64() {
                let (float, integer) = if left.is_f64() {
                    (left, right)
                } else {
                    (right, left)
                };
                let float = float.as_f64().unwrap();
                if let Some(integer) = integer.as_i64() {
                    float >= i64::MIN as f64
                        && float < -(i64::MIN as f64)
                        && float.fract() == 0.0
                        && float as i64 == integer
                } else if let Some(integer) = integer.as_u64() {
                    float >= 0.0
                        && float < 18_446_744_073_709_551_616.0
                        && float.fract() == 0.0
                        && float as u64 == integer
                } else {
                    left.as_f64() == right.as_f64()
                }
            } else {
                false
            }
        }
        (Value::Bool(left), Value::Number(right)) | (Value::Number(right), Value::Bool(left)) => {
            right.as_f64() == Some(if *left { 1.0 } else { 0.0 })
        }
        (Value::Array(left), Value::Array(right)) => {
            left.len() == right.len()
                && left
                    .iter()
                    .zip(right)
                    .all(|(left, right)| same_json(left, right))
        }
        (Value::Object(left), Value::Object(right)) => {
            left.len() == right.len()
                && left
                    .iter()
                    .all(|(key, left)| right.get(key).is_some_and(|right| same_json(left, right)))
        }
        _ => false,
    }
}

fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(value) => *value,
        Value::Number(value) => value.as_f64().is_some_and(|value| value != 0.0),
        Value::String(value) => !value.is_empty(),
        Value::Array(value) => !value.is_empty(),
        Value::Object(value) => !value.is_empty(),
    }
}

pub(super) fn upgrade(connection: &Connection, revision: u32) -> Result<()> {
    match revision {
        1 => sql(connection, include_str!("legacy/0001.sql")),
        2 => sql(connection, include_str!("legacy/0002.sql")),
        3 => early::plugin_network(connection, true),
        4 => { sql(connection, include_str!("legacy/0004.sql"))?; early::asset_snapshots(connection) }
        5 => early::target_snapshots(connection),
        6 => sql(connection, include_str!("legacy/0002_down.sql")),
        7 => sql(connection, include_str!("legacy/0007.sql")),
        8 => early::optional_account_column(connection, "feishu_card_config", "JSON", true),
        9 => sql(connection, "UPDATE account SET account_control_preset='ctp' WHERE trade_channel='ctp' AND account_control_preset='default'"),
        10 => sql(connection, include_str!("legacy/0010.sql")),
        11 => { early::optional_account_column(connection, "backtest_weight_type", "TEXT DEFAULT 'ts' NOT NULL", true)?; early::optional_account_column(connection, "backtest_fee_rate", "FLOAT DEFAULT '0' NOT NULL", true) }
        12 => { sql(connection, include_str!("legacy/0012.sql"))?; early::analysis_triggers(connection) }
        13 => patch_errors(connection, "axile_0013_error_backup", &public_codes(), false).map(|_| ()),
        14 => market_values::upgrade_ctp(connection),
        15 => market_values::upgrade_tq(connection),
        16 => sql(connection, "ALTER TABLE account ADD COLUMN copied_from_account_id INTEGER; ALTER TABLE account ADD COLUMN copied_from_account_name TEXT"),
        17 => sql(connection, "ALTER TABLE account ADD COLUMN execution_notification_mode TEXT DEFAULT 'default' NOT NULL; ALTER TABLE account ADD COLUMN execution_notification_code TEXT; ALTER TABLE account DROP COLUMN feishu_card_config"),
        18 => settings::notification_upgrade(connection),
        19 => settings::notification_type_upgrade(connection),
        20 => settings::upgrade(connection),
        21 => later::notification_result(connection, true),
        22 => sql(connection, include_str!("legacy/0022.sql")),
        23 => { settings::supplement_notification(connection, true)?; sql(connection, "ALTER TABLE account_notification_state ADD COLUMN last_attempt_event_type VARCHAR")?; sql(connection, include_str!("legacy/0023.sql")) }
        24 => sql(connection, include_str!("legacy/0024.sql")),
        25 => sql(connection, include_str!("legacy/0025.sql")),
        26 => sql(connection, "ALTER TABLE account_connection_state ADD COLUMN label TEXT; ALTER TABLE account_connection_state ADD COLUMN presentation JSON"),
        27 => later::control_context(connection, true),
        28 => later::connection_history(connection),
        29 => later::futures_upgrade(connection),
        30 => later::partial_session(connection, true),
        31 => sql(connection, include_str!("legacy/0031.sql")),
        32 => sql(connection, include_str!("legacy/0032.sql")),
        33 => sql(connection, include_str!("legacy/0033.sql")),
        _ => Err(error(format!("unsupported historical revision: {revision:04}"))),
    }
}

pub(super) fn downgrade(connection: &Connection, revision: u32) -> Result<()> {
    match revision {
        1 => sql(connection, include_str!("legacy/0001_down.sql")),
        2 => sql(connection, include_str!("legacy/0002_down.sql")),
        3 => early::plugin_network(connection, false),
        4 => sql(connection, include_str!("legacy/0004_down.sql")),
        5 => sql(connection, include_str!("legacy/0005_down.sql")),
        6 => sql(connection, include_str!("legacy/0002.sql")),
        7 => sql(connection, include_str!("legacy/0007_down.sql")),
        8 => early::optional_account_column(connection, "feishu_card_config", "JSON", false),
        9 | 19 => Ok(()),
        10 => sql(connection, include_str!("legacy/0010_down.sql")),
        11 => sql(connection, "ALTER TABLE account DROP COLUMN backtest_fee_rate; ALTER TABLE account DROP COLUMN backtest_weight_type"),
        12 => early::analysis_downgrade(connection),
        13 => restore_errors(connection, "axile_0013_error_backup", false),
        14 => market_values::restore(connection, 14),
        15 => market_values::restore(connection, 15),
        16 => sql(connection, "ALTER TABLE account DROP COLUMN copied_from_account_name; ALTER TABLE account DROP COLUMN copied_from_account_id"),
        17 => sql(connection, "ALTER TABLE account ADD COLUMN feishu_card_config JSON; ALTER TABLE account DROP COLUMN execution_notification_code; ALTER TABLE account DROP COLUMN execution_notification_mode"),
        18 => settings::notification_downgrade(connection),
        20 => settings::downgrade(connection),
        21 => later::notification_result(connection, false),
        22 => sql(connection, include_str!("legacy/0022_down.sql")),
        23 => { sql(connection, "DROP TABLE notification_event; DROP TABLE supplement_step; DROP TABLE supplement_group; ALTER TABLE account_notification_state DROP COLUMN last_attempt_event_type; UPDATE account_settings SET schedule=json_remove(schedule, '$.supplement')")?; settings::supplement_notification(connection, false) }
        24 => sql(connection, include_str!("legacy/0024_down.sql")),
        25 => sql(connection, include_str!("legacy/0025_down.sql")),
        26 => sql(connection, "ALTER TABLE account_connection_state DROP COLUMN presentation; ALTER TABLE account_connection_state DROP COLUMN label"),
        27 => later::control_context(connection, false),
        28 => sql(connection, "ALTER TABLE account_connection_state DROP COLUMN manually_disconnected; ALTER TABLE account_connection_state DROP COLUMN last_failure; ALTER TABLE account_connection_state DROP COLUMN last_success_at"),
        29 => later::futures_downgrade(connection),
        30 => later::partial_session(connection, false),
        31 => sql(connection, include_str!("legacy/0031_down.sql")),
        32 => sql(connection, include_str!("legacy/0032_down.sql")),
        33 => sql(connection, include_str!("legacy/0033_down.sql")),
        _ => Err(error(format!("unsupported historical revision: {revision:04}"))),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn historical_chain_preserves_account_settings_and_referring_rows() {
        let connection = Connection::open_in_memory().unwrap();
        sql(&connection, "PRAGMA foreign_keys=OFF; BEGIN IMMEDIATE").unwrap();
        upgrade(&connection, 1).unwrap();
        sql(&connection, "INSERT INTO portfolio(name,market,custom_calc_py_code,id,updated_at,created_at) VALUES('p','futures','',2,'b','a'); INSERT INTO account(name,market,trade_channel,account_control_preset,account_control_override,account_config,is_started,cron_expr,brokerage,weight_precision,algorithm,id,updated_at,created_at) VALUES('a','futures','ctp','default','null','{\"broker\":\"saved\"}',1,'* * * * *','',0.25,'{\"params\":{\"offset_priority\":\"今昨\"}}',1,'b','a'); INSERT INTO portfolioaccount(account_id,portfolio_id,created_at,id) VALUES(1,2,'a',1); INSERT INTO executerecord(execution_id,raw_input,raw_result,is_success,id,account_id,created_at) VALUES('e','{\"curr_target\":{}}','{\"account_assets\":{\"market_value\":0}}',1,1,1,'b')").unwrap();
        for revision in 2..=33 {
            upgrade(&connection, revision)
                .unwrap_or_else(|error| panic!("upgrade {revision:04}: {error}"));
        }
        assert_eq!(connection.query_row("SELECT json_extract(connection,'$.broker') FROM account_settings WHERE account_id=1", [], |row|row.get::<_,String>(0)).unwrap(), "saved");
        assert_eq!(
            connection
                .query_row("SELECT count(*) FROM account_asset_snapshot", [], |row| row
                    .get::<_, i64>(0))
                .unwrap(),
            1
        );
        for revision in (20..=33).rev() {
            downgrade(&connection, revision)
                .unwrap_or_else(|error| panic!("downgrade {revision:04}: {error}"));
        }
        assert_eq!(
            connection
                .query_row("SELECT account_config FROM account WHERE id=1", [], |row| {
                    row.get::<_, String>(0)
                })
                .unwrap(),
            "{\"broker\":\"saved\"}"
        );
        assert_eq!(
            connection
                .query_row(
                    "SELECT count(*) FROM executerecord WHERE account_id=1",
                    [],
                    |row| row.get::<_, i64>(0)
                )
                .unwrap(),
            1
        );
        assert_eq!(
            connection
                .query_row(
                    "SELECT count(*) FROM portfolioaccount WHERE account_id=1",
                    [],
                    |row| row.get::<_, i64>(0)
                )
                .unwrap(),
            1
        );
        for revision in (1..=19).rev() {
            downgrade(&connection, revision)
                .unwrap_or_else(|error| panic!("downgrade {revision:04}: {error}"));
        }
        assert!(!table_exists(&connection, "account").unwrap());
        sql(&connection, "ROLLBACK").unwrap();
    }

    #[test]
    fn target_snapshot_indexes_preserve_uniqueness_and_lookup_columns_through_head() {
        let connection = Connection::open_in_memory().unwrap();
        sql(&connection, "PRAGMA foreign_keys=OFF; BEGIN IMMEDIATE").unwrap();
        for revision in 1..=33 {
            upgrade(&connection, revision).unwrap();
        }
        for revision in [33, 34] {
            if revision == 34 {
                crate::migrations::structured::upgrade(&connection).unwrap();
            }
            let mut query = connection
                .prepare("PRAGMA index_list(target_weight_snapshot)")
                .unwrap();
            let indexes = query
                .query_map([], |row| {
                    Ok((row.get::<_, String>(1)?, row.get::<_, bool>(2)?))
                })
                .unwrap()
                .collect::<std::result::Result<std::collections::BTreeMap<_, _>, _>>()
                .unwrap();
            assert_eq!(indexes.len(), 3, "revision {revision:04}");
            for (name, unique, expected) in [
                (
                    "ix_target_weight_snapshot_portfolio_id_id",
                    false,
                    vec!["portfolio_id", "id"],
                ),
                (
                    "ix_target_weight_snapshot_account_id_id",
                    false,
                    vec!["account_id", "id"],
                ),
                (
                    "ix_target_weight_snapshot_execution_id",
                    true,
                    vec!["execution_id"],
                ),
            ] {
                assert_eq!(
                    indexes.get(name),
                    Some(&unique),
                    "revision {revision:04}: {name}"
                );
                let mut query = connection
                    .prepare(&format!("PRAGMA index_info({name})"))
                    .unwrap();
                let columns = query
                    .query_map([], |row| row.get::<_, String>(2))
                    .unwrap()
                    .collect::<std::result::Result<Vec<_>, _>>()
                    .unwrap();
                assert_eq!(columns, expected, "revision {revision:04}: {name}");
            }
        }
        sql(&connection, "ROLLBACK").unwrap();
    }

    #[test]
    fn json_backup_equality_matches_python_numbers_without_rounding_large_integers() {
        assert!(same_json(
            &serde_json::json!({"value":1}),
            &serde_json::json!({"value":1.0})
        ));
        assert!(!same_json(
            &serde_json::json!(9_007_199_254_740_993_i64),
            &serde_json::json!(9_007_199_254_740_992.0)
        ));
    }
}
