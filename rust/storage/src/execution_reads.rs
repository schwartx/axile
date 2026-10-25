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

pub(crate) fn record_status(
    connection: &Connection,
    record: &Map<String, Value>,
) -> Result<Value, AnalysisError> {
    let status = match record["task_status"].as_str() {
        Some(
            value @ ("QUEUED" | "RUNNING" | "TERMINATING" | "TERMINATED" | "SUCCEEDED" | "FAILED"
            | "SKIPPED" | "PARTIAL"),
        ) => value,
        Some(_) => return Err(invalid("unknown execution task status")),
        None => {
            if record["is_success"] == 1 {
                "SUCCEEDED"
            } else {
                "FAILED"
            }
        }
    };
    let symbols = result_symbols::read_outcome_symbols(
        connection,
        record["id"]
            .as_i64()
            .ok_or_else(|| invalid("执行记录身份无效"))?,
    )?;
    let optional_text = |name: &str| record[name].as_str().filter(|s| !s.trim().is_empty());
    let payload = json!({
        "execution_id":record["execution_id"], "account_id":record["account_id"],
        "execution_kind":record["execution_kind"].as_str().filter(|value| matches!(*value,"rebalance"|"clear_positions")),
        "status":status, "created_at":record["created_at"], "started_at":null,
        "finished_at":record["termination_finished_at"],
        "error":optional_text("display_error"),
        "output_status":optional_text("display_status"),
        "outcome":record["display_outcome"], "outcome_reason":record["display_outcome_reason"],
        "outcome_symbols":symbols, "record_id":record["id"], "is_success":record["is_success"],
        "cancel_requested_at":record["termination_requested_at"], "cancel_reason":record["termination_reason"],
        "terminate_mode":record["termination_mode"].as_str().filter(|value| matches!(*value,"graceful"|"cancel_pending")),
    });
    Ok(payload)
}

fn persisted_status(connection: &Connection, request: &Value) -> Result<Value, AnalysisError> {
    let id = text(request, "execution_id")?;
    let intent = crate::intents::get_in_connection(connection, id)?;
    let active = intent.as_ref().is_some_and(|value| {
        matches!(
            value["status"].as_str(),
            Some("QUEUED" | "RUNNING" | "TERMINATING")
        )
    });
    let record = if active {
        None
    } else {
        record_on(connection, id, None)?
    };
    let Some(record) = record else {
        return Ok(intent.map(|value| json!({
            "execution_id":value["execution_id"], "account_id":value["account_id"],
            "execution_kind":value["kind"], "status":value["status"],
            "created_at":value["created_at"], "started_at":value["started_at"],
            "finished_at":value["finished_at"], "error":value["error"],
            "output_status":null, "record_id":null, "is_success":null,
            "cancel_requested_at":value["cancel_requested_at"], "cancel_reason":value["cancel_reason"],
            "terminate_mode":value["terminate_mode"],
        })).unwrap_or(Value::Null));
    };
    let mut payload = record_status(connection, &record)?;
    payload["finished_at"] = record["termination_finished_at"]
        .as_str()
        .filter(|s| !s.is_empty())
        .map(Value::from)
        .unwrap_or_else(|| record["created_at"].clone());
    if payload["status"] == "TERMINATED" {
        payload["error"] = Value::Null;
    }
    if payload["status"] == "PARTIAL" {
        if let Some(intent) = intent.filter(|value| {
            value["status"] == "FAILED" && value["error"].as_str().is_some_and(|s| !s.is_empty())
        }) {
            payload["status"] = json!("FAILED");
            payload["error"] = intent["error"].clone();
            payload["output_status"] = json!("FAILED");
        }
    }
    Ok(payload)
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
        connection.execute_batch("CREATE TABLE execution_intent(execution_id TEXT,account_id INTEGER,kind TEXT,trigger_source TEXT,status TEXT,channel TEXT,algorithm TEXT,payload TEXT,created_at TEXT,started_at TEXT,finished_at TEXT,error TEXT,cancel_requested_at TEXT,cancel_reason TEXT,terminate_mode TEXT);").unwrap();
        connection
    }

    #[test]
    fn status_preserves_active_intent_and_later_partial_failure_priority() {
        let connection = fixture();
        for name in [
            "task_status",
            "execution_id",
            "execution_kind",
            "display_status",
            "display_error",
            "display_outcome",
            "display_outcome_reason",
            "termination_finished_at",
            "termination_requested_at",
            "termination_reason",
            "termination_mode",
        ] {
            if !["execution_id"].contains(&name) {
                connection
                    .execute_batch(&format!(
                        "ALTER TABLE executerecord ADD COLUMN {name} TEXT;"
                    ))
                    .unwrap();
            }
        }
        connection.execute_batch("INSERT INTO executerecord(id,account_id,is_success,created_at,task_status,execution_id,display_status,display_error,termination_mode) VALUES(7,1,0,'t','PARTIAL','status','PARTIAL','partial evidence','graceful');").unwrap();
        connection.execute_batch("CREATE TABLE execution_symbol_result(record_id INTEGER,path TEXT,map_key TEXT,node_kind TEXT,status TEXT,outcome TEXT,ordinal INTEGER);").unwrap();
        let request = json!({"execution_id":"status"});
        assert_eq!(
            persisted_status(&connection, &request).unwrap()["status"],
            "PARTIAL"
        );
        connection.execute_batch("INSERT INTO execution_intent(execution_id,account_id,kind,trigger_source,status,channel,algorithm,payload,created_at) VALUES('status',1,'rebalance','manual','QUEUED','ctp','SINGLE-MAKER','{}','t');").unwrap();
        let queued = persisted_status(&connection, &request).unwrap();
        assert_eq!(queued["status"], "QUEUED");
        assert!(queued["record_id"].is_null());
        connection
            .execute_batch("UPDATE execution_intent SET status='FAILED',error='later failure';")
            .unwrap();
        let failed = persisted_status(&connection, &request).unwrap();
        assert_eq!(failed["status"], "FAILED");
        assert_eq!(failed["error"], "later failure");
        assert_eq!(failed["output_status"], "FAILED");
        assert_eq!(failed["record_id"], 7);
        assert_eq!(failed["terminate_mode"], "graceful");
        connection
            .execute_batch(
                "UPDATE executerecord SET task_status='TERMINATED',termination_finished_at=NULL;",
            )
            .unwrap();
        let row = record_on(&connection, "status", None).unwrap().unwrap();
        let receipt = record_status(&connection, &row).unwrap();
        assert!(receipt["finished_at"].is_null());
        assert_eq!(receipt["error"], "partial evidence");
        let persisted = persisted_status(&connection, &request).unwrap();
        assert_eq!(persisted["finished_at"], "t");
        assert!(persisted["error"].is_null());
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
            Value::Null
        );
        assert!(read_on(&connection, &json!({"op":"last_target","account_id":true})).is_err());
    }
}
