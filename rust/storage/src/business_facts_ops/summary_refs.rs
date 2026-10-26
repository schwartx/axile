//! 冻结摘要、审计附件及通知引用；所有写入借用业务事务，不打开连接或提交。

use rusqlite::{params, Connection, OptionalExtension};
use serde_json::{json, Map, Value};

use super::encode::{py_eq, target_field};
use crate::{business_detail, error::AnalysisError};

pub(crate) const REFERENCE_KEY: &str = "business_facts_ref";
const TARGET_FIELDS: &[&str] = &[
    "curr_target",
    "last_target",
    "strategy_target",
    "account_target",
    "account_weights",
    "normalized_weights",
    "strategy_weights",
    "raw_weights",
    "sizing_context",
    "plan",
    "audit_evidence",
];
const RULE_FIELDS: &[&str] = &["trade_rules", "futures_close_policy"];

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
        || !["input", "targets", "rules", "assets_before", "assets_after"].contains(&view)
    {
        return Err(invalid("审计业务事实引用版本或视图无效"));
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

fn resolve_content(
    conn: &Connection,
    content: &Value,
    reference: &Map<String, Value>,
    execution_id: &Value,
    account_id: Option<i64>,
) -> Result<Value, AnalysisError> {
    let view = reference["view"].as_str().expect("validated view");
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
                        target_field(payload, key)
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

#[cfg(test)]
mod tests {
    use super::*;
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
}
