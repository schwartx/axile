//! 从已发布的 0033 直接迁至最终结构；所有转换由外层事务原子提交。

use crate::error::AnalysisError;
use crate::json_codec::{self as json, number_value};
use rusqlite::{params_from_iter, types::Value as SqlValue, Connection, Params};
use serde_json::{Map, Value};

mod account_identity;
mod business;
mod guards;
mod schema;
mod times;

type Object = Map<String, Value>;
type Result<T> = std::result::Result<T, AnalysisError>;

pub(super) fn create(connection: &Connection) -> Result<()> {
    execute(connection, include_str!("schema_0034.sql"))?;
    execute(connection, include_str!("structured/business.sql"))?;
    execute(connection, include_str!("../analysis/schema.sql"))?;
    times::upgrade(connection)?;
    guards::install_business(connection)?;
    guards::install_analysis(connection)?;
    guards::install_incremental(connection)
}

pub(super) fn upgrade(connection: &Connection) -> Result<()> {
    // 0033 predates this namespace; reject collisions before replacing triggers.
    if let Some(trigger) = rows(
        connection,
        "SELECT name FROM sqlite_schema WHERE type='trigger' AND lower(name) GLOB 'business_facts_0034_*' ORDER BY name LIMIT 1",
        [],
    )?.first() {
        return Err(error(format!(
            "Cannot upgrade 0033: reserved trigger name {}; rename it before migrating",
            text(trigger, "name")?
        )));
    }
    check_analysis_dependencies(connection, super::BASELINE_REVISION)?;
    guards::drop_analysis(connection)?;
    let state = rows(
        connection,
        "SELECT account_id,source_version FROM account_analysis",
        [],
    )?;
    for table in [
        "performance_cost_trade",
        "performance_cost_execution",
        "performance_snapshot",
        "account_analysis",
    ] {
        execute(connection, &format!("DROP TABLE {table}"))?;
    }
    execute(connection, include_str!("structured/analysis.sql"))?;
    execute(connection, include_str!("../analysis/schema.sql"))?;
    requeue_analysis(connection, state, "retry_at_us")?;
    business::upgrade(connection)?;
    account_identity::upgrade(connection)?;
    super::execution_views::upgrade(connection)?;
    times::upgrade(connection)?;
    schema::install_query_indexes(connection)?;
    guards::install_analysis(connection)?;
    guards::install_incremental(connection)
}

pub(super) fn downgrade(connection: &Connection) -> Result<()> {
    preflight_downgrade(connection)?;
    guards::drop_analysis(connection)?;
    let state = rows(
        connection,
        "SELECT account_id,source_version FROM account_analysis",
        [],
    )?;
    schema::drop_query_indexes(connection)?;
    times::downgrade(connection)?;
    business::downgrade(connection)?;
    account_identity::downgrade(connection)?;
    for table in crate::analysis::TABLES
        .iter()
        .chain(["analysis_calendar_day", "account_analysis"].iter())
    {
        execute(connection, &format!("DROP TABLE {}", quote(table)))?;
    }
    execute(connection, include_str!("structured/analysis_0033.sql"))?;
    requeue_analysis(connection, state, "retry_at")
}

fn requeue_analysis(connection: &Connection, state: Vec<Object>, retry: &str) -> Result<()> {
    for row in state {
        insert(connection, "account_analysis", &row)?;
    }
    execute(connection,&format!("UPDATE account_analysis SET source_version=source_version+1,requested=1,running_version=NULL,failures=0,{retry}=0,error=NULL"))
}

pub(super) fn preflight_downgrade(connection: &Connection) -> Result<()> {
    check_analysis_dependencies(connection, super::PUBLIC_HEAD_NUMBER)?;
    business::preflight_downgrade(connection)
}

pub(super) fn check_analysis_dependencies(
    connection: &Connection,
    source_revision: u32,
) -> Result<()> {
    let known_columns: &[&str] = if source_revision == super::BASELINE_REVISION {
        &[
            "account_id",
            "source_version",
            "published_version",
            "current_snapshot",
            "previous_snapshot",
            "requested",
            "running_version",
            "failures",
            "retry_at",
            "error",
            "logic_version",
            "engine_version",
        ]
    } else {
        &[
            "account_id",
            "source_version",
            "requested",
            "running_version",
            "failures",
            "retry_at_us",
            "error",
            "running_token",
            "dirty_record",
        ]
    };
    for column in rows(connection, "PRAGMA table_xinfo(account_analysis)", [])? {
        let name = text(&column, "name")?;
        if !known_columns.contains(&name) {
            return Err(error(format!(
                "Cannot rebuild account_analysis: custom column {name}; preserve or remove it before migrating"
            )));
        }
    }
    let dependencies = rows(connection,
        "SELECT name FROM sqlite_schema WHERE tbl_name='account_analysis' AND type IN ('index','trigger') AND sql IS NOT NULL ORDER BY name", [])?;
    if let Some(dependency) = dependencies.first() {
        return Err(error(format!(
            "Cannot rebuild account_analysis: custom dependency {}; preserve or remove it before migrating",
            text(dependency, "name")?
        )));
    }
    Ok(())
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn analysis_rebuild_rejects_custom_dependencies_before_removing_them() {
        let connection = Connection::open_in_memory().unwrap();
        connection
            .execute_batch(include_str!("../testdata/migration_0033.sql"))
            .unwrap();
        connection.execute_batch("CREATE TRIGGER custom_queue_audit AFTER UPDATE ON account_analysis BEGIN SELECT 1; END").unwrap();
        assert!(upgrade(&connection)
            .unwrap_err()
            .error
            .contains("custom_queue_audit"));
        assert!(connection
            .prepare("SELECT raw_result FROM executerecord")
            .is_ok());
        assert_eq!(
            connection
                .query_row(
                    "SELECT count(*) FROM sqlite_schema WHERE name='custom_queue_audit'",
                    [],
                    |row| row.get::<_, i64>(0)
                )
                .unwrap(),
            1
        );

        let final_connection = Connection::open_in_memory().unwrap();
        crate::calendar::register_time_function(&final_connection).unwrap();
        create(&final_connection).unwrap();
        final_connection
            .execute_batch("CREATE INDEX custom_queue_version ON account_analysis(source_version)")
            .unwrap();
        assert!(downgrade(&final_connection)
            .unwrap_err()
            .error
            .contains("custom_queue_version"));
        assert!(final_connection
            .prepare("SELECT dirty_record FROM account_analysis")
            .is_ok());
        assert_eq!(
            final_connection
                .query_row(
                    "SELECT count(*) FROM sqlite_schema WHERE name='custom_queue_version'",
                    [],
                    |row| row.get::<_, i64>(0)
                )
                .unwrap(),
            1
        );
    }

    #[test]
    fn direct_upgrade_and_downgrade_preserve_facts_and_unrelated_schema() {
        let connection = Connection::open_in_memory().unwrap();
        connection.execute_batch("PRAGMA foreign_keys=OFF").unwrap();
        crate::calendar::register_time_function(&connection).unwrap();
        connection
            .execute_batch(include_str!("../testdata/migration_0033.sql"))
            .unwrap();
        connection.execute_batch("CREATE TABLE unrelated_evidence(value TEXT); CREATE TRIGGER unrelated_write AFTER INSERT ON unrelated_evidence BEGIN SELECT 1; END;
            INSERT INTO account(id,name,market,trade_channel,brokerage,created_at,updated_at) VALUES(1,'saved','FUTURES','ctp','','2026-10-28','2026-10-28');
            INSERT INTO executerecord(id,account_id,execution_id,is_success,created_at,raw_input,raw_result) VALUES(7,1,'saved-execution',1,'2026-10-28','{\"curr_target\":{\"A\":0.5}}','{\"status\":\"SUCCEEDED\",\"success\":true,\"symbol_results\":{},\"extra\":{\"zero\":0,\"flag\":false,\"missing\":null}}');").unwrap();
        for _ in 0..2 {
            upgrade(&connection).unwrap();
            let result = crate::business_detail::read(&connection, 1, 7).unwrap();
            assert_eq!(result["input"]["curr_target"]["A"], serde_json::json!(0.5));
            assert_eq!(
                result["result"]["extra"],
                serde_json::json!({"zero":0,"flag":false,"missing":null})
            );
            let retired: i64 = connection
                .query_row(
                    "SELECT count(*) FROM sqlite_schema WHERE name LIKE 'performance_%'",
                    [],
                    |row| row.get(0),
                )
                .unwrap();
            assert_eq!(retired, 0);
            downgrade(&connection).unwrap();
            let payload: String = connection
                .query_row(
                    "SELECT raw_result FROM executerecord WHERE id=7",
                    [],
                    |row| row.get(0),
                )
                .unwrap();
            let result: Value = serde_json::from_str(&payload).unwrap();
            assert_eq!(
                result["extra"],
                serde_json::json!({"zero":0,"flag":false,"missing":null})
            );
            let retained: i64 = connection.query_row("SELECT count(*) FROM sqlite_schema WHERE name IN ('unrelated_evidence','unrelated_write')", [], |row| row.get(0)).unwrap();
            assert_eq!(retained, 2);
        }
    }

    #[test]
    fn new_database_builds_only_final_schema_and_enforces_fact_ownership() {
        let connection = Connection::open_in_memory().unwrap();
        crate::calendar::register_time_function(&connection).unwrap();
        create(&connection).unwrap();
        let count: i64 = connection.query_row("SELECT count(*) FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'", [], |row| row.get(0)).unwrap();
        assert_eq!(count, 56);
        let retired: i64 = connection
            .query_row(
                "SELECT count(*) FROM sqlite_schema WHERE name LIKE 'performance_%'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(retired, 0);
        let hidden: i64 = connection.query_row("SELECT count(*) FROM pragma_table_info('executerecord') WHERE name='history_visible'", [], |row| row.get(0)).unwrap();
        assert_eq!(hidden, 0);
        let final_columns: i64 = connection.query_row("SELECT (SELECT count(*) FROM pragma_table_info('executerecord') WHERE name='result_staged') + (SELECT count(*) FROM pragma_table_info('account_analysis') WHERE name='dirty_record')", [], |row| row.get(0)).unwrap();
        assert_eq!(final_columns, 2);
        connection.execute_batch("INSERT INTO account(id,name,market,trade_channel,brokerage,created_at_us,updated_at_us) VALUES(1,'one','FUTURES','ctp','',0,0),(2,'two','FUTURES','ctp','',0,0);").unwrap();
        let input = crate::business_facts_ops::encode::encode_inputs(
            "owned",
            &serde_json::json!({}),
            1,
            None,
            "2026-10-28",
            None,
        )
        .unwrap();
        crate::business_facts_ops::write_input(&connection, &input).unwrap();
        let failure = connection.execute("INSERT INTO executerecord(id,account_id,execution_id,is_success,created_at_us,input_id) VALUES(1,2,'wrong-owner',1,0,'owned')", []).unwrap_err();
        assert!(
            failure
                .to_string()
                .contains("Execution input owner mismatch"),
            "{failure:?}"
        );
        connection.execute("INSERT INTO executerecord(id,account_id,execution_id,is_success,created_at_us,input_id) VALUES(1,1,'owned-execution',1,0,'owned')", []).unwrap();
        connection.execute_batch("UPDATE account_analysis SET source_version=7,requested=0,dirty_record=NULL WHERE account_id=1;UPDATE executerecord SET result_staged=1 WHERE id=1;").unwrap();
        let (version, requested, dirty): (i64, bool, i64) = connection.query_row("SELECT source_version,requested,dirty_record FROM account_analysis WHERE account_id=1", [], |row| Ok((row.get(0)?,row.get(1)?,row.get(2)?))).unwrap();
        assert!(version > 7);
        assert!(requested);
        assert_eq!(dirty, 1);
        connection
            .execute("DELETE FROM account WHERE id=1", [])
            .unwrap();
        for table in ["executerecord", "execution_input", "account_analysis"] {
            let rows: i64 = connection
                .query_row(&format!("SELECT count(*) FROM {table}"), [], |row| {
                    row.get(0)
                })
                .unwrap();
            assert_eq!(rows, 0, "{table}");
        }
    }
}
