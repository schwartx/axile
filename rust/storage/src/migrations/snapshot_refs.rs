//! 0037—0040 摘要和通知去重格式；仅历史迁移使用。

use super::notification_refs::{self, EXEC_METADATA};
use crate::business_detail;
use crate::business_facts_ops::{
    encode::py_eq,
    summary_refs::{self as artifact_refs, REFERENCE_KEY},
};
use crate::error::AnalysisError;
use rusqlite::{params, Connection};
use serde_json::{json, Map, Value};
const MARKER: &str = "$axile_notification_context";
const ROOT_METADATA: &[&str] = &["event_id", "event_type"];
fn invalid(message: impl Into<String>) -> AnalysisError {
    AnalysisError::new("BUSINESS_FACTS_REFERENCE", message.into())
}
fn sql(error: rusqlite::Error) -> AnalysisError {
    AnalysisError::new("BUSINESS_FACTS_SQL", error.to_string())
}
fn object(value: &Value) -> Result<&Map<String, Value>, AnalysisError> {
    value
        .as_object()
        .ok_or_else(|| invalid("审计附件正文不是对象"))
}
fn one(rows: &[Map<String, Value>]) -> Result<&Map<String, Value>, AnalysisError> {
    if rows.len() != 1 {
        return Err(invalid("审计业务事实引用目标不存在或不唯一"));
    }
    Ok(&rows[0])
}

pub(crate) fn validate_summary_reference(
    reference: &Map<String, Value>,
) -> Result<(), AnalysisError> {
    if !reference
        .get("record_id")
        .and_then(Value::as_i64)
        .is_some_and(|id| id > 0)
    {
        return Err(invalid("摘要引用缺少终态记录身份"));
    }
    if reference.get("projection_version").and_then(Value::as_i64) != Some(2) {
        return Err(invalid("摘要业务视图版本无效"));
    }
    Ok(())
}

pub(crate) fn parse_artifact_reference(
    content: &Value,
) -> Result<Option<&Map<String, Value>>, AnalysisError> {
    let Some(reference) = content
        .get(REFERENCE_KEY)
        .and_then(Value::as_object)
        .filter(|reference| reference.get("view").and_then(Value::as_str) == Some("result"))
    else {
        return artifact_refs::parse_artifact_reference(content);
    };
    if reference.get("version").and_then(Value::as_i64) != Some(1) {
        return Err(invalid("审计业务事实引用版本或视图无效"));
    }
    validate_summary_reference(reference)?;
    Ok(Some(reference))
}

pub(crate) fn compact_summary_content(
    content: &Value,
    result: &Value,
    record_id: i64,
) -> Option<Value> {
    let content = content.as_object()?;
    if content.contains_key(REFERENCE_KEY) {
        return None;
    }
    let facts = result.get("symbol_results")?.as_object()?;
    let observed = content.get("symbol_results")?.as_object()?;
    let heavy = [
        "orders",
        "trades",
        "sizing",
        "first_tick",
        "account_assets",
        "memory",
        "technical_detail",
    ];
    let mut kept = Map::new();
    for (symbol, row) in observed {
        let row = match row.as_object() {
            None => row.clone(),
            Some(row) => Value::Object(
                row.iter()
                    .filter(|(key, value)| {
                        !heavy.contains(&key.as_str())
                            || facts
                                .get(symbol)
                                .and_then(Value::as_object)
                                .and_then(|fact| fact.get(*key))
                                .is_none_or(|fact| !py_eq(fact, value))
                    })
                    .map(|(key, value)| (key.clone(), value.clone()))
                    .collect(),
            ),
        };
        kept.insert(symbol.clone(), row);
    }
    let mut compact = content.clone();
    compact.insert("symbol_results".into(), Value::Object(kept));
    compact.insert(
        REFERENCE_KEY.into(),
        json!({"version":1,"view":"result","record_id":record_id,"projection_version":2}),
    );
    Some(Value::Object(compact))
}

pub(crate) fn hydrate_summary_content(
    content: &Value,
    result: &Value,
    include_reference: bool,
) -> Result<Value, AnalysisError> {
    let mut hydrated = object(content)?.clone();
    let reference = hydrated
        .get(REFERENCE_KEY)
        .and_then(Value::as_object)
        .filter(|reference| reference.get("view").and_then(Value::as_str) == Some("result"))
        .ok_or_else(|| invalid("执行摘要缺少结果引用"))?;
    validate_summary_reference(reference)?;
    let facts = result
        .get("symbol_results")
        .and_then(Value::as_object)
        .ok_or_else(|| invalid("摘要缺少冻结品种事实"))?;
    if let Some(kept) = hydrated.get("symbol_results").and_then(Value::as_object) {
        let mut merged = Map::new();
        for (symbol, evidence) in kept {
            let fact = facts
                .get(symbol)
                .ok_or_else(|| invalid("摘要品种事实缺失"))?;
            let value = match (fact.as_object(), evidence.as_object()) {
                (Some(fact), Some(evidence)) => {
                    let mut value = fact.clone();
                    value.extend(evidence.clone());
                    Value::Object(value)
                }
                _ => evidence.clone(),
            };
            merged.insert(symbol.clone(), value);
        }
        hydrated.insert("symbol_results".into(), Value::Object(merged));
    }
    if !include_reference {
        hydrated.shift_remove(REFERENCE_KEY);
    }
    Ok(Value::Object(hydrated))
}

pub(crate) fn detach_execution_summaries(
    conn: &Connection,
    record_id: i64,
) -> Result<(), AnalysisError> {
    let mut statement = conn.prepare(
        "SELECT id,execution_id,content FROM execution_artifact WHERE lower(artifact_type)='execution_summary'
         AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
         AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END=?1",
    ).map_err(sql)?;
    let artifacts = statement
        .query_map([record_id], |row| {
            Ok((
                row.get::<_, i64>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
            ))
        })
        .map_err(sql)?
        .collect::<Result<Vec<_>, _>>()
        .map_err(sql)?;
    if artifacts.is_empty() {
        return Ok(());
    }
    let records =
        business_detail::load(conn, "executerecord", "id", &json!(record_id)).map_err(invalid)?;
    let record = one(&records)?;
    let execution_id = record.get("execution_id").unwrap_or(&Value::Null);
    let account_id = record
        .get("account_id")
        .and_then(Value::as_i64)
        .ok_or_else(|| invalid("执行账户身份无效"))?;
    let mut result = business_detail::read_result(conn, account_id, record_id).map_err(invalid)?;
    if let Some(result) = result.as_object_mut() {
        notification_refs::hydrate(conn, record, result).map_err(invalid)?;
    }
    for (artifact_id, artifact_execution, content) in artifacts {
        if execution_id != &json!(artifact_execution) {
            return Err(invalid("审计业务事实引用与执行账户不一致"));
        }
        let content: Value =
            serde_json::from_str(&content).map_err(|error| invalid(error.to_string()))?;
        let reference =
            parse_artifact_reference(&content)?.ok_or_else(|| invalid("执行摘要缺少结果引用"))?;
        let schema_version = reference
            .get("legacy_schema_version")
            .unwrap_or(&json!(2))
            .as_i64()
            .filter(|version| *version > 0)
            .ok_or_else(|| invalid("执行摘要原协议版本无效"))?;
        let hydrated = hydrate_summary_content(&content, &result, false)?;
        conn.execute(
            "UPDATE execution_artifact SET content=?1,schema_version=?2 WHERE id=?3",
            params![
                serde_json::to_string(&hydrated).map_err(|error| invalid(error.to_string()))?,
                schema_version,
                artifact_id
            ],
        )
        .map_err(sql)?;
    }
    Ok(())
}

fn business_context(context: &Map<String, Value>) -> Option<Value> {
    let mut business: Map<String, Value> = context
        .iter()
        .filter(|(key, _)| !ROOT_METADATA.contains(&key.as_str()) && key.as_str() != "execution")
        .map(|(key, value)| (key.clone(), value.clone()))
        .collect();
    let execution = match context.get("execution") {
        Some(Value::Object(execution)) => execution
            .iter()
            .filter(|(key, _)| !EXEC_METADATA.contains(&key.as_str()))
            .map(|(key, value)| (key.clone(), value.clone()))
            .collect(),
        None | Some(Value::Null) => Map::new(),
        _ => return None,
    };
    business.insert("execution".into(), Value::Object(execution));
    Some(Value::Object(business))
}

pub(crate) fn compact_captured_context(
    captured: &Value,
    event_context: &Value,
    event_id: &str,
) -> Option<Value> {
    let captured_map = captured.as_object()?;
    let event_map = event_context.as_object()?;
    if captured_map.contains_key(MARKER) {
        return None;
    }
    let same_business = {
        let business = business_context(captured_map)?;
        let event_business = business_context(event_map)?;
        business_detail::wire(&business).ok()? == business_detail::wire(&event_business).ok()?
    };
    if !same_business {
        return None;
    }
    let root_metadata: Map<String, Value> = captured_map
        .iter()
        .filter(|(key, _)| ROOT_METADATA.contains(&key.as_str()))
        .map(|(key, value)| (key.clone(), value.clone()))
        .collect();
    let execution = captured_map.get("execution").and_then(Value::as_object);
    let execution_metadata: Map<String, Value> = execution
        .map(|execution| {
            execution
                .iter()
                .filter(|(key, _)| EXEC_METADATA.contains(&key.as_str()))
                .map(|(key, value)| (key.clone(), value.clone()))
                .collect()
        })
        .unwrap_or_default();
    let reference = json!({
        "$axile_notification_context":1,"event_id":event_id,
        "event_sha256":notification_refs::digest(event_context).ok()?,
        "capture_sha256":notification_refs::digest(captured).ok()?,
        "root_order":captured_map.keys().collect::<Vec<_>>(),
        "root_metadata":root_metadata,
        "execution_state":if execution.is_some() {"object"} else if captured.get("execution").is_some() {"null"} else {"absent"},
        "execution_order":execution.map(|value| value.keys().collect::<Vec<_>>()).unwrap_or_default(),
        "execution_metadata":execution_metadata,
    });
    let restored = notification_refs::hydrate_notification_context(
        reference.as_object()?,
        event_id,
        event_context,
    )
    .ok()?;
    if business_detail::wire(&restored).ok()? != business_detail::wire(captured).ok()? {
        return None;
    }
    Some(reference)
}

pub(crate) fn compact_notification_capture(
    conn: &Connection,
    record: &Map<String, Value>,
    result: &Value,
) -> Result<Value, AnalysisError> {
    let Some(execution_id) = record.get("execution_id").and_then(Value::as_str) else {
        return Ok(result.clone());
    };
    let Some(captured) = result
        .get("extra")
        .and_then(|extra| extra.get("server_notification_context"))
        .filter(|value| value.is_object())
    else {
        return Ok(result.clone());
    };
    let event_id = format!("execution:{execution_id}");
    let events = business_detail::load(conn, "notification_event", "id", &json!(event_id))
        .map_err(invalid)?;
    if events.is_empty() {
        return Ok(result.clone());
    }
    let event = one(&events)?;
    if event.get("account_id") != record.get("account_id")
        || event.get("event_type").and_then(Value::as_str) != Some("execution.finished")
    {
        return Ok(result.clone());
    }
    let Some(reference) = event
        .get("context")
        .and_then(|context| compact_captured_context(captured, context, &event_id))
    else {
        return Ok(result.clone());
    };
    let mut result = result.clone();
    result["extra"]["server_notification_context"] = reference;
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn capture() -> Value {
        json!({"execution":{"status":"PARTIAL","notified_at":"t"},
            "assets":{"total_assets":0.0,"market_value":null},"positions":[]})
    }

    fn event_context() -> Value {
        json!({"execution":{"status":"PARTIAL","notified_at":"later","id":"e1"},
            "assets":{"total_assets":0.0,"market_value":null},"positions":[],"event_id":"execution:e1"})
    }

    #[test]
    fn summary_keeps_unique_evidence_and_only_hydrates_its_symbols() {
        let fact = json!({"status":"SUCCEEDED","orders":[{"order_id":"0","price":2}],"sizing":{"target_quantity":0}});
        let result = json!({"symbol_results":{"A":fact,"unrelated":{"orders":[1,2,3]}}});
        let content = json!({"symbol_results":{"A":{"status":"SUCCEEDED","orders":[{"order_id":"0","price":2}],
            "sizing":{"target_quantity":3}}},"independent":{"null":null,"zero":0,"false":false}});
        let compact = compact_summary_content(&content, &result, 7).unwrap();
        assert!(compact["symbol_results"]["A"].get("orders").is_none());
        assert_eq!(
            compact["symbol_results"]["A"]["sizing"]["target_quantity"],
            3
        );
        assert_eq!(
            hydrate_summary_content(&compact, &result, false).unwrap(),
            content
        );
        assert!(compact_summary_content(&compact, &result, 7).is_none());
        let mut missing = result.clone();
        missing["symbol_results"]
            .as_object_mut()
            .unwrap()
            .shift_remove("A");
        assert_eq!(
            hydrate_summary_content(&compact, &missing, false)
                .unwrap_err()
                .error,
            "摘要品种事实缺失"
        );
    }

    #[test]
    fn malformed_summary_identity_and_projection_are_rejected() {
        for identity in [json!(true), json!(0), json!("1"), json!(1.0)] {
            let content = json!({"business_facts_ref":{"version":1,"view":"result","record_id":identity,"projection_version":2}});
            assert_eq!(
                parse_artifact_reference(&content).unwrap_err().error,
                "摘要引用缺少终态记录身份"
            );
        }
        let content = json!({"business_facts_ref":{"version":1,"view":"result","record_id":1,"projection_version":2.0}});
        assert_eq!(
            parse_artifact_reference(&content).unwrap_err().error,
            "摘要业务视图版本无效"
        );
    }

    #[test]
    fn frozen_capture_matches_python_hashes_and_preserves_order() {
        let captured = capture();
        let event = event_context();
        let reference = compact_captured_context(&captured, &event, "execution:e1").unwrap();
        assert_eq!(
            reference["capture_sha256"],
            "03a1afa43e8fbc0858cb0ea046449e2bea12138e2841f676cd60645a53260d96"
        );
        assert_eq!(
            reference["event_sha256"],
            "533a941a42eed59dcd43822b753b97366b669b70ae80f2c6cfab36e0a1f94ca0"
        );
        let restored = notification_refs::hydrate_notification_context(
            reference.as_object().unwrap(),
            "execution:e1",
            &event,
        )
        .unwrap();
        assert_eq!(
            business_detail::wire(&restored).unwrap(),
            business_detail::wire(&captured).unwrap()
        );
        let mut changed = event.clone();
        changed["assets"]["total_assets"] = json!(0);
        assert!(compact_captured_context(&captured, &changed, "execution:e1").is_none());
        assert!(notification_refs::hydrate_notification_context(
            reference.as_object().unwrap(),
            "execution:e1",
            &changed
        )
        .is_err());
    }

    #[test]
    fn notification_missing_null_empty_and_nested_overrides_are_distinct() {
        let event = event_context();
        let mut captured = capture();
        for state in [None, Some(Value::Null), Some(json!({}))] {
            captured.as_object_mut().unwrap().shift_remove("execution");
            if let Some(value) = state {
                captured["execution"] = value;
            }
            let reference = compact_captured_context(&captured, &event, "execution:e1").unwrap();
            let restored = notification_refs::hydrate_notification_context(
                reference.as_object().unwrap(),
                "execution:e1",
                &event,
            )
            .unwrap();
            assert_eq!(
                business_detail::wire(&restored).unwrap(),
                business_detail::wire(&captured).unwrap()
            );
        }
        captured["execution"] = json!({"error":{"orders":[]}});
        assert!(compact_captured_context(&captured, &event, "execution:e1").is_none());
    }

    #[test]
    fn detaching_one_record_does_not_materialize_unrelated_history() {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch("CREATE TABLE execution_artifact(id INTEGER,execution_id TEXT,
            artifact_type TEXT,schema_version INTEGER,content JSON);
            INSERT INTO execution_artifact VALUES(1,'unrelated','EXECUTION_SUMMARY',3,
                '{\"business_facts_ref\":{\"view\":\"result\",\"record_id\":99}}');
            INSERT INTO execution_artifact VALUES(2,'legacy','EXECUTION_SUMMARY',1,'invalid JSON');").unwrap();
        detach_execution_summaries(&conn, 7).unwrap();
        let count: i64 = conn
            .query_row("SELECT count(*) FROM execution_artifact", [], |row| {
                row.get(0)
            })
            .unwrap();
        assert_eq!(count, 2);
    }
}
