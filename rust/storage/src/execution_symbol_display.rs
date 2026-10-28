//! Scalar symbol display shared with browser analysis.

use crate::error::AnalysisError;
use serde_json::{json, Map, Value};

pub(crate) const SYMBOL_FIELDS: [&str; 5] = [
    "status",
    "error",
    "outcome",
    "outcome_reason",
    "reason_code",
];

pub(crate) fn symbol_from_row(
    row: &rusqlite::Row<'_>,
) -> Result<Map<String, Value>, AnalysisError> {
    let read_error =
        |error: rusqlite::Error| AnalysisError::new("EXECUTION_READ_ERROR", error.to_string());
    let mut fields = Map::new();
    for name in SYMBOL_FIELDS {
        if let Some(value) = row.get::<_, Option<String>>(name).map_err(read_error)? {
            fields.insert(name.into(), json!(value));
        }
    }
    if let Some(memory) = row.get::<_, Option<String>>("memory").map_err(read_error)? {
        fields.insert(
            "memory".into(),
            serde_json::from_str(&memory)
                .map_err(|error| AnalysisError::new("EXECUTION_READ_ERROR", error.to_string()))?,
        );
    }
    let mut normalized = if row.get::<_, String>("node_kind").map_err(read_error)? == "object" {
        crate::result_normalization::normalized_result(&fields)
            .map_err(|error| AnalysisError::new("BUSINESS_FACTS", error))?
    } else {
        Map::from_iter([("outcome".into(), Value::Null)])
    };
    normalized.retain(|key, _| SYMBOL_FIELDS.contains(&key.as_str()));
    Ok(normalized)
}
