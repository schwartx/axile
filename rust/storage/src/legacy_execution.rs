//! 冻结的旧执行展示字段，供迁移和版本零快照读取共用。

use crate::error::AnalysisError;
use serde_json::{Map, Value};
type Object = Map<String, Value>;
type Result<T> = std::result::Result<T, AnalysisError>;

pub(crate) const RAW_FIELDS: &[&str] = &[
    "status",
    "task_status",
    "error",
    "outcome",
    "outcome_reason",
    "reason_code",
    "msg",
    "execution_kind",
];
pub(crate) const SYMBOL_FIELDS: &[&str] = &[
    "status",
    "error",
    "outcome",
    "outcome_reason",
    "reason_code",
];
fn object(value: &Value) -> Object {
    value.as_object().cloned().unwrap_or_default()
}
fn value<'a>(row: &'a Object, name: &str) -> &'a Value {
    row.get(name).unwrap_or(&Value::Null)
}

pub(crate) fn compact_execution_list(value: &Value) -> Result<Value> {
    let payload = value.as_object().ok_or_else(|| {
        AnalysisError::new(
            "MIGRATION_ERROR",
            "Legacy execution projection must be an object",
        )
    })?;
    compact_execution(payload).map(Value::Object)
}

fn compact_fields(source: &Object, names: &[&str]) -> Object {
    names
        .iter()
        .filter_map(|name| source.get(*name).map(|item| ((*name).into(), item.clone())))
        .collect()
}

pub(crate) fn compact_execution(payload: &Object) -> Result<Object> {
    let mut result = payload.clone();
    let mut record = object(value(payload, "record"));
    let raw = crate::business_facts_ops::result_head::normalize_legacy_result(value(
        &record,
        "raw_result",
    ))
    .map_err(|failure| AnalysisError::new("MIGRATION_VALUE", failure.error))?;
    let raw = object(&raw);
    let mut compact = compact_fields(&raw, RAW_FIELDS);
    if let Some(symbols) = value(&raw, "symbol_results").as_object() {
        compact.insert(
            "symbol_results".into(),
            Value::Object(
                symbols
                    .iter()
                    .map(|(symbol, item)| {
                        let item = object(item);
                        (
                            symbol.clone(),
                            Value::Object(compact_fields(&item, SYMBOL_FIELDS)),
                        )
                    })
                    .collect(),
            ),
        );
        if value(payload, "tradeCount").is_null()
            && symbols
                .values()
                .any(|item| item.get("trades").is_some_and(Value::is_array))
        {
            result.insert(
                "tradeCount".into(),
                Value::from(
                    symbols
                        .values()
                        .map(|item| {
                            item.get("trades")
                                .and_then(Value::as_array)
                                .map_or(0, Vec::len)
                        })
                        .sum::<usize>(),
                ),
            );
        }
    }
    record.insert("raw_result".into(), Value::Object(compact));
    result.insert("record".into(), Value::Object(record));
    result.entry("tradeCount").or_insert(Value::Null);
    Ok(result)
}
