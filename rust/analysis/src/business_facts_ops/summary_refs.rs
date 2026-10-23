//! 冻结摘要、审计附件及通知引用；所有写入借用业务事务，不打开连接或提交。

use rusqlite::{params, Connection, OptionalExtension};
use serde_json::{json, Map, Value};

use super::encode::py_eq;
use crate::{
    business_detail::{self, EXEC_METADATA},
    error::AnalysisError,
};

pub(crate) const REFERENCE_KEY: &str = "business_facts_ref";
const MARKER: &str = "$axile_notification_context";
const TARGET_FIELDS: &[&str] = &[
    "curr_target",
    "last_target",
    "strategy_target",
    "sizing_context",
    "plan",
    "audit_evidence",
];
const RULE_FIELDS: &[&str] = &["trade_rules", "futures_close_policy"];
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
        .as_object()
        .and_then(|value| value.get(REFERENCE_KEY))
    else {
        return Ok(None);
    };
    let reference = reference
        .as_object()
        .ok_or_else(|| invalid("审计业务事实引用不是对象"))?;
    let view = reference.get("view").and_then(Value::as_str).unwrap_or("");
    if reference.get("version").and_then(Value::as_i64) != Some(1)
        || ![
            "input",
            "targets",
            "rules",
            "assets_before",
            "assets_after",
            "result",
        ]
        .contains(&view)
    {
        return Err(invalid("审计业务事实引用版本或视图无效"));
    }
    if view == "result" {
        validate_summary_reference(reference)?;
        return Ok(Some(reference));
    }
    if let Some(selected) = reference.get("select_fields") {
        let allowed = if view == "targets" {
            TARGET_FIELDS
        } else if view == "rules" {
            RULE_FIELDS
        } else {
            &[]
        };
        let selected = selected
            .as_array()
            .ok_or_else(|| invalid("审计业务事实引用的字段选择无效"))?;
        let mut seen = std::collections::HashSet::new();
        if selected.is_empty()
            || selected.iter().any(|key| {
                key.as_str()
                    .is_none_or(|key| !allowed.contains(&key) || !seen.insert(key))
            })
        {
            return Err(invalid("审计业务事实引用的字段选择无效"));
        }
    }
    for key in ["input_id", "asset_id"] {
        if reference
            .get(key)
            .is_some_and(|value| value.as_str().is_none_or(str::is_empty))
        {
            return Err(invalid("审计业务事实引用身份无效"));
        }
    }
    let required = if ["assets_before", "assets_after"].contains(&view) {
        "asset_id"
    } else {
        "input_id"
    };
    if !reference.contains_key(required) {
        return Err(invalid("审计业务事实引用缺少不可变身份"));
    }
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

fn check_owner(
    owner: &Map<String, Value>,
    execution_id: &Value,
    account_id: Option<i64>,
) -> Result<(), AnalysisError> {
    let primary = owner.get("execution_id").unwrap_or(&Value::Null);
    let identity = owner
        .get("owner_execution_id")
        .filter(|value| !value.is_null())
        .unwrap_or(primary);
    if identity != execution_id
        || (!primary.is_null() && primary != execution_id)
        || account_id.is_some_and(|id| owner.get("account_id").and_then(Value::as_i64) != Some(id))
    {
        return Err(invalid("审计业务事实引用与执行账户不一致"));
    }
    Ok(())
}

pub(crate) fn read_owned_input(
    conn: &Connection,
    input_id: &str,
    execution_id: &Value,
    account_id: Option<i64>,
) -> Result<Value, AnalysisError> {
    let id = json!(input_id);
    let rows = business_detail::load(conn, "execution_input", "id", &id).map_err(invalid)?;
    let head = one(&rows)?;
    check_owner(head, execution_id, account_id)?;
    if head.get("root_kind").and_then(Value::as_str) != Some("object") {
        return head
            .get("root_value")
            .cloned()
            .ok_or_else(|| invalid("执行输入根值缺失"));
    }
    let targets =
        business_detail::load(conn, "execution_input_target", "input_id", &id).map_err(invalid)?;
    let symbols =
        business_detail::load(conn, "execution_input_symbol", "input_id", &id).map_err(invalid)?;
    let plans =
        business_detail::load(conn, "execution_input_plan", "input_id", &id).map_err(invalid)?;
    business_detail::decode_input(head, &targets, &symbols, &plans).map_err(invalid)
}

fn read_owned_asset(
    conn: &Connection,
    reference: &Map<String, Value>,
    execution_id: &Value,
    account_id: Option<i64>,
) -> Result<Value, AnalysisError> {
    let asset_id = &reference["asset_id"];
    let rows = business_detail::load(conn, "business_asset_snapshot", "asset_id", asset_id)
        .map_err(invalid)?;
    let root = one(&rows)?;
    let before = reference["view"] == "assets_before";
    let scope = root.get("scope").and_then(Value::as_str).unwrap_or("");
    if (before && scope != "before") || (!before && !["after", "after_pending"].contains(&scope)) {
        return Err(invalid("审计资产事实引用时点不一致"));
    }
    if let Some(input_id) = root.get("input_id").filter(|value| !value.is_null()) {
        let owners =
            business_detail::load(conn, "execution_input", "id", input_id).map_err(invalid)?;
        check_owner(one(&owners)?, execution_id, account_id)?;
        if reference
            .get("input_id")
            .is_some_and(|value| value != input_id)
        {
            return Err(invalid("审计资产事实与输入引用不一致"));
        }
    } else if let Some(record_id) = root.get("record_id").filter(|value| !value.is_null()) {
        let owners =
            business_detail::load(conn, "executerecord", "id", record_id).map_err(invalid)?;
        let owner = one(&owners)?;
        check_owner(owner, execution_id, account_id)?;
        if reference
            .get("input_id")
            .is_some_and(|value| owner.get("input_id") != Some(value))
        {
            return Err(invalid("审计资产事实与输入引用不一致"));
        }
    } else {
        return Err(invalid("执行审计资产事实缺少执行所有者"));
    }
    let positions = business_detail::load(conn, "business_asset_position", "asset_id", asset_id)
        .map_err(invalid)?;
    crate::assets::observation_value(root, &positions).map_err(invalid)
}

pub(crate) fn read_owned_result(
    conn: &Connection,
    record_id: i64,
    execution_id: &Value,
    account_id: Option<i64>,
) -> Result<Value, AnalysisError> {
    let rows =
        business_detail::load(conn, "executerecord", "id", &json!(record_id)).map_err(invalid)?;
    let record = one(&rows)?;
    check_owner(record, execution_id, account_id)?;
    let owner = record
        .get("account_id")
        .and_then(Value::as_i64)
        .ok_or_else(|| invalid("执行账户身份无效"))?;
    business_detail::read_result(conn, owner, record_id).map_err(invalid)
}

fn resolve_content(
    conn: &Connection,
    content: &Value,
    reference: &Map<String, Value>,
    execution_id: &Value,
    account_id: Option<i64>,
) -> Result<Value, AnalysisError> {
    let view = reference["view"].as_str().expect("validated view");
    if view == "result" {
        let result = read_owned_result(
            conn,
            reference["record_id"].as_i64().expect("validated identity"),
            execution_id,
            account_id,
        )?;
        return hydrate_summary_content(content, &result, true);
    }
    let fields = if ["assets_before", "assets_after"].contains(&view) {
        Map::from_iter([(
            "account_assets".into(),
            read_owned_asset(conn, reference, execution_id, account_id)?,
        )])
    } else {
        let payload = read_owned_input(
            conn,
            reference["input_id"].as_str().expect("validated identity"),
            execution_id,
            account_id,
        )?;
        if view == "input" {
            Map::from_iter([("input".into(), payload)])
        } else {
            let payload = payload
                .as_object()
                .ok_or_else(|| invalid("审计目标或规则引用的输入不是对象"))?;
            if let Some(selected) = reference.get("select_fields").and_then(Value::as_array) {
                let mut fields = Map::new();
                for key in selected {
                    let key = key.as_str().expect("validated field");
                    fields.insert(
                        key.into(),
                        payload
                            .get(key)
                            .ok_or_else(|| invalid("审计业务事实引用的选定字段缺失"))?
                            .clone(),
                    );
                }
                fields
            } else {
                let fields = if view == "targets" {
                    TARGET_FIELDS
                } else {
                    RULE_FIELDS
                };
                fields
                    .iter()
                    .filter_map(|key| {
                        payload
                            .get(*key)
                            .map(|value| ((*key).into(), value.clone()))
                    })
                    .collect()
            }
        }
    };
    let mut hydrated = object(content)?.clone();
    if fields.iter().any(|(key, value)| {
        hydrated
            .get(key)
            .is_some_and(|existing| !py_eq(existing, value))
    }) {
        return Err(invalid("审计附件正文与不可变引用冲突"));
    }
    hydrated.extend(fields);
    Ok(Value::Object(hydrated))
}

fn artifact_type(view: &str) -> &str {
    match view {
        "input" => "standard_input",
        "targets" => "target_snapshot",
        "rules" => "trade_rules_snapshot",
        "assets_before" => "account_snapshot_before",
        "assets_after" => "account_snapshot",
        "result" => "execution_summary",
        _ => unreachable!("validated artifact view"),
    }
}

pub(crate) fn resolve_artifact_content(
    conn: &Connection,
    artifact: &Value,
    account_id: Option<i64>,
) -> Result<Value, AnalysisError> {
    let artifact = object(artifact)?;
    let content = artifact
        .get("content")
        .ok_or_else(|| invalid("审计附件正文不是对象"))?;
    object(content)?;
    let Some(reference) = parse_artifact_reference(content)? else {
        return Ok(content.clone());
    };
    let view = reference["view"].as_str().expect("validated view");
    if !artifact
        .get("artifact_type")
        .and_then(Value::as_str)
        .is_some_and(|kind| kind.eq_ignore_ascii_case(artifact_type(view)))
    {
        return Err(invalid("审计业务事实引用与附件类型不一致"));
    }
    resolve_content(
        conn,
        content,
        reference,
        artifact.get("execution_id").unwrap_or(&Value::Null),
        account_id,
    )
}

pub(crate) fn resolve_event_details(
    conn: &Connection,
    event: &Value,
    account_id: Option<i64>,
) -> Result<Value, AnalysisError> {
    let event = object(event)?;
    let details = event.get("details").unwrap_or(&Value::Null);
    let Some(decision) = details.get("decision").filter(|value| value.is_object()) else {
        return Ok(details.clone());
    };
    let Some(reference) = parse_artifact_reference(decision)? else {
        return Ok(details.clone());
    };
    if reference["view"] != "targets"
        || !reference.contains_key("select_fields")
        || !event
            .get("event_type")
            .and_then(Value::as_str)
            .is_some_and(|kind| kind.eq_ignore_ascii_case("target_computed"))
    {
        return Err(invalid("执行业务事实引用与事件类型不一致"));
    }
    let resolved = resolve_content(
        conn,
        decision,
        reference,
        event.get("execution_id").unwrap_or(&Value::Null),
        account_id,
    )?;
    let mut details = object(details)?.clone();
    details.insert("decision".into(), resolved);
    Ok(Value::Object(details))
}

pub(crate) fn read_artifact_input_id(
    conn: &Connection,
    execution_id: &str,
    view: &str,
    account_id: Option<i64>,
) -> Result<Option<String>, AnalysisError> {
    if !["input", "targets", "rules"].contains(&view) {
        return Err(invalid("附件输入引用视图无效"));
    }
    let content: Option<String> = conn.query_row("SELECT content FROM execution_artifact WHERE execution_id=?1 AND lower(artifact_type)=?2", params![execution_id,artifact_type(view)], |row| row.get(0)).optional().map_err(sql)?;
    let content = match content {
        Some(value) => serde_json::from_str(&value).map_err(|error| invalid(error.to_string()))?,
        None => return Ok(None),
    };
    let Some(reference) = parse_artifact_reference(&content)? else {
        return Ok(None);
    };
    if reference["view"] != view {
        return Err(invalid("审计业务事实引用与附件类型不一致"));
    }
    let input_id = reference["input_id"].as_str().expect("validated identity");
    let owners =
        business_detail::load(conn, "execution_input", "id", &json!(input_id)).map_err(invalid)?;
    check_owner(one(&owners)?, &json!(execution_id), account_id)?;
    Ok(Some(input_id.into()))
}

pub(crate) fn compact_execution_summary(
    conn: &Connection,
    record_id: i64,
    execution_id: &str,
    result: &Value,
) -> Result<bool, AnalysisError> {
    let artifact: Option<(i64,i64,String)> = conn.query_row(
        "SELECT id,schema_version,content FROM execution_artifact WHERE execution_id=?1 AND lower(artifact_type)='execution_summary'",
        [execution_id], |row| Ok((row.get(0)?,row.get(1)?,row.get(2)?)),
    ).optional().map_err(sql)?;
    let Some((artifact_id, schema_version, content)) = artifact else {
        return Ok(false);
    };
    let content: Value =
        serde_json::from_str(&content).map_err(|error| invalid(error.to_string()))?;
    if parse_artifact_reference(&content)?.is_some() {
        return Ok(false);
    }
    let Some(mut compact) = compact_summary_content(&content, result, record_id) else {
        return Ok(false);
    };
    compact[REFERENCE_KEY]["legacy_schema_version"] = json!(schema_version);
    conn.execute(
        "UPDATE execution_artifact SET content=?1,schema_version=3 WHERE id=?2",
        params![
            serde_json::to_string(&compact).map_err(|error| invalid(error.to_string()))?,
            artifact_id
        ],
    )
    .map_err(sql)?;
    Ok(true)
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
    let result = business_detail::read_result(conn, account_id, record_id).map_err(invalid)?;
    for (artifact_id, artifact_execution, content) in artifacts {
        check_owner(record, &json!(artifact_execution), Some(account_id))?;
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
        "event_sha256":business_detail::digest(event_context).ok()?,
        "capture_sha256":business_detail::digest(captured).ok()?,
        "root_order":captured_map.keys().collect::<Vec<_>>(),
        "root_metadata":root_metadata,
        "execution_state":if execution.is_some() {"object"} else if captured.get("execution").is_some() {"null"} else {"absent"},
        "execution_order":execution.map(|value| value.keys().collect::<Vec<_>>()).unwrap_or_default(),
        "execution_metadata":execution_metadata,
    });
    let restored = business_detail::hydrate_notification_context(
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

pub(crate) fn compact_record_notification_context(
    conn: &Connection,
    record_id: i64,
    event: &Value,
) -> Result<bool, AnalysisError> {
    let records =
        business_detail::load(conn, "executerecord", "id", &json!(record_id)).map_err(invalid)?;
    let record = one(&records)?;
    let Some(execution_id) = record.get("execution_id").and_then(Value::as_str) else {
        return Ok(false);
    };
    let event = object(event)?;
    let event_id = format!("execution:{execution_id}");
    if event.get("account_id") != record.get("account_id")
        || event.get("id").and_then(Value::as_str) != Some(event_id.as_str())
        || event.get("event_type").and_then(Value::as_str) != Some("execution.finished")
    {
        return Err(invalid("冻结通知与执行记录身份不一致"));
    }
    let rows = business_detail::load(conn, "execution_extension", "record_id", &json!(record_id))
        .map_err(invalid)?;
    let Some(extra) = rows
        .first()
        .and_then(|row| row.get("extra"))
        .and_then(Value::as_object)
    else {
        return Ok(false);
    };
    let Some(captured) = extra
        .get("server_notification_context")
        .filter(|value| value.is_object())
    else {
        return Ok(false);
    };
    let Some(reference) = event
        .get("context")
        .and_then(|context| compact_captured_context(captured, context, &event_id))
    else {
        return Ok(false);
    };
    let mut extra = extra.clone();
    extra.insert("server_notification_context".into(), reference);
    conn.execute(
        "UPDATE execution_extension SET extra=?1 WHERE record_id=?2",
        params![
            serde_json::to_string(&extra).map_err(|error| invalid(error.to_string()))?,
            record_id
        ],
    )
    .map_err(sql)?;
    Ok(true)
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
        let restored = business_detail::hydrate_notification_context(
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
        assert!(business_detail::hydrate_notification_context(
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
            let restored = business_detail::hydrate_notification_context(
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
    fn notification_compression_uses_callers_transaction_and_checks_ownership() {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE executerecord(id INTEGER,account_id INTEGER,execution_id TEXT);
            CREATE TABLE execution_extension(record_id INTEGER,extra JSON);
            INSERT INTO executerecord VALUES(1,7,'e1');",
        )
        .unwrap();
        let extra =
            json!({"independent":{"null":null,"zero":0},"server_notification_context":capture()});
        conn.execute(
            "INSERT INTO execution_extension VALUES(1,?1)",
            [serde_json::to_string(&extra).unwrap()],
        )
        .unwrap();
        let event = json!({"id":"execution:e1","account_id":7,"event_type":"execution.finished","context":event_context()});
        conn.execute_batch("BEGIN IMMEDIATE").unwrap();
        assert!(compact_record_notification_context(&conn, 1, &event).unwrap());
        assert!(!conn.is_autocommit());
        conn.execute_batch("ROLLBACK").unwrap();
        let restored: String = conn
            .query_row(
                "SELECT extra FROM execution_extension WHERE record_id=1",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(serde_json::from_str::<Value>(&restored).unwrap(), extra);
        let mut wrong = event.clone();
        wrong["account_id"] = json!(8);
        assert_eq!(
            compact_record_notification_context(&conn, 1, &wrong)
                .unwrap_err()
                .error,
            "冻结通知与执行记录身份不一致"
        );
    }

    #[test]
    fn input_identity_lookup_does_not_decode_unrelated_business_data() {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch("CREATE TABLE execution_artifact(execution_id TEXT,artifact_type TEXT,content JSON);
            CREATE TABLE execution_input(id TEXT,account_id INTEGER,execution_id TEXT,owner_execution_id TEXT);
            INSERT INTO execution_input VALUES('i1',7,'e1','e1');").unwrap();
        conn.execute(
            "INSERT INTO execution_artifact VALUES('e1','TARGET_SNAPSHOT',?1)",
            [
                json!({"business_facts_ref":{"version":1,"view":"targets","input_id":"i1"}})
                    .to_string(),
            ],
        )
        .unwrap();
        assert_eq!(
            read_artifact_input_id(&conn, "e1", "targets", Some(7)).unwrap(),
            Some("i1".into())
        );
        assert_eq!(
            read_artifact_input_id(&conn, "e1", "targets", Some(8))
                .unwrap_err()
                .error,
            "审计业务事实引用与执行账户不一致"
        );
    }

    #[test]
    fn summary_compaction_rolls_back_with_callers_transaction() {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE execution_artifact(id INTEGER,execution_id TEXT,
            artifact_type TEXT,schema_version INTEGER,content JSON);",
        )
        .unwrap();
        let result = json!({"symbol_results":{"A":{"orders":[],"memory":null}}});
        let content =
            json!({"symbol_results":{"A":{"orders":[],"memory":null}},"evidence":{"zero":0}});
        conn.execute(
            "INSERT INTO execution_artifact VALUES(1,'e1','EXECUTION_SUMMARY',2,?1)",
            [content.to_string()],
        )
        .unwrap();
        conn.execute_batch("BEGIN IMMEDIATE").unwrap();
        assert!(compact_execution_summary(&conn, 7, "e1", &result).unwrap());
        assert!(!conn.is_autocommit());
        let compact: String = conn
            .query_row("SELECT content FROM execution_artifact", [], |row| {
                row.get(0)
            })
            .unwrap();
        assert_eq!(
            serde_json::from_str::<Value>(&compact).unwrap()[REFERENCE_KEY]
                ["legacy_schema_version"],
            2
        );
        conn.execute_batch("ROLLBACK").unwrap();
        let restored: (i64, String) = conn
            .query_row(
                "SELECT schema_version,content FROM execution_artifact",
                [],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .unwrap();
        assert_eq!(restored.0, 2);
        assert_eq!(serde_json::from_str::<Value>(&restored.1).unwrap(), content);
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
