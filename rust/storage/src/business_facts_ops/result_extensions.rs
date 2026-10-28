//! 根扩展只保存未归属字段与原始证据，异常历史根值具有显式状态。

use rusqlite::Connection;
use serde_json::{json, Map, Value};

use super::encode::{audit_value, store_columns};
use crate::error::AnalysisError;

const FIELDS: &[(&str, &str)] = &[("memory", "json"), ("extra", "json")];
const DOMAINS: &[&str] = &[
    "reconciliation",
    "status",
    "symbol",
    "technical_detail",
    "task_status",
    "error",
    "outcome",
    "outcome_reason",
    "reason_code",
    "msg",
    "execution_kind",
    "channel_type",
    "execution_time",
    "success",
    "termination",
    "inputs",
    "account_assets",
    "symbol_results",
    "orders",
    "trades",
    "first_ticks",
    "target_volume",
];

pub(crate) fn encode_extensions(
    record_id: i64,
    payload: &Map<String, Value>,
) -> Result<Map<String, Value>, String> {
    let (mut row, evidence) = store_columns(payload, FIELDS, DOMAINS)?;
    row.insert("record_id".into(), json!(record_id));
    row.insert("root_kind".into(), json!("object"));
    row.insert("root_value".into(), Value::Null);
    row.insert("audit_evidence".into(), Value::Object(evidence));
    Ok(row)
}

pub(crate) fn encode_result_root_state(
    record_id: i64,
    payload: &Value,
) -> Result<Map<String, Value>, String> {
    let kind =
        match payload {
            Value::Object(map) => return encode_extensions(record_id, map),
            Value::Null => "null",
            Value::String(_) | Value::Bool(_) | Value::Number(_) => "scalar",
            Value::Array(values) if values.is_empty() => "empty_list",
            _ => return Err(
                "Unsupported historical result root; expected object, null, scalar or empty list"
                    .into(),
            ),
        };
    encode_result_root_value(record_id, kind, payload)
}

/// 历史解析器显式保留原根类型，非有限标量的证据对象不能被误判成业务对象。
pub(crate) fn encode_result_root_value(
    record_id: i64,
    kind: &str,
    payload: &Value,
) -> Result<Map<String, Value>, String> {
    let valid = match kind {
        "object" => {
            return payload
                .as_object()
                .ok_or_else(|| "Execution result root value disagrees with its state".into())
                .and_then(|value| encode_extensions(record_id, value));
        }
        "null" => payload.is_null(),
        "empty_list" => payload.as_array().is_some_and(Vec::is_empty),
        "scalar" => {
            payload.is_string()
                || payload.is_boolean()
                || payload.is_number()
                || payload
                    .as_object()
                    .is_some_and(|value| value.len() == 1 && value.contains_key("invalid_number"))
        }
        _ => false,
    };
    if !valid {
        return Err("Execution result root value disagrees with its state".into());
    }
    let row = Map::from_iter([
        ("record_id".into(), json!(record_id)),
        ("memory".into(), Value::Null),
        ("extra".into(), Value::Null),
        ("root_kind".into(), json!(kind)),
        ("audit_evidence".into(), json!({})),
        ("root_value".into(), audit_value(payload)),
    ]);
    Ok(row)
}

pub(crate) fn write_result_extensions(
    connection: &Connection,
    record_id: i64,
    payload: &Value,
) -> Result<(), AnalysisError> {
    let row = encode_result_root_state(record_id, payload).map_err(super::facts_error)?;
    insert_extension(connection, &row)
}

pub(crate) fn insert_extension(
    connection: &Connection,
    row: &Map<String, Value>,
) -> Result<(), AnalysisError> {
    super::insert_rows(
        connection,
        "execution_extension",
        std::slice::from_ref(row),
        &[
            ("record_id", "integer"),
            ("memory", "json"),
            ("extra", "json"),
            ("root_kind", "text"),
            ("root_value", "json"),
            ("audit_evidence", "json"),
        ],
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn owned_domains_are_excluded_and_original_evidence_is_preserved() {
        let mut payload = Map::new();
        for name in DOMAINS {
            payload.insert((*name).into(), json!({"must_not_duplicate":name}));
        }
        payload.extend(
            json!({"memory":{"diagnostic":[0,null,false]},"extra":{"custom":[]},
            "unknown":false,"audit_evidence":{"original":{"empty":""}}})
            .as_object()
            .unwrap()
            .clone(),
        );
        let batch = encode_extensions(3, &payload).unwrap();
        let row = &batch;
        assert_eq!(row["root_kind"], "object");
        assert_eq!(row["memory"], payload["memory"]);
        assert_eq!(
            row["audit_evidence"],
            json!({"original":{"empty":""},"unrecognized_fields":{"unknown":false}})
        );
        assert!(!serde_json::to_string(row)
            .unwrap()
            .contains("must_not_duplicate"));
    }

    #[test]
    fn historical_root_states_do_not_turn_into_objects() {
        for (value, kind) in [
            (Value::Null, "null"),
            (json!([]), "empty_list"),
            (json!(0), "scalar"),
            (json!(false), "scalar"),
            (json!(""), "scalar"),
        ] {
            let batch = encode_result_root_state(4, &value).unwrap();
            let row = &batch;
            assert_eq!(row["root_kind"], kind);
            assert_eq!(row["root_value"], value);
        }
        assert!(encode_result_root_state(4, &json!([{"orders":[]}])).is_err());
    }

    #[test]
    fn explicit_historical_scalar_state_keeps_nonfinite_evidence_distinct_from_object() {
        let value = json!({"invalid_number":"nan"});
        let scalar = encode_result_root_value(4, "scalar", &value).unwrap();
        assert_eq!(scalar["root_kind"], "scalar");
        assert_eq!(scalar["root_value"], value);
        let object = encode_result_root_state(4, &value).unwrap();
        assert_eq!(object["root_kind"], "object");
        assert!(encode_result_root_value(4, "scalar", &json!({})).is_err());
    }
}
