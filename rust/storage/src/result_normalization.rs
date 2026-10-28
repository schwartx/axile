//! Native and browser readers share the same execution display semantics.

use serde_json::{json, Map, Value};

pub(crate) const COMMON_SESSION_CLOSED: &str = "COMMON.SESSION.CLOSED";

pub(crate) fn text(value: Option<&Value>) -> Option<&str> {
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

pub(crate) fn technical_text(value: &str) -> bool {
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

pub(crate) fn status_from_outcome(outcome: &str) -> Option<&'static str> {
    match outcome {
        "completed" => Some("SUCCEEDED"),
        "not_reached" => Some("PARTIAL"),
        "error" => Some("FAILED"),
        "blocked" => Some("BLOCKED"),
        _ => None,
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

pub(crate) fn normalized_result(raw: &Map<String, Value>) -> Result<Map<String, Value>, String> {
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

pub(crate) fn session_code(code: &str) -> &str {
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
