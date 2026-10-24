//! 0037—0040 冻结通知引用的解码，仅在迁移事务内使用。

use crate::business_detail::{load, wire};
use rusqlite::Connection;
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
type Object = Map<String, Value>;
pub(crate) fn digest(value: &Value) -> Result<String, String> {
    Ok(format!("{:x}", Sha256::digest(wire(value)?.as_bytes())))
}
pub(crate) const EXEC_METADATA: &[&str] = &[
    "id",
    "kind",
    "trigger_source",
    "notified_at",
    "execution_time",
    "outcome_reason",
    "channel_type",
    "status",
    "success",
    "outcome",
    "reason_code",
    "error",
    "is_test",
    "supplements",
];
fn scalar(value: &Value) -> bool {
    !value.is_object() && !value.is_array()
}
fn reorder(values: Object, order: &Value) -> Result<Object, String> {
    let order = order
        .as_array()
        .ok_or_else(|| invalid("invalid notification field order"))?;
    if order.len() != values.len() {
        return Err(invalid("incomplete notification field order"));
    }
    let mut result = Object::new();
    for key in order {
        let key = key
            .as_str()
            .ok_or_else(|| invalid("invalid notification order key"))?;
        let value = values
            .get(key)
            .ok_or_else(|| invalid("unknown notification order key"))?;
        if result.insert(key.into(), value.clone()).is_some() {
            return Err(invalid("duplicate notification order key"));
        }
    }
    Ok(result)
}
pub(super) fn hydrate(
    conn: &Connection,
    record: &Object,
    result: &mut Object,
) -> Result<(), String> {
    let Some(extra) = result.get_mut("extra").and_then(Value::as_object_mut) else {
        return Ok(());
    };
    let Some(reference) = extra
        .get("server_notification_context")
        .and_then(Value::as_object)
    else {
        return Ok(());
    };
    if !reference.contains_key("$axile_notification_context") {
        return Ok(());
    }
    validate_notification_reference(reference)?;
    let event_id = format!("execution:{}", text(record, "execution_id")?);
    let events = load(conn, "notification_event", "id", &json!(event_id))?;
    let event = one(&events)?;
    if get(event, "account_id")? != get(record, "account_id")?
        || text(event, "event_type")? != "execution.finished"
    {
        return Err(invalid("notification identity or digest mismatch"));
    }
    let restored = hydrate_notification_context(reference, &event_id, get(event, "context")?)?;
    extra.insert("server_notification_context".into(), restored);
    Ok(())
}

fn validate_notification_reference(reference: &Object) -> Result<(), String> {
    let expected = [
        "$axile_notification_context",
        "event_id",
        "event_sha256",
        "capture_sha256",
        "root_order",
        "root_metadata",
        "execution_state",
        "execution_order",
        "execution_metadata",
    ];
    if reference.len() != expected.len()
        || expected.iter().any(|k| !reference.contains_key(*k))
        || get(reference, "$axile_notification_context")?.as_i64() != Some(1)
    {
        return Err(invalid("invalid notification reference"));
    }
    Ok(())
}

/// 复用冻结引用的唯一线格式、哈希和恢复规则。
pub(crate) fn hydrate_notification_context(
    reference: &Map<String, Value>,
    event_id: &str,
    event_context: &Value,
) -> Result<Value, String> {
    validate_notification_reference(reference)?;
    if text(reference, "event_id")? != event_id
        || text(reference, "event_sha256")? != digest(event_context)?
    {
        return Err(invalid("notification identity or digest mismatch"));
    }
    let root_metadata = object(get(reference, "root_metadata")?)?;
    let execution_metadata = object(get(reference, "execution_metadata")?)?;
    if root_metadata
        .iter()
        .any(|(k, v)| !["event_id", "event_type"].contains(&k.as_str()) || !scalar(v))
    {
        return Err(invalid("notification root metadata override"));
    }
    for (key, value) in execution_metadata {
        if !EXEC_METADATA.contains(&key.as_str()) {
            return Err(invalid("notification business override"));
        }
        if key == "supplements" {
            let supplements = value
                .as_array()
                .ok_or_else(|| invalid("invalid notification supplements"))?;
            for item in supplements {
                if object(item)?.iter().any(|(k, v)| {
                    ![
                        "group_id",
                        "base_scheduled_at",
                        "scheduled_at",
                        "index",
                        "configured_count",
                        "effective_count",
                        "is_last",
                        "group_status",
                        "end_reason",
                    ]
                    .contains(&k.as_str())
                        || !scalar(v)
                }) {
                    return Err(invalid("notification supplement override"));
                }
            }
        } else if !scalar(value) {
            return Err(invalid("notification execution metadata override"));
        }
    }
    let mut context = object(event_context)?.clone();
    for key in ["event_id", "event_type"] {
        context.shift_remove(key);
    }
    context.extend(root_metadata.clone());
    match text(reference, "execution_state")? {
        "object" => {
            let mut execution = object(
                context
                    .get("execution")
                    .ok_or_else(|| invalid("missing notification execution"))?,
            )?
            .clone();
            for key in EXEC_METADATA {
                execution.shift_remove(*key);
            }
            execution.extend(execution_metadata.clone());
            context.insert(
                "execution".into(),
                Value::Object(reorder(execution, get(reference, "execution_order")?)?),
            );
        }
        "null" => {
            context.insert("execution".into(), Value::Null);
        }
        "absent" => {
            context.shift_remove("execution");
        }
        _ => return Err(invalid("invalid notification execution state")),
    }
    let restored = Value::Object(reorder(context, get(reference, "root_order")?)?);
    if text(reference, "capture_sha256")? != digest(&restored)? {
        return Err(invalid("notification capture digest mismatch"));
    }
    Ok(restored)
}

fn invalid(message: impl std::fmt::Display) -> String {
    format!("INVALID_DATA: {message}")
}
fn get<'a>(row: &'a Object, key: &str) -> Result<&'a Value, String> {
    row.get(key)
        .ok_or_else(|| invalid(format!("missing {key}")))
}
fn text<'a>(row: &'a Object, key: &str) -> Result<&'a str, String> {
    get(row, key)?
        .as_str()
        .ok_or_else(|| invalid(format!("invalid {key}")))
}
fn object(value: &Value) -> Result<&Object, String> {
    value.as_object().ok_or_else(|| invalid("expected object"))
}
fn one(rows: &[Object]) -> Result<&Object, String> {
    if rows.len() != 1 {
        return Err(invalid("expected exactly one row"));
    }
    Ok(&rows[0])
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn huge_integer_float_and_text_keep_distinct_python_wire_hashes() {
        for (source, expected, sha256) in [
            (
                "1267650600228229401496703205377",
                "1267650600228229401496703205377",
                "06fcc0ce6458663091c174b303abd34f88b8989fa5f6a4d366dac2b7ca75eb43",
            ),
            (
                "1267650600228229401496703205377.0",
                "1.2676506002282294e+30",
                "fc89c65a72ca5e5a9e13e75750cc2c8e7bb353386303341542fd173e28bd0cdd",
            ),
            (
                "\"1267650600228229401496703205377\"",
                "\"1267650600228229401496703205377\"",
                "eff34fb5aa8f6060f856d19ae0dbee4bbd18095eca5eb6ef35fd1cec5b2959b7",
            ),
        ] {
            let value: Value = serde_json::from_str(source).unwrap();
            assert_eq!(wire(&value).unwrap(), expected);
            assert_eq!(digest(&value).unwrap(), sha256);
        }
        let value: Value = serde_json::from_str(
            r#"{"integer":1267650600228229401496703205377,"float":1267650600228229401496703205377.0,"text":"1267650600228229401496703205377","rounded":1.234567890123456789,"negative_zero":-0}"#,
        ).unwrap();
        assert_eq!(
            wire(&value).unwrap(),
            r#"{"integer":1267650600228229401496703205377,"float":1.2676506002282294e+30,"text":"1267650600228229401496703205377","rounded":1.2345678901234567,"negative_zero":0}"#
        );
        assert_eq!(
            digest(&value).unwrap(),
            "87cb20f921b397792b060267474a4619298ae8888bd468256d46d3a1a287e781"
        );
    }

    #[test]
    fn duplicate_order_is_rejected() {
        assert!(reorder(
            object(&json!({"a":1,"b":2})).unwrap().clone(),
            &json!(["a", "a"])
        )
        .is_err());
    }
}
