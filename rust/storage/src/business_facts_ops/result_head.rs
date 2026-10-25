//! 终态主列、展示摘要及有序撤单失败证据;实时写入与 0037 迁移共用。

use rusqlite::Connection;
use serde_json::{json, Map, Value};

use super::encode;
use super::encode::{audit_value, is_truthy, store_columns, Rows};
use super::{facts_error, sql};
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

pub(crate) fn normalize_event_details(details: &mut Value) {
    if text(details.get("error")).is_none() {
        if let Some(error) = text(details.get("debug").and_then(|debug| debug.get("error")))
            .filter(|error| !technical_text(error))
        {
            details["error"] = json!(error);
        }
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
) -> Result<(Map<String, Value>, Rows), AnalysisError> {
    let mut head = execution_head_values(result, is_success)?;
    head.insert("id".into(), json!(record_id));
    Ok((head, cancellation_rows(record_id, result)?))
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
    let rows = cancellation_rows(record_id, result)?;
    let mut statement = connection.prepare_cached("INSERT INTO execution_termination_cancel_failure (\"record_id\",\"ordinal\",\"order_id\",\"audit_evidence\") VALUES (?1,?2,?3,?4)").map_err(sql)?;
    for row in &rows {
        statement
            .execute(rusqlite::params![
                row.get("record_id").and_then(Value::as_i64),
                row.get("ordinal").and_then(Value::as_i64),
                encode::json_text(row.get("order_id"))?,
                encode::json_text(row.get("audit_evidence"))?
            ])
            .map_err(sql)?;
    }
    Ok(())
}

/// 使用调用方事务;输入关联和 facts_complete 由终态事务负责人统一写入。
pub(crate) fn write_result_head(
    connection: &Connection,
    record_id: i64,
    result: &Value,
    is_success: Option<i64>,
) -> Result<(), AnalysisError> {
    let head = execution_head_values(result, is_success)?;
    let affected = connection.execute("UPDATE executerecord SET \"status\"=?1,\"symbol\"=?2,\"technical_detail\"=?3,\"task_status\"=?4,\"error\"=?5,\"outcome\"=?6,\"outcome_reason\"=?7,\"reason_code\"=?8,\"msg\"=?9,\"execution_kind\"=?10,\"channel_type\"=?11,\"execution_time\"=?12,\"success\"=?13,\"display_status\"=?14,\"display_error\"=?15,\"display_outcome\"=?16,\"display_outcome_reason\"=?17,\"termination_reason\"=?18,\"termination_mode\"=?19,\"termination_trigger\"=?20,\"termination_requested_at\"=?21,\"termination_acked_at\"=?22,\"termination_finished_at\"=?23,\"termination_cancel_attempted\"=?24,\"termination_forced\"=?25,\"termination_cancel_unconfirmed\"=?26,\"result_input_kind\"=?27,\"result_input_value\"=?28,\"audit_evidence\"=?29,\"termination_evidence\"=?30,\"termination_kind\"=?31,\"termination_cancel_ids_kind\"=?32,\"termination_cancel_count\"=?33 WHERE id=?34", rusqlite::params![
head.get("status").and_then(Value::as_str),
head.get("symbol").and_then(Value::as_str),
head.get("technical_detail").and_then(Value::as_str),
head.get("task_status").and_then(Value::as_str),
head.get("error").and_then(Value::as_str),
head.get("outcome").and_then(Value::as_str),
head.get("outcome_reason").and_then(Value::as_str),
head.get("reason_code").and_then(Value::as_str),
head.get("msg").and_then(Value::as_str),
head.get("execution_kind").and_then(Value::as_str),
head.get("channel_type").and_then(Value::as_str),
head.get("execution_time").and_then(Value::as_f64),
head.get("success").and_then(Value::as_bool),
head.get("display_status").and_then(Value::as_str),
head.get("display_error").and_then(Value::as_str),
head.get("display_outcome").and_then(Value::as_str),
head.get("display_outcome_reason").and_then(Value::as_str),
head.get("termination_reason").and_then(Value::as_str),
head.get("termination_mode").and_then(Value::as_str),
head.get("termination_trigger").and_then(Value::as_str),
head.get("termination_requested_at").and_then(Value::as_str),
head.get("termination_acked_at").and_then(Value::as_str),
head.get("termination_finished_at").and_then(Value::as_str),
head.get("termination_cancel_attempted").and_then(Value::as_bool),
head.get("termination_forced").and_then(Value::as_bool),
head.get("termination_cancel_unconfirmed").and_then(Value::as_bool),
head.get("result_input_kind").and_then(Value::as_str),
encode::json_text(head.get("result_input_value"))?,
encode::json_text(head.get("audit_evidence"))?,
encode::json_text(head.get("termination_evidence"))?,
head.get("termination_kind").and_then(Value::as_str),
head.get("termination_cancel_ids_kind").and_then(Value::as_str),
head.get("termination_cancel_count").and_then(Value::as_i64),
record_id,
]).map_err(sql)?;
    if affected != 1 {
        return Err(facts_error("执行主记录不存在"));
    }
    write_result_cancellations(connection, record_id, result)
}

pub(crate) fn insert_execution(
    connection: &Connection,
    row: &Map<String, Value>,
) -> Result<(), AnalysisError> {
    let mut statement = connection.prepare_cached(r#"INSERT INTO executerecord (
                "execution_id", "is_success", "account_id", "created_at", "status", "symbol",
                "technical_detail", "task_status", "error", "outcome", "outcome_reason", "reason_code",
                "msg", "execution_kind", "channel_type", "execution_time", "success", "display_status",
                "display_error", "display_outcome", "display_outcome_reason", "termination_reason", "termination_mode", "termination_trigger",
                "termination_requested_at", "termination_acked_at", "termination_finished_at", "termination_cancel_attempted", "termination_forced", "termination_cancel_unconfirmed",
                "result_input_kind", "result_input_value", "audit_evidence", "termination_evidence", "termination_kind", "termination_cancel_ids_kind",
                "termination_cancel_count", "facts_complete"
            ) VALUES (
                ?1, ?2, ?3, ?4, ?5, ?6,
                ?7, ?8, ?9, ?10, ?11, ?12,
                ?13, ?14, ?15, ?16, ?17, ?18,
                ?19, ?20, ?21, ?22, ?23, ?24,
                ?25, ?26, ?27, ?28, ?29, ?30,
                ?31, ?32, ?33, ?34, ?35, ?36,
                ?37, ?38
            )"#).map_err(sql)?;
    statement
        .execute(rusqlite::params![
            row.get("execution_id").and_then(Value::as_str),
            row.get("is_success").and_then(Value::as_i64),
            row.get("account_id").and_then(Value::as_i64),
            row.get("created_at").and_then(Value::as_str),
            row.get("status").and_then(Value::as_str),
            row.get("symbol").and_then(Value::as_str),
            row.get("technical_detail").and_then(Value::as_str),
            row.get("task_status").and_then(Value::as_str),
            row.get("error").and_then(Value::as_str),
            row.get("outcome").and_then(Value::as_str),
            row.get("outcome_reason").and_then(Value::as_str),
            row.get("reason_code").and_then(Value::as_str),
            row.get("msg").and_then(Value::as_str),
            row.get("execution_kind").and_then(Value::as_str),
            row.get("channel_type").and_then(Value::as_str),
            row.get("execution_time").and_then(Value::as_f64),
            row.get("success").and_then(Value::as_bool),
            row.get("display_status").and_then(Value::as_str),
            row.get("display_error").and_then(Value::as_str),
            row.get("display_outcome").and_then(Value::as_str),
            row.get("display_outcome_reason").and_then(Value::as_str),
            row.get("termination_reason").and_then(Value::as_str),
            row.get("termination_mode").and_then(Value::as_str),
            row.get("termination_trigger").and_then(Value::as_str),
            row.get("termination_requested_at").and_then(Value::as_str),
            row.get("termination_acked_at").and_then(Value::as_str),
            row.get("termination_finished_at").and_then(Value::as_str),
            row.get("termination_cancel_attempted")
                .and_then(Value::as_bool),
            row.get("termination_forced").and_then(Value::as_bool),
            row.get("termination_cancel_unconfirmed")
                .and_then(Value::as_bool),
            row.get("result_input_kind").and_then(Value::as_str),
            encode::json_text(row.get("result_input_value"))?,
            encode::json_text(row.get("audit_evidence"))?,
            encode::json_text(row.get("termination_evidence"))?,
            row.get("termination_kind").and_then(Value::as_str),
            row.get("termination_cancel_ids_kind")
                .and_then(Value::as_str),
            row.get("termination_cancel_count").and_then(Value::as_i64),
            row.get("facts_complete").and_then(Value::as_bool),
        ])
        .map_err(sql)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::super::encode::frozen_cases;
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
                    {
                        let (head, cancellations) = encoded.unwrap();
                        json!({"executerecord":[head],"execution_termination_cancel_failure":cancellations})
                    },
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
