//! 执行编排所需的有界证据读取；同一阶段共用只读快照。

use std::path::Path;

use rusqlite::{params, Connection, OptionalExtension};
use serde_json::{json, Map, Value};

use crate::business_facts_ops::{find_execution_input, result_symbols, summary_refs};
use crate::{business_detail, error::AnalysisError, storage_connection};

fn invalid(message: impl Into<String>) -> AnalysisError {
    AnalysisError::new("EXECUTION_READ_ERROR", message.into())
}

fn sql(error: rusqlite::Error) -> AnalysisError {
    invalid(error.to_string())
}

fn text<'a>(request: &'a Value, key: &str) -> Result<&'a str, AnalysisError> {
    request
        .get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| invalid(format!("执行读取字段 {key} 必须是字符串")))
}

fn integer(request: &Value, key: &str) -> Result<i64, AnalysisError> {
    request
        .get(key)
        .and_then(Value::as_i64)
        .ok_or_else(|| invalid(format!("执行读取字段 {key} 必须是整数")))
}

fn record_on(
    connection: &Connection,
    execution_id: &str,
    account_id: Option<i64>,
) -> Result<Option<Map<String, Value>>, AnalysisError> {
    connection
        .query_row(
            "SELECT * FROM executerecord WHERE execution_id=?1
             AND (?2 IS NULL OR account_id=?2) ORDER BY id DESC LIMIT 1",
            params![execution_id, account_id],
            |row| Ok(business_detail::decode(row, "executerecord")),
        )
        .optional()
        .map_err(sql)?
        .transpose()
        .map_err(invalid)
}

fn persisted_status(connection: &Connection, request: &Value) -> Result<Value, AnalysisError> {
    let record = record_on(connection, text(request, "execution_id")?, None)?;
    let symbols = match record.as_ref() {
        Some(record) => result_symbols::read_outcome_symbols(
            connection,
            record["id"]
                .as_i64()
                .ok_or_else(|| invalid("执行记录身份无效"))?,
        )?,
        None => json!([]),
    };
    Ok(json!({"record": record, "outcome_symbols": symbols}))
}

fn input_target(connection: &Connection, input_id: &str) -> Result<Value, AnalysisError> {
    let (state, count): (String, Option<i64>) = connection
        .query_row(
            "SELECT curr_target_state,curr_target_count FROM execution_input WHERE id=?1",
            [input_id],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .optional()
        .map_err(sql)?
        .ok_or_else(|| invalid("Execution input does not exist"))?;
    if state == "absent" {
        return Ok(json!({"present":false,"target":null}));
    }
    if state == "null" || state == "invalid" {
        return Ok(json!({"present":true,"target":null}));
    }
    let mut statement = connection
        .prepare(
            "SELECT map_key,value FROM execution_input_target
        WHERE input_id=?1 AND role='curr_target' ORDER BY map_key",
        )
        .map_err(sql)?;
    let target = statement
        .query_map([input_id], |row| {
            Ok((
                row.get::<_, String>(0)?,
                json!(row.get::<_, Option<f64>>(1)?),
            ))
        })
        .map_err(sql)?
        .collect::<Result<Map<_, _>, _>>()
        .map_err(sql)?;
    if count != Some(target.len() as i64) {
        return Err(invalid("Incomplete frozen target facts"));
    }
    Ok(json!({"present":true,"target":target}))
}

fn sizing_context(connection: &Connection, input_id: &str) -> Result<Value, AnalysisError> {
    connection
        .query_row(
            "SELECT sizing_context_state,long_leverage,short_leverage,weight_precision
        FROM execution_input WHERE id=?1",
            [input_id],
            |row| {
                let state: String = row.get(0)?;
                let mut fields = Map::new();
                if state == "value" {
                    for (index, name) in [
                        (1, "long_leverage"),
                        (2, "short_leverage"),
                        (3, "weight_precision"),
                    ] {
                        if let Some(value) = row.get::<_, Option<f64>>(index)? {
                            fields.insert(name.into(), json!(value));
                        }
                    }
                }
                Ok(Value::Object(fields))
            },
        )
        .optional()
        .map_err(sql)?
        .ok_or_else(|| invalid("Execution input does not exist"))
}

fn last_target(connection: &Connection, request: &Value) -> Result<Value, AnalysisError> {
    let input: Option<Option<String>> = connection
        .query_row(
            "SELECT input_id FROM executerecord WHERE account_id=?1 AND is_success=1
             ORDER BY id DESC LIMIT 1",
            [integer(request, "account_id")?],
            |row| row.get(0),
        )
        .optional()
        .map_err(sql)?;
    match input.flatten() {
        Some(input_id) => input_target(connection, &input_id),
        None => Ok(json!({"present":false,"target":null})),
    }
}

fn target_sizing(connection: &Connection, request: &Value) -> Result<Value, AnalysisError> {
    let execution_id = text(request, "execution_id")?;
    let account_id = match request.get("account_id") {
        None | Some(Value::Null) => None,
        Some(_) => Some(integer(request, "account_id")?),
    };
    let record = if account_id.is_some() {
        record_on(connection, execution_id, account_id)?
    } else {
        None
    };
    let mut statement = connection
        .prepare(
            "SELECT lower(artifact_type),schema_version FROM execution_artifact
             WHERE execution_id=?1 AND lower(artifact_type) IN ('target_snapshot','execution_summary')",
        )
        .map_err(sql)?;
    let versions = statement
        .query_map([execution_id], |row| {
            Ok((row.get::<_, String>(0)?, json!(row.get::<_, i64>(1)?)))
        })
        .map_err(sql)?
        .collect::<Result<Map<_, _>, _>>()
        .map_err(sql)?;
    let mut input_id =
        summary_refs::read_artifact_input_id(connection, execution_id, "targets", account_id)?;
    if input_id.is_none() {
        input_id = record
            .as_ref()
            .and_then(|row| row.get("input_id"))
            .and_then(Value::as_str)
            .map(str::to_owned);
    }
    if input_id.is_none() {
        if let Some(account_id) = account_id {
            input_id = find_execution_input(connection, account_id, execution_id)?;
        }
    }
    let target_context = match &input_id {
        Some(input_id) => sizing_context(connection, input_id)?,
        None => json!({}),
    };
    let record_id = record
        .as_ref()
        .and_then(|row| row.get("id"))
        .and_then(Value::as_i64);
    let sizings = match record_id {
        Some(record_id) => result_symbols::read_sizing_rows(connection, record_id)?,
        None => json!({}),
    };
    Ok(
        json!({"artifact_versions": versions, "record_id": record_id, "input_id": input_id,
        "target_context": target_context, "sizings": sizings}),
    )
}

fn read_on(connection: &Connection, request: &Value) -> Result<Value, AnalysisError> {
    match text(request, "op")? {
        "status" => persisted_status(connection, request),
        "outcome_symbols" => {
            result_symbols::read_outcome_symbols(connection, integer(request, "record_id")?)
        }
        "target_sizing" => target_sizing(connection, request),
        "last_target" => last_target(connection, request),
        _ => Err(invalid("未知执行读取阶段")),
    }
}

pub fn execution_read(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let mut connection = storage_connection::open_reader(Path::new(path))?;
    let transaction = connection.transaction().map_err(sql)?;
    read_on(&transaction, request)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture() -> Connection {
        let connection = Connection::open_in_memory().unwrap();
        connection.execute_batch("CREATE TABLE executerecord(id INTEGER PRIMARY KEY,
            account_id INTEGER,is_success INTEGER,input_id TEXT,execution_id TEXT,created_at TEXT);
            CREATE TABLE execution_input(id TEXT,curr_target_state TEXT,curr_target_count INTEGER,
              sizing_context_state TEXT,long_leverage REAL,short_leverage REAL,weight_precision REAL);
            CREATE TABLE execution_input_target(input_id TEXT,role TEXT,map_key TEXT,value REAL);").unwrap();
        connection
    }

    #[test]
    fn targeted_input_reads_preserve_null_and_reject_missing_members() {
        let connection = fixture();
        connection
            .execute(
                "INSERT INTO execution_input VALUES('i','null',NULL,'value',2,NULL,0.01)",
                [],
            )
            .unwrap();
        assert_eq!(
            input_target(&connection, "i").unwrap(),
            json!({"present":true,"target":null})
        );
        assert_eq!(
            sizing_context(&connection, "i").unwrap(),
            json!({"long_leverage":2.0,"weight_precision":0.01})
        );
        connection
            .execute(
                "UPDATE execution_input SET curr_target_state='value',curr_target_count=1",
                [],
            )
            .unwrap();
        assert!(input_target(&connection, "i").is_err());
        connection
            .execute(
                "INSERT INTO execution_input_target VALUES('i','curr_target','short',-0.5)",
                [],
            )
            .unwrap();
        assert_eq!(
            input_target(&connection, "i").unwrap(),
            json!({"present":true,"target":{"short":-0.5}})
        );
    }

    #[test]
    fn latest_success_without_input_does_not_search_older_input() {
        let connection = fixture();
        connection.execute("INSERT INTO executerecord(id,account_id,is_success,input_id,created_at) VALUES(1,1,1,'missing-input','t')", []).unwrap();
        connection
            .execute(
                "INSERT INTO executerecord(id,account_id,is_success,created_at) VALUES(2,1,1,'t')",
                [],
            )
            .unwrap();
        connection.execute("INSERT INTO executerecord(id,account_id,is_success,input_id,created_at) VALUES(3,1,0,'missing-input','t')", []).unwrap();
        assert_eq!(
            last_target(&connection, &json!({"account_id":1})).unwrap(),
            json!({"present":false,"target":null})
        );
        assert_eq!(
            last_target(&connection, &json!({"account_id":2})).unwrap(),
            json!({"present":false,"target":null})
        );
    }

    #[test]
    fn missing_execution_status_and_invalid_identifier_are_bounded() {
        let connection = fixture();
        assert_eq!(
            read_on(
                &connection,
                &json!({"op":"status","execution_id":"missing"})
            )
            .unwrap(),
            json!({"record":null,"outcome_symbols":[]})
        );
        assert!(read_on(&connection, &json!({"op":"last_target","account_id":true})).is_err());
    }
}
