//! Python 编码契约的 Rust 移植:列投影、稀疏审计证据、输入与资产事实编码。
//!
//! 错误消息与 Python ValueError 保持一致;数值等价比较沿用 Python 语义
//! (1 == 1.0,True == 1),重放判定依赖该语义。JSON 对象键天然是字符串,
//! 因此 Python 中针对非字符串字典键的防御检查在此不可达。

use serde_json::{json, Map, Value};

use crate::business_detail;

/// 事实批:按插入顺序排列的 (表名, 行) 集合。
pub(super) struct FactBatch {
    statements: Vec<(&'static str, Vec<Map<String, Value>>)>,
}

impl FactBatch {
    pub(super) fn new() -> Self {
        Self {
            statements: Vec::new(),
        }
    }
    pub(super) fn push(&mut self, table: &'static str, rows: Vec<Map<String, Value>>) {
        self.statements.push((table, rows));
    }
    pub(super) fn statements(&self) -> &[(&'static str, Vec<Map<String, Value>>)] {
        &self.statements
    }
}

const INPUT_FIELDS: &[(&str, &str)] = &[
    ("channel_type", "text"),
    ("execution_timeout", "integer"),
    ("algorithm", "json"),
    ("symbol_algorithms", "json"),
    ("trade_rules", "json"),
    ("futures_close_policy", "json"),
    ("feishu_key", "json"),
    ("feishu_account", "json"),
    ("extra", "json"),
];
const TARGET_ROLES: &[&str] = &["curr_target", "last_target", "strategy_target"];
const TARGET_ALIASES: &[(&str, &str)] = &[
    ("account_target", "curr_target"),
    ("account_weights", "curr_target"),
    ("normalized_weights", "curr_target"),
    ("strategy_weights", "strategy_target"),
    ("raw_weights", "strategy_target"),
];
const SIZING_FIELDS: &[&str] = &["long_leverage", "short_leverage", "weight_precision"];
const SIZING_SPECS: &[(&str, &str)] = &[
    ("long_leverage", "number"),
    ("short_leverage", "number"),
    ("weight_precision", "number"),
];
const PLAN_FIELDS: &[&str] = &["quantities", "target_units"];
const QUANTITY_ALIASES: &[&str] = &["target_quantities", "target_quantity", "target_volume"];
const CHILD_FIELDS: &[&str] = &[
    "curr_target",
    "last_target",
    "strategy_target",
    "forbidden_symbols",
    "risk_symbols",
    "sizing_context",
    "quantities",
    "target_units",
];
const COUNTED_ROLES: &[&str] = &[
    "curr_target",
    "last_target",
    "strategy_target",
    "quantities",
    "target_units",
    "forbidden_symbols",
    "risk_symbols",
];

const ASSET_FIELDS: &[(&str, &str)] = &[
    ("available_cash", "number"),
    ("total_asset", "number"),
    ("market_value", "number"),
    ("currency", "text"),
    ("update_time", "text"),
    ("source", "text"),
    ("extra", "json"),
];
const POSITION_FIELDS: &[(&str, &str)] = &[
    ("symbol", "text"),
    ("volume", "number"),
    ("available_volume", "number"),
    ("market_value", "number"),
    ("direction", "text"),
    ("avg_price", "number"),
    ("extra", "json"),
];
const POSITION_AMOUNTS: &[&str] = &["volume", "available_volume", "market_value", "avg_price"];

// ---------------------------------------------------------------------------
// Python 值等价
// ---------------------------------------------------------------------------

fn as_number(value: &Value) -> Option<f64> {
    match value {
        Value::Number(number) => number.as_f64(),
        Value::Bool(flag) => Some(if *flag { 1.0 } else { 0.0 }),
        _ => None,
    }
}

fn as_int(value: &Value) -> Option<i64> {
    match value {
        Value::Number(number) => number.as_i64(),
        Value::Bool(flag) => Some(*flag as i64),
        _ => None,
    }
}

/// Python `left == right`:布尔与数字互相按数值相等,数字跨 int/float 相等。
pub(super) fn py_eq(left: &Value, right: &Value) -> bool {
    match (left, right) {
        (Value::Null, Value::Null) => true,
        (Value::Bool(_), Value::Bool(_)) => left == right,
        (Value::Bool(_) | Value::Number(_), Value::Bool(_) | Value::Number(_)) => {
            match (as_int(left), as_int(right)) {
                // 双方可精确表示为整数时按整数比较,避开 f64 精度丢失。
                (Some(l), Some(r)) => l == r,
                _ => as_number(left).is_some_and(|l| as_number(right).is_some_and(|r| l == r)),
            }
        }
        (Value::String(l), Value::String(r)) => l == r,
        (Value::Array(l), Value::Array(r)) => {
            l.len() == r.len() && l.iter().zip(r).all(|(l, r)| py_eq(l, r))
        }
        (Value::Object(l), Value::Object(r)) => {
            l.len() == r.len()
                && l.iter()
                    .all(|(key, value)| r.get(key).is_some_and(|other| py_eq(value, other)))
        }
        _ => false,
    }
}

// ---------------------------------------------------------------------------
// 列投影与审计证据 (columns.py)
// ---------------------------------------------------------------------------

fn valid(value: &Value, kind: &str) -> bool {
    if value.is_null() {
        return true;
    }
    match kind {
        "json" => true,
        "identity" => value.is_string() || matches!(value, Value::Number(n) if n.is_i64()),
        "text" => matches!(value, Value::String(_)),
        "boolean" => matches!(value, Value::Bool(_)),
        "integer" => matches!(value, Value::Number(n) if n.is_i64()),
        // number: bool 不算数字;JSON 输入不含非有限数。
        _ => matches!(value, Value::Number(_)),
    }
}

pub(super) fn audit_value(value: &Value) -> Value {
    match value {
        Value::Object(map) => Value::Object(
            map.iter()
                .map(|(k, v)| (k.clone(), audit_value(v)))
                .collect(),
        ),
        Value::Array(items) => Value::Array(items.iter().map(audit_value).collect()),
        other => other.clone(),
    }
}

type StoreResult = Result<(Map<String, Value>, Map<String, Value>), String>;

fn store_columns(
    payload: &Map<String, Value>,
    specs: &[(&str, &str)],
    child_keys: &[&str],
) -> StoreResult {
    let mut columns = Map::new();
    let mut invalid_fields = Map::new();
    for (name, kind) in specs {
        let value = payload.get(*name).unwrap_or(&Value::Null);
        if valid(value, kind) {
            columns.insert(
                (*name).into(),
                if *kind == "json" || *kind == "identity" {
                    audit_value(value)
                } else {
                    value.clone()
                },
            );
        } else {
            columns.insert((*name).into(), Value::Null);
            invalid_fields.insert((*name).into(), audit_value(value));
        }
    }
    let mut unknown = Map::new();
    for (key, value) in payload {
        let spec_name = specs.iter().any(|(name, _)| name == key);
        if !spec_name && !child_keys.contains(&key.as_str()) && key != "audit_evidence" {
            unknown.insert(key.clone(), audit_value(value));
        }
    }
    let base = payload.get("audit_evidence").and_then(Value::as_object);
    let mut evidence = base.cloned().unwrap_or_default();
    if !invalid_fields.is_empty() {
        let mut previous = match evidence.get("invalid_fields") {
            None => Map::new(),
            Some(value) => value
                .as_object()
                .cloned()
                .ok_or("Invalid audit evidence fields")?,
        };
        for (key, value) in invalid_fields {
            previous.insert(key, value);
        }
        evidence.insert("invalid_fields".into(), Value::Object(previous));
    }
    if !unknown.is_empty() {
        let mut previous = match evidence.get("unrecognized_fields") {
            None => Map::new(),
            Some(value) => value
                .as_object()
                .cloned()
                .ok_or("Invalid unrecognized audit fields")?,
        };
        for (key, value) in unknown {
            previous.insert(key, value);
        }
        evidence.insert("unrecognized_fields".into(), Value::Object(previous));
    }
    Ok((columns, evidence))
}

// ---------------------------------------------------------------------------
// 输入净化与目标归一 (inputs.py)
// ---------------------------------------------------------------------------

fn is_truthy(value: &Value) -> bool {
    match value {
        Value::Null | Value::Bool(false) => false,
        Value::Bool(true) => true,
        Value::Number(number) => number.as_f64().is_some_and(|v| v != 0.0),
        Value::String(text) => !text.is_empty(),
        Value::Array(items) => !items.is_empty(),
        Value::Object(map) => !map.is_empty(),
    }
}

fn sanitize_mapping(payload: &Map<String, Value>) -> Map<String, Value> {
    let mut sanitized = payload.clone();
    sanitized.remove("account_config");
    sanitized.remove("execution_notification_code");
    if sanitized.get("feishu_key").is_some_and(is_truthy) {
        sanitized.insert("feishu_key".into(), json!("***"));
    }
    sanitized
}

fn sanitize_payload(payload: &Value) -> Result<Value, String> {
    match payload {
        Value::Object(map) => Ok(Value::Object(sanitize_mapping(map))),
        Value::Null | Value::String(_) | Value::Bool(_) | Value::Number(_) => Ok(payload.clone()),
        Value::Array(items) if items.is_empty() => Ok(payload.clone()),
        Value::Array(_) => Err("Unsupported execution input document root type".into()),
    }
}

/// 字段别名归入业务角色,冲突值单独保留。
pub(super) fn normalize_targets(payload: &Value) -> Map<String, Value> {
    let mut result = payload.as_object().cloned().unwrap_or_default();
    let mut conflicts = Map::new();
    for (alias, role) in TARGET_ALIASES {
        let Some(value) = result.get(*alias).cloned() else {
            continue;
        };
        result.remove(*alias);
        match result.get(*role) {
            None => {
                result.insert((*role).into(), value);
            }
            Some(existing) if !py_eq(existing, &value) => {
                conflicts.insert((*alias).into(), value);
            }
            _ => {}
        }
    }
    if !conflicts.is_empty() {
        result.insert("target_conflicts".into(), Value::Object(conflicts));
    }
    normalize_plan_input(&mut result);
    result
}

fn normalize_plan_input(result: &mut Map<String, Value>) {
    let has_plan = result.contains_key("plan");
    let plan = result.remove("plan");
    let plan_object = plan.as_ref().is_some_and(|plan| plan.is_object());
    if has_plan && !plan_object {
        result.insert("plan_evidence".into(), plan.expect("checked"));
    } else if let Some(plan_map) = plan.as_ref().and_then(Value::as_object) {
        let mut remainder = Map::new();
        for (key, value) in plan_map {
            if !["quantities", "units", "availability"].contains(&key.as_str()) {
                remainder.insert(key.clone(), value.clone());
            }
        }
        if !remainder.is_empty() {
            result.insert("plan_evidence".into(), Value::Object(remainder));
        }
        if let Some(quantities) = plan_map.get("quantities") {
            result
                .entry("quantities".to_owned())
                .or_insert_with(|| quantities.clone());
        }
        if let Some(units) = plan_map.get("units") {
            result
                .entry("target_units".to_owned())
                .or_insert_with(|| units.clone());
        }
    }
    let mut conflicts = Map::new();
    for alias in QUANTITY_ALIASES {
        let Some(value) = result.get(*alias).cloned() else {
            continue;
        };
        result.remove(*alias);
        match result.get("quantities") {
            None => {
                result.insert("quantities".into(), value);
            }
            Some(existing) if !py_eq(existing, &value) => {
                conflicts.insert((*alias).into(), value);
            }
            _ => {}
        }
    }
    if !conflicts.is_empty() {
        result.insert("quantity_conflicts".into(), Value::Object(conflicts));
    }
}

fn collection_state(body: &Map<String, Value>, key: &str, expects_object: bool) -> &'static str {
    let invalid = body
        .get("audit_evidence")
        .and_then(Value::as_object)
        .and_then(|evidence| evidence.get("invalid_collections"))
        .and_then(Value::as_object);
    if invalid.is_some_and(|invalid| invalid.contains_key(key)) {
        return "invalid";
    }
    let Some(value) = body.get(key) else {
        return "absent";
    };
    if value.is_null() {
        return "null";
    }
    let matches = if expects_object {
        value.is_object()
    } else {
        value.is_array()
    };
    if matches {
        "value"
    } else {
        "invalid"
    }
}

// ---------------------------------------------------------------------------
// 输入编码 (inputs.py encode_inputs)
// ---------------------------------------------------------------------------

/// 直接映射冻结配置、业务目标和风险名单,不保存字段排列和数值表示。
pub(super) fn encode_inputs(
    input_id: &str,
    payload: &Value,
    account_id: i64,
    execution_id: Option<&str>,
    created_at: &str,
    owner_execution_id: Option<&str>,
) -> Result<FactBatch, String> {
    let payload = sanitize_payload(payload)?;
    let body = if payload.is_object() {
        normalize_targets(&payload)
    } else {
        Map::new()
    };
    let (columns, mut evidence) = store_columns(&body, INPUT_FIELDS, CHILD_FIELDS)?;
    let mut root = Map::new();
    root.insert("id".into(), json!(input_id));
    root.insert("account_id".into(), json!(account_id));
    root.insert(
        "execution_id".into(),
        execution_id.map(|id| json!(id)).unwrap_or(Value::Null),
    );
    root.insert(
        "owner_execution_id".into(),
        owner_execution_id
            .map(|id| json!(id))
            .unwrap_or(Value::Null),
    );
    root.insert("created_at".into(), json!(created_at));
    root.insert(
        "root_kind".into(),
        match &payload {
            Value::Object(_) => json!("object"),
            Value::Null => json!("null"),
            _ => json!("invalid"),
        },
    );
    root.insert(
        "root_value".into(),
        if payload.is_object() {
            Value::Null
        } else {
            audit_value(&payload)
        },
    );
    for (name, value) in columns {
        root.insert(name, value);
    }
    for field in SIZING_FIELDS {
        root.insert((*field).into(), Value::Null);
    }
    let mut targets: Vec<Map<String, Value>> = Vec::new();
    let mut symbols: Vec<Map<String, Value>> = Vec::new();
    let mut invalid_collections: Map<String, Value> = evidence
        .get("invalid_collections")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let mut invalid_targets = Map::new();
    let mut invalid_symbols = Map::new();
    for role in CHILD_FIELDS {
        if PLAN_FIELDS.contains(role) {
            continue;
        }
        let expects_object = TARGET_ROLES.contains(role) || *role == "sizing_context";
        let state = collection_state(&body, role, expects_object);
        root.insert(format!("{role}_state"), json!(state));
        if *role != "sizing_context" {
            let value = body.get(*role);
            let count = match value {
                Some(value @ (Value::Object(_) | Value::Array(_))) if state == "value" => {
                    match value {
                        Value::Object(map) => json!(map.len()),
                        Value::Array(items) => json!(items.len()),
                        _ => unreachable!(),
                    }
                }
                _ => Value::Null,
            };
            root.insert(format!("{role}_count"), count);
        }
        if state == "invalid" {
            let value = body.get(*role).unwrap_or(&Value::Null);
            let replace = !(invalid_collections.contains_key(*role) && value.is_null());
            if replace {
                invalid_collections.insert((*role).into(), audit_value(value));
            }
        }
        if state != "value" {
            continue;
        }
        let value = body.get(*role).expect("state checked");
        if *role == "sizing_context" {
            let object = value.as_object().expect("state checked");
            let (sizing, sizing_evidence) = store_columns(object, SIZING_SPECS, &[])?;
            for (name, column) in sizing {
                root.insert(name, column);
            }
            if !sizing_evidence.is_empty() {
                evidence.insert("sizing".into(), Value::Object(sizing_evidence));
            }
        } else if TARGET_ROLES.contains(role) {
            let weights = value.as_object().expect("state checked");
            let (rows, issues) = target_rows(input_id, role, weights);
            targets.extend(rows);
            if !issues.is_empty() {
                invalid_targets.insert((*role).into(), Value::Object(issues));
            }
        } else {
            let values = value.as_array().expect("state checked");
            symbols.extend(risk_rows(input_id, role, values, &mut invalid_symbols));
        }
    }
    if !invalid_collections.is_empty() {
        evidence.insert(
            "invalid_collections".into(),
            Value::Object(invalid_collections),
        );
    }
    if !invalid_targets.is_empty() {
        evidence.insert(
            "invalid_target_values".into(),
            Value::Object(invalid_targets),
        );
    }
    if !invalid_symbols.is_empty() {
        evidence.insert(
            "invalid_symbol_values".into(),
            Value::Object(invalid_symbols),
        );
    }
    let plans = encode_plan(input_id, &body, &mut root, &mut evidence)?;
    for role in COUNTED_ROLES {
        root.entry(format!("{role}_count")).or_insert(Value::Null);
    }
    root.insert("audit_evidence".into(), Value::Object(evidence));
    let mut batch = FactBatch::new();
    batch.push("execution_input", vec![root]);
    batch.push("execution_input_target", targets);
    batch.push("execution_input_symbol", symbols);
    batch.push("execution_input_plan", plans);
    Ok(batch)
}

fn target_rows(
    input_id: &str,
    role: &str,
    weights: &Map<String, Value>,
) -> (Vec<Map<String, Value>>, Map<String, Value>) {
    let mut rows = Vec::new();
    let mut issues = Map::new();
    for (symbol, weight) in weights {
        let mut payload = Map::new();
        payload.insert("value".into(), weight.clone());
        let (typed, evidence) =
            store_columns(&payload, &[("value", "number")], &[]).expect("number spec");
        let mut row = Map::new();
        row.insert("input_id".into(), json!(input_id));
        row.insert("role".into(), json!(role));
        row.insert("map_key".into(), json!(symbol));
        for (name, value) in typed {
            row.insert(name, value);
        }
        row.insert("audit_evidence".into(), Value::Object(evidence.clone()));
        if !evidence.is_empty() {
            issues.insert(symbol.clone(), Value::Object(evidence));
        }
        rows.push(row);
    }
    (rows, issues)
}

fn risk_rows(
    input_id: &str,
    role: &str,
    values: &[Value],
    invalid_symbols: &mut Map<String, Value>,
) -> Vec<Map<String, Value>> {
    let mut rows = Vec::new();
    for (ordinal, symbol) in values.iter().enumerate() {
        let mut payload = Map::new();
        payload.insert("value".into(), symbol.clone());
        let (typed, evidence) =
            store_columns(&payload, &[("value", "text")], &[]).expect("text spec");
        if let Some(fields) = evidence.get("invalid_fields").and_then(Value::as_object) {
            invalid_symbols
                .entry(role.to_string())
                .or_insert_with(|| Value::Object(Map::new()))
                .as_object_mut()
                .expect("object")
                .insert(
                    ordinal.to_string(),
                    fields.get("value").cloned().unwrap_or(Value::Null),
                );
        }
        let mut row = Map::new();
        row.insert("input_id".into(), json!(input_id));
        row.insert("role".into(), json!(role));
        row.insert("ordinal".into(), json!(ordinal));
        for (name, value) in typed {
            row.insert(name, value);
        }
        row.insert("audit_evidence".into(), Value::Object(evidence));
        rows.push(row);
    }
    rows
}

fn encode_plan(
    input_id: &str,
    body: &Map<String, Value>,
    root: &mut Map<String, Value>,
    evidence: &mut Map<String, Value>,
) -> Result<Vec<Map<String, Value>>, String> {
    let mut quantities: Map<String, Value> = Map::new();
    let mut units: Map<String, Value> = Map::new();
    for role in PLAN_FIELDS {
        let state = collection_state(body, role, true);
        root.insert(format!("{role}_state"), json!(state));
        let value = body.get(*role);
        root.insert(
            format!("{role}_count"),
            match value {
                Some(Value::Object(map)) => json!(map.len()),
                _ => Value::Null,
            },
        );
        let target = if *role == "quantities" {
            &mut quantities
        } else {
            &mut units
        };
        if state == "value" {
            *target = value
                .and_then(Value::as_object)
                .cloned()
                .unwrap_or_default();
        }
        if state == "invalid" {
            let mut issues = match evidence.get("invalid_collections") {
                None => Map::new(),
                Some(value) => value
                    .as_object()
                    .cloned()
                    .ok_or("Invalid input collection evidence")?,
            };
            let value = value.cloned().unwrap_or(Value::Null);
            if !issues.contains_key(*role) {
                issues.insert((*role).into(), audit_value(&value));
            }
            evidence.insert("invalid_collections".into(), Value::Object(issues));
        }
    }

    let mut merged: Vec<&String> = quantities.keys().chain(units.keys()).collect();
    merged.sort();
    merged.dedup();
    let mut rows = Vec::new();
    for symbol in merged {
        let mut payload = Map::new();
        if let Some(quantity) = quantities.get(symbol) {
            payload.insert("quantity".into(), quantity.clone());
        }
        if let Some(unit) = units.get(symbol) {
            payload.insert("units".into(), unit.clone());
        }
        if let Some(existing) = evidence
            .get("invalid_plan_values")
            .and_then(Value::as_object)
            .filter(|values| values.contains_key(symbol))
        {
            payload.insert(
                "audit_evidence".into(),
                existing.get(symbol).cloned().expect("checked"),
            );
        }
        let (columns, issues) = store_columns(
            &payload,
            &[("quantity", "number"), ("units", "number")],
            &[],
        )?;
        if !issues.is_empty() {
            let mut invalid = match evidence.get("invalid_plan_values") {
                None => Map::new(),
                Some(value) => value
                    .as_object()
                    .cloned()
                    .ok_or("Invalid quantity plan evidence")?,
            };
            invalid.insert(symbol.clone(), Value::Object(issues.clone()));
            evidence.insert("invalid_plan_values".into(), Value::Object(invalid));
        }
        let invalid_names = issues
            .get("invalid_fields")
            .and_then(Value::as_object)
            .map(|fields| fields.keys().cloned().collect::<Vec<_>>())
            .unwrap_or_default();
        let mut row = Map::new();
        row.insert("input_id".into(), json!(input_id));
        row.insert("symbol".into(), json!(symbol));
        for key in ["quantity", "units"] {
            row.insert(key.into(), columns.get(key).cloned().unwrap_or(Value::Null));
            let state = if !payload.contains_key(key) {
                "absent"
            } else if invalid_names.iter().any(|name| name == key) {
                "invalid"
            } else if payload.get(key).is_some_and(Value::is_null) {
                "null"
            } else if columns.get(key).is_some_and(|value| !value.is_null()) {
                "value"
            } else {
                "invalid"
            };
            row.insert(format!("{key}_state"), json!(state));
        }
        row.insert("audit_evidence".into(), Value::Object(issues));
        rows.push(row);
    }
    Ok(rows)
}

/// 比较冻结输入使用同一业务口径:编码后立即按读端口径解码。
pub(super) fn canonical_input(payload: &Value) -> Value {
    encode_decode(payload).unwrap_or(Value::Null)
}

fn encode_decode(payload: &Value) -> Result<Value, String> {
    let batch = encode_inputs("", payload, 0, None, "", None)?;
    let statements = batch.statements();
    let head = statements[0].1[0].clone();
    let targets = statements[1].1.clone();
    let symbols = statements[2].1.clone();
    let plans = statements[3].1.clone();
    business_detail::decode_input(&head, &targets, &symbols, &plans)
}

// ---------------------------------------------------------------------------
// 资产观测编码 (assets.py)
// ---------------------------------------------------------------------------

fn kind_of(value: &Value) -> &'static str {
    if value.is_object() {
        "object"
    } else if value.is_null() {
        "null"
    } else {
        "invalid"
    }
}

fn amount_state(
    payload: &Map<String, Value>,
    key: &str,
    columns: &Map<String, Value>,
    label: &str,
) -> &'static str {
    if let Some(state) = payload
        .get("availability")
        .and_then(Value::as_object)
        .and_then(|availability| availability.get(label))
        .and_then(Value::as_str)
    {
        if let Some(state) = ["absent", "null", "value", "invalid"]
            .iter()
            .find(|valid| **valid == state)
        {
            return match *state {
                "absent" => "absent",
                "null" => "null",
                "value" => "value",
                _ => "invalid",
            };
        }
    }
    let Some(value) = payload.get(key) else {
        return "absent";
    };
    if value.is_null() {
        return "null";
    }
    if columns.get(key).is_some_and(|column| !column.is_null()) {
        "value"
    } else {
        "invalid"
    }
}

const DEGRADED_SOURCES: &[&str] = &["assumed", "error", "unavailable"];

fn worker_placeholder(result: &Map<String, Value>) -> bool {
    let inputs_ok = match result.get("inputs") {
        None | Some(Value::Null) => true,
        Some(Value::Object(map)) => map.is_empty(),
        _ => false,
    };
    result.get("status").and_then(Value::as_str) == Some("FAILED")
        && result
            .get("extra")
            .and_then(Value::as_object)
            .is_some_and(|extra| extra.get("worker_error").is_some_and(Value::is_object))
        && inputs_ok
        && matches!(result.get("execution_time"), Some(Value::Number(number)) if number.as_f64() == Some(0.0))
}

fn blocked_placeholder(result: &Map<String, Value>) -> bool {
    result.get("status").and_then(Value::as_str) == Some("BLOCKED")
        && result.get("error").and_then(Value::as_str) == Some("当前不在交易时间")
        && py_eq(
            result.get("memory").unwrap_or(&Value::Null),
            &json!({"message": "当前不在交易时间"}),
        )
        && result.get("inputs").is_some_and(Value::is_object)
        && matches!(result.get("execution_time"), Some(Value::Number(_)))
}

fn legacy_placeholder(result: &Map<String, Value>) -> bool {
    let Some(assets) = result.get("account_assets").and_then(Value::as_object) else {
        return false;
    };
    if assets
        .get("source")
        .and_then(Value::as_str)
        .unwrap_or("real")
        != "real"
    {
        return false;
    }
    if !["available_cash", "total_asset", "market_value"].iter().all(|key| {
        matches!(assets.get(*key), Some(Value::Number(number)) if number.as_f64() == Some(0.0))
    }) {
        return false;
    }
    if !py_eq(assets.get("positions").unwrap_or(&Value::Null), &json!([]))
        || !py_eq(assets.get("extra").unwrap_or(&Value::Null), &json!({}))
        || !py_eq(
            result.get("symbol_results").unwrap_or(&Value::Null),
            &json!({}),
        )
    {
        return false;
    }
    worker_placeholder(result) || blocked_placeholder(result)
}

fn is_asset_observation(assets: &Map<String, Value>, result: Option<&Map<String, Value>>) -> bool {
    let degraded = assets
        .get("source")
        .and_then(Value::as_str)
        .is_some_and(|source| DEGRADED_SOURCES.contains(&source));
    if degraded {
        return false;
    }
    let observed = assets
        .get("total_asset")
        .is_some_and(|value| matches!(value, Value::Number(number) if number.as_f64().is_some_and(|v| v.abs() <= f64::MAX)));
    observed && result.is_none_or(|result| !legacy_placeholder(result))
}

struct Observation {
    root: Map<String, Value>,
    positions: Vec<Map<String, Value>>,
}

fn encode_observation(
    value: &Value,
    asset_id: &str,
    path: &str,
    scope: &str,
    input_id: Option<&str>,
    result: Option<&Map<String, Value>>,
) -> Observation {
    let kind = kind_of(value);
    let payload = value.as_object().cloned().unwrap_or_default();
    let (columns, metadata) = store_columns(&payload, ASSET_FIELDS, &["positions", "availability"])
        .expect("asset fields");
    let positions_value = payload.get("positions");
    let positions_kind = if !payload.contains_key("positions") {
        "absent"
    } else if positions_value.is_some_and(Value::is_array) {
        "array"
    } else {
        kind_of(positions_value.expect("checked"))
    };
    let mut root = Map::new();
    root.insert("asset_id".into(), json!(asset_id));
    root.insert("record_id".into(), Value::Null);
    root.insert(
        "input_id".into(),
        input_id.map(|id| json!(id)).unwrap_or(Value::Null),
    );
    root.insert("account_snapshot_id".into(), Value::Null);
    root.insert("path".into(), json!(path));
    root.insert("scope".into(), json!(scope));
    root.insert("ordinal".into(), json!(0));
    root.insert("map_key".into(), Value::Null);
    root.insert("payload_kind".into(), json!(kind));
    root.insert(
        "payload_value".into(),
        if kind == "object" {
            Value::Null
        } else {
            audit_value(value)
        },
    );
    root.insert("audit_evidence".into(), Value::Object(metadata));
    root.insert(
        "cash_state".into(),
        json!(amount_state(&payload, "available_cash", &columns, "cash")),
    );
    root.insert(
        "equity_state".into(),
        json!(amount_state(&payload, "total_asset", &columns, "equity")),
    );
    root.insert(
        "market_state".into(),
        json!(amount_state(
            &payload,
            "market_value",
            &columns,
            "market_value"
        )),
    );
    root.insert("positions_kind".into(), json!(positions_kind));
    root.insert(
        "positions_count".into(),
        positions_value
            .and_then(Value::as_array)
            .map(|items| json!(items.len()))
            .unwrap_or(Value::Null),
    );
    root.insert(
        "positions_value".into(),
        if ["absent", "array"].contains(&positions_kind) {
            Value::Null
        } else {
            audit_value(positions_value.expect("checked"))
        },
    );
    root.insert(
        "observation_valid".into(),
        json!(is_asset_observation(&payload, result)),
    );
    for (name, value) in columns {
        root.insert(name, value);
    }
    let positions = position_rows(
        asset_id,
        positions_value.and_then(Value::as_array).map(Vec::as_slice),
    );
    Observation { root, positions }
}

fn position_rows(asset_id: &str, positions: Option<&[Value]>) -> Vec<Map<String, Value>> {
    let mut rows = Vec::new();
    for (ordinal, position) in positions.unwrap_or_default().iter().enumerate() {
        let kind = kind_of(position);
        let payload = position.as_object().cloned().unwrap_or_default();
        let (columns, metadata) =
            store_columns(&payload, POSITION_FIELDS, &["availability"]).expect("position fields");
        let mut row = Map::new();
        row.insert("asset_id".into(), json!(asset_id));
        row.insert("ordinal".into(), json!(ordinal));
        for name in POSITION_AMOUNTS {
            row.insert(
                format!("{name}_state"),
                json!(amount_state(&payload, name, &columns, name)),
            );
        }
        row.insert("payload_kind".into(), json!(kind));
        row.insert(
            "payload_value".into(),
            if kind == "object" {
                Value::Null
            } else {
                audit_value(position)
            },
        );
        row.insert("audit_evidence".into(), Value::Object(metadata));
        for (name, value) in columns {
            row.insert(name, value);
        }
        rows.push(row);
    }
    rows
}

/// 保存下单前资产;参数通过 account_assets 键明确区分缺失与空值。
pub(super) fn encode_input_assets(input_id: &str, payload: &Value) -> FactBatch {
    let mut batch = FactBatch::new();
    let Some(assets) = payload.get("account_assets") else {
        return batch;
    };
    let observation = encode_observation(
        assets,
        &format!("input:{input_id}:/before_account_assets"),
        "/before_account_assets",
        "before",
        Some(input_id),
        None,
    );
    batch.push("business_asset_snapshot", vec![observation.root]);
    batch.push("business_asset_position", observation.positions);
    batch
}

/// 在 worker 返回前保存执行后观测,终态记录随后只绑定同一事实。
pub(super) fn encode_input_after_assets(input_id: &str, payload: &Map<String, Value>) -> FactBatch {
    let mut batch = FactBatch::new();
    let Some(assets) = payload.get("account_assets") else {
        return batch;
    };
    let observation = encode_observation(
        assets,
        &format!("input:{input_id}:/after_account_assets"),
        "/after_account_assets",
        "after_pending",
        Some(input_id),
        Some(payload),
    );
    batch.push("business_asset_snapshot", vec![observation.root]);
    batch.push("business_asset_position", observation.positions);
    batch
}

/// 重放比较用:把传入观测按同一编码-解码口径转成业务值。
pub(super) fn business_asset_value(incoming: &Value) -> Value {
    let observation =
        encode_observation(incoming, "compare", "/account_assets", "after", None, None);
    crate::assets::observation_value(&observation.root, &observation.positions)
        .unwrap_or(Value::Null)
}

// ---------------------------------------------------------------------------
// SQL 绑定:按列名声明类型转换,JSON 列序列化为文本。
// ---------------------------------------------------------------------------

fn is_json_column(column: &str) -> bool {
    matches!(
        column,
        "audit_evidence"
            | "root_value"
            | "payload_value"
            | "positions_value"
            | "algorithm"
            | "symbol_algorithms"
            | "trade_rules"
            | "futures_close_policy"
            | "feishu_key"
            | "feishu_account"
            | "extra"
    )
}

pub(super) fn sql_value(column: &str, value: Value) -> Result<rusqlite::types::Value, String> {
    if value.is_null() {
        return Ok(rusqlite::types::Value::Null);
    }
    if is_json_column(column) {
        return Ok(rusqlite::types::Value::Text(
            serde_json::to_string(&value).map_err(|error| error.to_string())?,
        ));
    }
    Ok(match value {
        Value::Bool(flag) => rusqlite::types::Value::Integer(flag as i64),
        Value::Number(number) => {
            if let Some(integer) = number.as_i64() {
                rusqlite::types::Value::Integer(integer)
            } else {
                rusqlite::types::Value::Real(number.as_f64().ok_or("invalid number")?)
            }
        }
        Value::String(text) => rusqlite::types::Value::Text(text),
        other => {
            return Err(format!(
                "unexpected structured value in column {column}: {other}"
            ))
        }
    })
}
