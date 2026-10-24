//! 终态主列、展示摘要及有序撤单失败证据;实时写入与 0037 迁移共用。

use rusqlite::Connection;
use serde_json::{json, Map, Value};

use super::encode::{audit_value, is_truthy, sql_value, store_columns, FactBatch};
use super::{facts_error, insert_batch, sql};
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
    ("requested_at", "text"),
    ("acked_at", "text"),
    ("finished_at", "text"),
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

fn text(value: Option<&Value>) -> Option<&str> {
    value
        .and_then(Value::as_str)
        .filter(|text| text.chars().any(|character| !python_whitespace(character)))
}

fn python_whitespace(character: char) -> bool {
    character.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&character)
}

fn decimal_digit(character: char) -> bool {
    // 0037 的 Python 3.12 正则 \d 使用 Unicode 15 十进制数字。
    const ZEROES: &[u32] = &[
        0x30, 0x660, 0x6f0, 0x7c0, 0x966, 0x9e6, 0xa66, 0xae6, 0xb66, 0xbe6, 0xc66, 0xce6, 0xd66,
        0xde6, 0xe50, 0xed0, 0xf20, 0x1040, 0x1090, 0x17e0, 0x1810, 0x1946, 0x19d0, 0x1a80, 0x1a90,
        0x1b50, 0x1bb0, 0x1c40, 0x1c50, 0xa620, 0xa8d0, 0xa900, 0xa9d0, 0xa9f0, 0xaa50, 0xabf0,
        0xff10, 0x104a0, 0x10d30, 0x11066, 0x110f0, 0x11136, 0x111d0, 0x112f0, 0x11450, 0x114d0,
        0x11650, 0x116c0, 0x11730, 0x118e0, 0x11950, 0x11c50, 0x11d50, 0x11da0, 0x11f50, 0x16a60,
        0x16ac0, 0x16b50, 0x1e140, 0x1e2f0, 0x1e4f0, 0x1e950, 0x1fbf0,
    ];
    let code = character as u32;
    (0x1d7ce..0x1d800).contains(&code)
        || ZEROES
            .iter()
            .any(|zero| (*zero..*zero + 10).contains(&code))
}

fn technical_text(value: &str) -> bool {
    if ["Traceback", "ErrorID=", "return_code="]
        .iter()
        .any(|marker| value.contains(marker))
        || value == "gm_authentication_error"
    {
        return true;
    }
    // 异常名正则的前缀只含字母、数字、下划线和点,匹配位置可从词边界开始。
    if ["Error", "Exception"].iter().any(|suffix| {
        value.match_indices(suffix).any(|(index, _)| {
            value[index + suffix.len()..]
                .chars()
                .next()
                .is_some_and(|character| character == ':' || python_whitespace(character))
        })
    }) {
        return true;
    }
    let stripped = value.trim_matches(python_whitespace);
    let digits = stripped.strip_prefix('-').unwrap_or(stripped);
    if !digits.is_empty() && digits.chars().all(decimal_digit) {
        return true;
    }
    stripped
        .as_bytes()
        .first()
        .is_some_and(u8::is_ascii_uppercase)
        && stripped.bytes().all(|character| {
            character.is_ascii_uppercase()
                || character.is_ascii_digit()
                || b"_.".contains(&character)
        })
}

fn status_from_outcome(outcome: &str) -> Option<&'static str> {
    match outcome {
        "completed" => Some("SUCCEEDED"),
        "not_reached" => Some("PARTIAL"),
        "error" => Some("FAILED"),
        "blocked" => Some("BLOCKED"),
        _ => None,
    }
}

fn outcome_from_status(status: &str) -> Option<&'static str> {
    match status {
        "SUCCEEDED" | "NOOP" => Some("completed"),
        "PARTIAL" => Some("not_reached"),
        "FAILED" => Some("error"),
        "BLOCKED" => Some("blocked"),
        _ => None,
    }
}

fn error_copy(outcome: Option<&str>) -> &'static str {
    match outcome {
        Some("error") => "执行失败，具体原因见执行证据",
        Some("not_reached") => "执行不到位",
        Some("blocked") => "执行受阻，具体原因见执行证据",
        Some("terminated") => "执行已终止",
        _ => "执行结果待确认",
    }
}

fn unsuccessful(status: Option<&Value>) -> Result<bool, String> {
    match status {
        Some(Value::Array(_)) => Err("unhashable type: 'list'".into()),
        Some(Value::Object(_)) => Err("unhashable type: 'dict'".into()),
        _ => Ok(matches!(
            status.and_then(Value::as_str),
            Some("FAILED" | "PARTIAL" | "BLOCKED" | "TERMINATED")
        )),
    }
}

fn legacy_message(raw: &Map<String, Value>) -> Option<&str> {
    let candidates = [
        text(raw.get("outcome_reason")),
        text(raw.get("msg")),
        text(
            raw.get("memory")
                .and_then(Value::as_object)
                .and_then(|memory| memory.get("message")),
        ),
    ];
    candidates
        .iter()
        .flatten()
        .copied()
        .find(|candidate| !technical_text(candidate))
        .or_else(|| candidates.into_iter().flatten().next())
}

fn normalized_result(raw: &Map<String, Value>) -> Result<Map<String, Value>, String> {
    let mut result = raw.clone();
    if text(result.get("status")).is_none() {
        if let Some(outcome) = raw.get("outcome").and_then(Value::as_str) {
            result.insert(
                "status".into(),
                status_from_outcome(outcome).map_or(Value::Null, |value| json!(value)),
            );
            if outcome == "terminated" && text(result.get("task_status")).is_none() {
                result.insert("task_status".into(), json!("TERMINATED"));
            }
        }
    }
    if text(result.get("outcome")).is_none() {
        let outcome = result
            .get("status")
            .and_then(Value::as_str)
            .and_then(outcome_from_status);
        let outcome = outcome.or_else(|| {
            (result.get("task_status").and_then(Value::as_str) == Some("TERMINATED"))
                .then_some("terminated")
        });
        result.insert(
            "outcome".into(),
            outcome.map_or(Value::Null, |value| json!(value)),
        );
    }
    normalize_error(raw, &mut result)?;
    if let Some(symbols) = raw.get("symbol_results").and_then(Value::as_object) {
        let mut normalized = Map::new();
        for (key, item) in symbols {
            normalized.insert(
                key.clone(),
                match item.as_object() {
                    Some(item) => Value::Object(normalized_result(item)?),
                    None => item.clone(),
                },
            );
        }
        result.insert("symbol_results".into(), Value::Object(normalized));
    }
    fill_reason_code(&mut result)?;
    Ok(result)
}

fn normalize_error(
    raw: &Map<String, Value>,
    result: &mut Map<String, Value>,
) -> Result<(), String> {
    if text(result.get("error")).is_none() {
        let legacy = legacy_message(raw);
        let needs_error = unsuccessful(result.get("status"))?
            || result.get("task_status").and_then(Value::as_str) == Some("TERMINATED")
            || result.get("status").unwrap_or(&Value::Null).is_null() && legacy.is_some();
        if needs_error {
            let error = if let Some(message) = legacy.filter(|message| !technical_text(message)) {
                message
            } else {
                if let Some(message) = legacy {
                    result
                        .entry("technical_detail")
                        .or_insert_with(|| json!(message));
                }
                error_copy(result.get("outcome").and_then(Value::as_str))
            };
            result.insert("error".into(), json!(error));
        }
    } else if text(result.get("error")).is_some_and(technical_text) {
        let error = result["error"].clone();
        result.entry("technical_detail").or_insert(error);
        let reason = text(result.get("outcome_reason")).filter(|reason| !technical_text(reason));
        let error = reason.unwrap_or_else(|| {
            error_copy(
                result
                    .get("status")
                    .and_then(Value::as_str)
                    .and_then(outcome_from_status),
            )
        });
        result.insert("error".into(), json!(error));
    }
    Ok(())
}

fn session_code(code: &str) -> &str {
    match code {
        "CLOSED" | "CTP.SESSION.CLOSED" => COMMON_SESSION_CLOSED,
        _ => code,
    }
}

fn fill_reason_code(result: &mut Map<String, Value>) -> Result<(), String> {
    if let Some(code) = text(result.get("reason_code")) {
        let normalized = session_code(code).to_owned();
        result.insert("reason_code".into(), json!(normalized));
        return Ok(());
    }
    if result.contains_key("reason_code") {
        return Ok(());
    }
    if let Some(code) = text(
        result
            .get("memory")
            .and_then(Value::as_object)
            .and_then(|memory| memory.get("symbol_decision_reason_code")),
    ) {
        let normalized = session_code(code).to_owned();
        result.insert("reason_code".into(), json!(normalized));
        return Ok(());
    }
    if result.get("status").and_then(Value::as_str) == Some("BLOCKED")
        && matches!(
            result.get("error").and_then(Value::as_str),
            Some("非交易时段" | "当前不在交易时段" | "当前不在交易时间")
        )
    {
        result.insert("reason_code".into(), json!(COMMON_SESSION_CLOSED));
        return Ok(());
    }
    if let Some(symbols) = result.get("symbol_results").and_then(Value::as_object) {
        let mut failed = false;
        let mut all_closed = true;
        for item in symbols.values().filter_map(Value::as_object) {
            if unsuccessful(item.get("status"))? {
                all_closed &= text(item.get("reason_code")) == Some(COMMON_SESSION_CLOSED);
                failed = true;
            }
        }
        if failed && all_closed {
            result.insert("reason_code".into(), json!(COMMON_SESSION_CLOSED));
        }
    }
    Ok(())
}

/// 保留旧读时归一的递归、技术原文和休市原因码语义,供冻结历史迁移复用。
pub(crate) fn normalize_legacy_result(raw: &Value) -> Result<Value, AnalysisError> {
    match raw.as_object() {
        Some(raw) => normalized_result(raw)
            .map(Value::Object)
            .map_err(facts_error),
        None => Ok(raw.clone()),
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

/// 原字段、展示字段分开保存,展示归一失败不能阻断历史证据迁移。
pub(crate) fn execution_head_values(
    payload: &Value,
    is_success: Option<i64>,
) -> Result<Map<String, Value>, AnalysisError> {
    let empty = Map::new();
    let payload = payload.as_object().unwrap_or(&empty);
    let children = payload.keys().map(String::as_str).collect::<Vec<_>>();
    let (mut values, evidence) =
        store_columns(payload, HEAD_FIELDS, &children).map_err(facts_error)?;
    let normalized = normalized_result(payload).unwrap_or_else(|_| values.clone());
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

pub(crate) fn encode_head(
    record_id: i64,
    result: &Value,
    is_success: Option<i64>,
) -> Result<FactBatch, AnalysisError> {
    let mut head = execution_head_values(result, is_success)?;
    head.insert("id".into(), json!(record_id));
    let mut batch = FactBatch::new();
    batch.push("executerecord", vec![head]);
    batch.push(
        "execution_termination_cancel_failure",
        cancellation_rows(record_id, result)?,
    );
    Ok(batch)
}

fn cancellation_rows(
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
    let mut batch = FactBatch::new();
    batch.push(
        "execution_termination_cancel_failure",
        cancellation_rows(record_id, result)?,
    );
    insert_batch(connection, &batch)
}

/// 使用调用方事务;输入关联和 facts_complete 由终态事务负责人统一写入。
pub(crate) fn write_result_head(
    connection: &Connection,
    record_id: i64,
    result: &Value,
    is_success: Option<i64>,
) -> Result<(), AnalysisError> {
    let head = execution_head_values(result, is_success)?;
    let columns = head.keys().collect::<Vec<_>>();
    let assignments = columns
        .iter()
        .map(|key| format!("\"{key}\"=?"))
        .collect::<Vec<_>>()
        .join(",");
    let mut values = columns
        .iter()
        .map(|key| sql_value(key, head[*key].clone()))
        .collect::<Result<Vec<_>, _>>()
        .map_err(facts_error)?;
    values.push(rusqlite::types::Value::Integer(record_id));
    let affected = connection
        .execute(
            &format!("UPDATE executerecord SET {assignments} WHERE id=?"),
            rusqlite::params_from_iter(values),
        )
        .map_err(sql)?;
    if affected != 1 {
        return Err(facts_error("执行主记录不存在"));
    }
    write_result_cancellations(connection, record_id, result)
}

#[cfg(test)]
mod tests {
    use super::super::encode::{batch_value, frozen_cases};
    use super::*;

    #[test]
    fn frozen_0037_head_fixtures_preserve_raw_and_display_values() {
        for case in frozen_cases("head") {
            let encoded = encode_head(
                7,
                &case["payload"],
                case.get("is_success").and_then(Value::as_i64),
            );
            if let Some(error) = case.get("error") {
                assert_eq!(
                    encoded.err().unwrap().error,
                    error.as_str().unwrap(),
                    "{}",
                    case["name"]
                );
            } else {
                assert_eq!(
                    batch_value(&encoded.unwrap()),
                    case["expected"],
                    "{}",
                    case["name"]
                );
            }
        }
    }

    #[test]
    fn frozen_legacy_normalization_keeps_technical_and_session_evidence() {
        for case in frozen_cases("normalize") {
            let normalized = normalize_legacy_result(&case["payload"]);
            if let Some(error) = case.get("error") {
                assert_eq!(
                    normalized.err().unwrap().error,
                    error.as_str().unwrap(),
                    "{}",
                    case["name"]
                );
            } else {
                assert_eq!(normalized.unwrap(), case["expected"], "{}", case["name"]);
            }
        }
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
