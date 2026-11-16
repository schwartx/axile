//! 终态主列、展示摘要及有序撤单失败证据;实时写入与迁移共用。

use rusqlite::Connection;
use serde_json::{json, Map, Value};

use super::encode::{audit_value, is_truthy, store_columns};
use super::facts_error;
use crate::error::AnalysisError;

const HEAD_FIELDS: &[(&str, &str)] = &[
    ("symbol", "text"),
    ("technical_detail", "text"),
    ("status", "text"),
    ("task_status", "text"),
    ("error", "text"),
    ("outcome", "text"),
    ("outcome_reason", "text"),
    ("reason_code", "text"),
    ("msg", "text"),
    ("execution_kind", "text"),
    ("channel_type", "text"),
    ("execution_time", "number"),
    ("success", "boolean"),
];
const TERMINATION_FIELDS: &[(&str, &str)] = &[
    ("reason", "text"),
    ("mode", "text"),
    ("trigger", "text"),
    ("requested_at", "time"),
    ("acked_at", "time"),
    ("finished_at", "time"),
    ("cancel_attempted", "boolean"),
    ("forced", "boolean"),
    ("cancel_unconfirmed", "boolean"),
];
const COMMON_SESSION_CLOSED: &str = "COMMON.SESSION.CLOSED";

fn container_kind(payload: &Map<String, Value>, key: &str, array: bool) -> &'static str {
    match payload.get(key) {
        None => "absent",
        Some(Value::Null) => "null",
        Some(value)
            if if array {
                value.is_array()
            } else {
                value.is_object()
            } =>
        {
            "value"
        }
        _ => "invalid",
    }
}

pub(crate) fn outcome_from_status(status: &str) -> Option<&'static str> {
    match status {
        "SUCCEEDED" | "NOOP" => Some("completed"),
        "PARTIAL" => Some("not_reached"),
        "FAILED" => Some("error"),
        "BLOCKED" => Some("blocked"),
        _ => None,
    }
}

fn has_orders(result: &Map<String, Value>) -> bool {
    ["orders", "trades"]
        .iter()
        .any(|key| result.get(*key).is_some_and(is_truthy))
}

fn session_closed_scope(result: &Map<String, Value>) -> Option<&'static str> {
    if result.get("reason_code").and_then(Value::as_str) != Some(COMMON_SESSION_CLOSED)
        || result.get("task_status").and_then(Value::as_str) == Some("TERMINATED")
    {
        return None;
    }
    let status = result.get("status").and_then(Value::as_str)?;
    if !["BLOCKED", "PARTIAL"].contains(&status) || status == "BLOCKED" && has_orders(result) {
        return None;
    }
    let empty = Map::new();
    let symbols = match result.get("symbol_results") {
        None => &empty,
        Some(symbols) => symbols.as_object()?,
    };
    if symbols.is_empty() {
        return (status == "BLOCKED").then_some("all");
    }
    let mut open = false;
    let mut closed = false;
    for item in symbols.values() {
        let item = item.as_object()?;
        match item.get("status").and_then(Value::as_str)? {
            "SUCCEEDED" | "NOOP" => open = true,
            "BLOCKED"
                if item.get("reason_code").and_then(Value::as_str)
                    == Some(COMMON_SESSION_CLOSED)
                    && !has_orders(item) =>
            {
                closed = true
            }
            _ => return None,
        }
    }
    if !closed {
        None
    } else if open {
        (status == "PARTIAL").then_some("partial")
    } else {
        (status == "BLOCKED").then_some("all")
    }
}

fn terminal_task_status(payload: &Map<String, Value>, is_success: i64) -> &'static str {
    if payload.get("outcome").and_then(Value::as_str) == Some("terminated") {
        "TERMINATED"
    } else if is_success == 1 {
        "SUCCEEDED"
    } else {
        match session_closed_scope(payload) {
            Some("all") => "SKIPPED",
            Some("partial") => "PARTIAL",
            _ => "FAILED",
        }
    }
}

/// 保存原字段及其展示投影。
pub(crate) fn execution_head_values(
    payload: &Value,
    is_success: Option<i64>,
) -> Result<Map<String, Value>, AnalysisError> {
    let empty = Map::new();
    let payload = payload.as_object().unwrap_or(&empty);
    let children = payload.keys().map(String::as_str).collect::<Vec<_>>();
    let (mut values, evidence) =
        store_columns(payload, HEAD_FIELDS, &children).map_err(facts_error)?;
    let normalized = payload;
    for name in ["status", "error", "outcome", "outcome_reason"] {
        values.insert(
            format!("display_{name}"),
            normalized
                .get(name)
                .filter(|value| value.is_string())
                .cloned()
                .unwrap_or(Value::Null),
        );
    }
    let input_kind = container_kind(payload, "inputs", false);
    values.insert("result_input_kind".into(), json!(input_kind));
    values.insert(
        "result_input_value".into(),
        if input_kind == "invalid" {
            audit_value(&payload["inputs"])
        } else {
            Value::Null
        },
    );
    values.insert("audit_evidence".into(), Value::Object(evidence));
    if !payload.contains_key("task_status") {
        if let Some(is_success) = is_success {
            values.insert(
                "task_status".into(),
                json!(terminal_task_status(payload, is_success)),
            );
        }
    }
    let kind = container_kind(payload, "termination", false);
    values.insert("termination_kind".into(), json!(kind));
    values.insert("termination_cancel_ids_kind".into(), json!("absent"));
    values.insert("termination_cancel_count".into(), Value::Null);
    for (name, _) in TERMINATION_FIELDS {
        values.insert(format!("termination_{name}"), Value::Null);
    }
    let mut termination_evidence = Map::new();
    if let Some(termination) = payload.get("termination").and_then(Value::as_object) {
        let (fields, metadata) = store_columns(
            termination,
            TERMINATION_FIELDS,
            &["cancel_failed_order_ids"],
        )
        .map_err(facts_error)?;
        termination_evidence = metadata;
        for (name, value) in fields {
            values.insert(format!("termination_{name}"), value);
        }
        let ids_kind = container_kind(termination, "cancel_failed_order_ids", true);
        values.insert("termination_cancel_ids_kind".into(), json!(ids_kind));
        if let Some(ids) = termination
            .get("cancel_failed_order_ids")
            .and_then(Value::as_array)
        {
            values.insert("termination_cancel_count".into(), json!(ids.len()));
        }
        if ids_kind == "invalid" {
            termination_evidence.insert(
                "cancel_ids_value".into(),
                termination["cancel_failed_order_ids"].clone(),
            );
        }
    } else if kind == "invalid" {
        termination_evidence.insert("value".into(), payload["termination"].clone());
    }
    values.insert(
        "termination_evidence".into(),
        Value::Object(termination_evidence),
    );
    Ok(values)
}

pub(crate) fn cancellation_rows(
    record_id: i64,
    result: &Value,
) -> Result<Vec<Map<String, Value>>, AnalysisError> {
    let mut failures = Vec::new();
    if let Some(ids) = result
        .get("termination")
        .and_then(|termination| termination.get("cancel_failed_order_ids"))
        .and_then(Value::as_array)
    {
        for (ordinal, order_id) in ids.iter().enumerate() {
            let payload = Map::from_iter([("order_id".into(), order_id.clone())]);
            let (mut row, evidence) =
                store_columns(&payload, &[("order_id", "identity")], &[]).map_err(facts_error)?;
            row.insert("record_id".into(), json!(record_id));
            row.insert("ordinal".into(), json!(ordinal));
            row.insert("audit_evidence".into(), Value::Object(evidence));
            failures.push(row);
        }
    }
    Ok(failures)
}

/// 初次插入已带主表字段的终态只写子行,避免重复推进 source_version。
pub(crate) fn write_result_cancellations(
    connection: &Connection,
    record_id: i64,
    result: &Value,
) -> Result<(), AnalysisError> {
    let rows = cancellation_rows(record_id, result)?;
    super::insert_rows(
        connection,
        "execution_termination_cancel_failure",
        &rows,
        &[
            ("record_id", "integer"),
            ("ordinal", "integer"),
            ("order_id", "json"),
            ("audit_evidence", "json"),
        ],
    )
}

pub(crate) const EXECUTION_HEAD_COLUMNS: &[(&str, &str)] = &[
    ("status", "text"),
    ("symbol", "text"),
    ("technical_detail", "text"),
    ("task_status", "text"),
    ("error", "text"),
    ("outcome", "text"),
    ("outcome_reason", "text"),
    ("reason_code", "text"),
    ("msg", "text"),
    ("execution_kind", "text"),
    ("channel_type", "text"),
    ("execution_time", "number"),
    ("success", "boolean"),
    ("display_status", "text"),
    ("display_error", "text"),
    ("display_outcome", "text"),
    ("display_outcome_reason", "text"),
    ("termination_reason", "text"),
    ("termination_mode", "text"),
    ("termination_trigger", "text"),
    ("termination_requested_at", "text"),
    ("termination_acked_at", "text"),
    ("termination_finished_at", "text"),
    ("termination_cancel_attempted", "boolean"),
    ("termination_forced", "boolean"),
    ("termination_cancel_unconfirmed", "boolean"),
    ("result_input_kind", "text"),
    ("result_input_value", "json"),
    ("audit_evidence", "json"),
    ("termination_evidence", "json"),
    ("termination_kind", "text"),
    ("termination_cancel_ids_kind", "text"),
    ("termination_cancel_count", "integer"),
];

pub(crate) fn insert_execution(
    connection: &Connection,
    row: &Map<String, Value>,
) -> Result<(), AnalysisError> {
    let columns = [
        &[
            ("execution_id", "text"),
            ("is_success", "integer"),
            ("account_id", "integer"),
            ("created_at", "text"),
        ][..],
        EXECUTION_HEAD_COLUMNS,
        &[("facts_complete", "boolean")],
    ]
    .concat();
    super::insert_rows(
        connection,
        "executerecord",
        std::slice::from_ref(row),
        &columns,
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::migrations::legacy_result::write_result_head;

    #[test]
    fn insert_and_update_advance_once_and_missing_heads_leave_no_cancellations() {
        let connection = Connection::open_in_memory().unwrap();
        let columns = execution_head_values(&json!({}), None)
            .unwrap()
            .keys()
            .map(|key| format!("\"{key}\""))
            .collect::<Vec<_>>()
            .join(",");
        connection.execute_batch(&format!("CREATE TABLE executerecord(id INTEGER PRIMARY KEY,execution_id TEXT,is_success INTEGER,account_id INTEGER,created_at TEXT,facts_complete INTEGER,{columns});
            CREATE TABLE execution_termination_cancel_failure(record_id INTEGER,ordinal INTEGER,order_id JSON,audit_evidence JSON);
            CREATE TABLE account_analysis(source_version INTEGER); INSERT INTO account_analysis VALUES(0);
            CREATE TRIGGER record_insert AFTER INSERT ON executerecord BEGIN UPDATE account_analysis SET source_version=source_version+1; END;
            CREATE TRIGGER record_update AFTER UPDATE ON executerecord BEGIN UPDATE account_analysis SET source_version=source_version+1; END; BEGIN;")).unwrap();
        let mut row = execution_head_values(&json!({"status":"FAILED"}), Some(0)).unwrap();
        row.extend(json!({"execution_id":"e1","is_success":0,"account_id":1,"created_at":"t","facts_complete":true}).as_object().unwrap().clone());
        insert_execution(&connection, &row).unwrap();
        let id = connection.last_insert_rowid();
        write_result_cancellations(&connection, id, &json!({"status":"FAILED"})).unwrap();
        let version = || {
            connection
                .query_row("SELECT source_version FROM account_analysis", [], |row| {
                    row.get::<_, i64>(0)
                })
                .unwrap()
        };
        assert_eq!(version(), 1);
        let result = json!({"status":"FAILED","termination":{"cancel_failed_order_ids":["same","same",null]}});
        write_result_head(&connection, id, &result, Some(0)).unwrap();
        assert_eq!(version(), 2);
        assert_eq!(
            connection
                .query_row(
                    "SELECT facts_complete FROM executerecord WHERE id=?1",
                    [id],
                    |row| row.get::<_, i64>(0)
                )
                .unwrap(),
            1
        );
        let error = write_result_head(&connection, id + 1, &result, Some(0)).unwrap_err();
        assert_eq!(
            (error.code.as_str(), error.error.as_str()),
            ("BUSINESS_FACTS", "执行主记录不存在")
        );
        assert_eq!(version(), 2);
        assert_eq!(
            connection
                .query_row(
                    "SELECT count(*) FROM execution_termination_cancel_failure",
                    [],
                    |row| row.get::<_, i64>(0)
                )
                .unwrap(),
            3
        );
        connection.execute_batch("ROLLBACK").unwrap();
        assert_eq!(version(), 0);
        assert_eq!(
            connection
                .query_row("SELECT count(*) FROM executerecord", [], |row| row
                    .get::<_, i64>(0))
                .unwrap(),
            0
        );
    }

    #[test]
    fn head_writer_keeps_links_and_completion_in_the_callers_transaction() {
        let connection = Connection::open_in_memory().unwrap();
        let columns = execution_head_values(&json!({}), None)
            .unwrap()
            .keys()
            .map(|key| format!("\"{key}\""))
            .collect::<Vec<_>>()
            .join(",");
        connection.execute_batch(&format!("CREATE TABLE executerecord(id INTEGER PRIMARY KEY,input_id TEXT,result_input_id TEXT,facts_complete INTEGER,{columns});CREATE TABLE execution_termination_cancel_failure(record_id INTEGER,ordinal INTEGER,order_id JSON,audit_evidence JSON);INSERT INTO executerecord(id,input_id,result_input_id,facts_complete) VALUES(7,'primary','result',0);BEGIN;")).unwrap();
        write_result_head(&connection, 7, &json!({"status":"FAILED","termination":{"cancel_failed_order_ids":["same","same",null]}}), Some(0)).unwrap();
        assert!(!connection.is_autocommit());
        let saved: (String, String, i64) = connection
            .query_row(
                "SELECT input_id,result_input_id,facts_complete FROM executerecord WHERE id=7",
                [],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .unwrap();
        assert_eq!(saved, ("primary".into(), "result".into(), 0));
        assert_eq!(
            connection
                .query_row(
                    "SELECT count(*) FROM execution_termination_cancel_failure",
                    [],
                    |row| row.get::<_, i64>(0)
                )
                .unwrap(),
            3
        );
        connection.execute_batch("ROLLBACK").unwrap();
        assert_eq!(
            connection
                .query_row(
                    "SELECT count(*) FROM execution_termination_cancel_failure",
                    [],
                    |row| row.get::<_, i64>(0)
                )
                .unwrap(),
            0
        );
    }

    #[test]
    fn cancellation_writer_does_not_access_or_update_the_head() {
        let connection = Connection::open_in_memory().unwrap();
        connection.execute_batch("CREATE TABLE execution_termination_cancel_failure(record_id INTEGER,ordinal INTEGER,order_id JSON,audit_evidence JSON);BEGIN;").unwrap();
        write_result_cancellations(
            &connection,
            7,
            &json!({"termination":{"cancel_failed_order_ids":["same","same",null]}}),
        )
        .unwrap();
        assert!(!connection.is_autocommit());
        assert_eq!(
            connection
                .query_row(
                    "SELECT count(*) FROM execution_termination_cancel_failure",
                    [],
                    |row| row.get::<_, i64>(0)
                )
                .unwrap(),
            3
        );
        connection.execute_batch("ROLLBACK").unwrap();
        assert_eq!(
            connection
                .query_row(
                    "SELECT count(*) FROM execution_termination_cancel_failure",
                    [],
                    |row| row.get::<_, i64>(0)
                )
                .unwrap(),
            0
        );
    }
}
