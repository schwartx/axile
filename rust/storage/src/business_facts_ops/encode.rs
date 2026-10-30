//! Python 编码契约的 Rust 移植:列投影、稀疏审计证据、输入与资产事实编码。
//!
//! 错误消息与 Python ValueError 保持一致;数值等价比较沿用 Python 语义
//! (1 == 1.0,True == 1),重放判定依赖该语义。JSON 对象键天然是字符串,
//! 因此 Python 中针对非字符串字典键的防御检查在此不可达。

use serde_json::{json, Map, Value};

use crate::business_detail;

pub(crate) type Rows = Vec<Map<String, Value>>;
pub(crate) type InputRows = (Map<String, Value>, Rows, Rows, Rows);

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
    ("update_time", "time"),
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
        Value::Number(number) => number
            .as_f64()
            .or_else(|| number.to_string().parse::<f64>().ok()),
        Value::Bool(flag) => Some(if *flag { 1.0 } else { 0.0 }),
        _ => None,
    }
}

fn integer_text(value: &Value) -> Option<String> {
    match value {
        Value::Number(number) => {
            let raw = number.to_string();
            let digits = raw.strip_prefix('-').unwrap_or(&raw);
            (!digits.is_empty() && digits.bytes().all(|byte| byte.is_ascii_digit())).then(|| {
                if raw == "-0" {
                    "0".into()
                } else {
                    raw
                }
            })
        }
        Value::Bool(flag) => Some(if *flag { "1" } else { "0" }.into()),
        _ => None,
    }
}

fn integer_matches_float(integer: &str, float: f64) -> bool {
    float.is_finite()
        && float.fract() == 0.0
        && if float == 0.0 {
            integer == "0"
        } else {
            format!("{float:.0}") == integer
        }
}

/// Python `left == right`:布尔与数字互相按数值相等,数字跨 int/float 相等。
pub(crate) fn py_eq(left: &Value, right: &Value) -> bool {
    match (left, right) {
        (Value::Null, Value::Null) => true,
        (Value::Bool(_), Value::Bool(_)) => left == right,
        (Value::Bool(_) | Value::Number(_), Value::Bool(_) | Value::Number(_)) => {
            match (integer_text(left), integer_text(right)) {
                // 双方可精确表示为整数时按整数比较,避开 f64 精度丢失。
                (Some(l), Some(r)) => l == r,
                (Some(integer), None) => {
                    as_number(right).is_some_and(|float| integer_matches_float(&integer, float))
                }
                (None, Some(integer)) => {
                    as_number(left).is_some_and(|float| integer_matches_float(&integer, float))
                }
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

pub(crate) fn valid(value: &Value, kind: &str) -> bool {
    if value.is_null() {
        return true;
    }
    match kind {
        "json" => true,
        "identity" => value.is_string() || value.is_number() && integer_text(value).is_some(),
        "text" => matches!(value, Value::String(_)),
        "time" => value
            .as_str()
            .is_some_and(|value| crate::time_us::parse(value).is_ok()),
        "boolean" => matches!(value, Value::Bool(_)),
        "integer" => matches!(value, Value::Number(n) if n.is_i64()),
        // SQLite REAL 不能精确保留的大整数转为审计证据。
        _ => match value {
            Value::Number(number) => number.as_f64().is_some_and(|float| {
                integer_text(value).map_or(true, |integer| integer_matches_float(&integer, float))
            }),
            _ => false,
        },
    }
}

pub(crate) fn audit_value(value: &Value) -> Value {
    match value {
        Value::Object(map) => Value::Object(
            map.iter()
                .map(|(k, v)| (k.clone(), audit_value(v)))
                .collect(),
        ),
        Value::Array(items) => Value::Array(items.iter().map(audit_value).collect()),
        Value::Number(number) if number.as_i64() == Some(0) => json!(0),
        Value::Number(number) if integer_text(value).is_none() => {
            match number.as_f64().and_then(serde_json::Number::from_f64) {
                Some(number) => Value::Number(number),
                None => {
                    let raw = number.to_string();
                    json!({"invalid_number": if raw == "NaN" { "nan" } else if raw.starts_with('-') { "-inf" } else { "inf" }})
                }
            }
        }
        other => other.clone(),
    }
}

pub(crate) fn join_pointer(path: &str, key: &str) -> String {
    format!("{path}/{}", key.replace('~', "~0").replace('/', "~1"))
}

pub(crate) type StoreResult = Result<(Map<String, Value>, Map<String, Value>), String>;

pub(crate) fn store_columns(
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
    let null_fields: Vec<_> = specs
        .iter()
        .filter(|(name, _)| payload.get(*name).is_some_and(Value::is_null))
        .map(|(name, _)| Value::from(*name))
        .collect();
    evidence.remove("explicit_null_fields");
    if !null_fields.is_empty() {
        evidence.insert("explicit_null_fields".into(), Value::Array(null_fields));
    }
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

pub(crate) fn is_truthy(value: &Value) -> bool {
    match value {
        Value::Null | Value::Bool(false) => false,
        Value::Bool(true) => true,
        Value::Number(number) => number.as_f64().map_or(true, |value| value != 0.0),
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

pub(crate) fn sanitize_payload(payload: &Value) -> Result<Value, String> {
    match payload {
        Value::Object(map) => Ok(Value::Object(sanitize_mapping(map))),
        Value::Null | Value::String(_) | Value::Bool(_) | Value::Number(_) => Ok(payload.clone()),
        Value::Array(items) if items.is_empty() => Ok(payload.clone()),
        Value::Array(_) => Err("Unsupported execution input document root type".into()),
    }
}

/// 字段别名归入业务角色,冲突值单独保留。
pub(crate) fn normalize_targets(payload: &Value) -> Map<String, Value> {
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

/// 历史附件按原字段名投影，别名值仍来自规范事实或已有冲突证据。
pub(crate) fn target_field<'a>(payload: &'a Map<String, Value>, key: &str) -> Option<&'a Value> {
    payload.get(key).or_else(|| {
        let (_, role) = TARGET_ALIASES.iter().find(|(alias, _)| *alias == key)?;
        payload
            .get("audit_evidence")
            .and_then(|evidence| evidence.get("unrecognized_fields"))
            .and_then(|fields| fields.get("target_conflicts"))
            .and_then(|conflicts| conflicts.get(key))
            .or_else(|| payload.get(*role))
    })
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
pub(crate) fn encode_inputs(
    input_id: &str,
    payload: &Value,
    account_id: i64,
    execution_id: Option<&str>,
    created_at: &str,
    owner_execution_id: Option<&str>,
) -> Result<InputRows, String> {
    let payload = sanitize_payload(payload)?;
    if execution_id.is_some() && owner_execution_id.is_some() && execution_id != owner_execution_id
    {
        return Err("Execution input primary identity and owner differ".into());
    }
    let owner_execution_id = owner_execution_id.or(execution_id);
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
    Ok((root, targets, symbols, plans))
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
pub(crate) fn canonical_input(payload: &Value) -> Result<Value, String> {
    encode_decode(payload)
}

fn encode_decode(payload: &Value) -> Result<Value, String> {
    let (head, targets, symbols, plans) = encode_inputs("", payload, 0, None, "", None)?;
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
        && matches!(result.get("execution_time"), Some(Value::Number(number)) if number.as_f64().is_some_and(|value| value >= 0.0))
}

fn legacy_placeholder(result: &Map<String, Value>) -> bool {
    let Some(assets) = result.get("account_assets").and_then(Value::as_object) else {
        return false;
    };
    if assets
        .get("source")
        .is_some_and(|source| source.as_str() != Some("real"))
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

pub(crate) struct Observation {
    pub(crate) root: Map<String, Value>,
    pub(crate) positions: Vec<Map<String, Value>>,
}

pub(crate) fn encode_observation(
    value: &Value,
    asset_id: &str,
    path: &str,
    scope: &str,
    input_id: Option<&str>,
    result: Option<&Map<String, Value>>,
) -> Result<Observation, String> {
    let kind = kind_of(value);
    let payload = value.as_object().cloned().unwrap_or_default();
    let (columns, metadata) =
        store_columns(&payload, ASSET_FIELDS, &["positions", "availability"])?;
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
    )?;
    Ok(Observation { root, positions })
}

fn position_rows(
    asset_id: &str,
    positions: Option<&[Value]>,
) -> Result<Vec<Map<String, Value>>, String> {
    let mut rows = Vec::new();
    for (ordinal, position) in positions.unwrap_or_default().iter().enumerate() {
        let kind = kind_of(position);
        let payload = position.as_object().cloned().unwrap_or_default();
        let (columns, metadata) = store_columns(&payload, POSITION_FIELDS, &["availability"])?;
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
    Ok(rows)
}

/// 保存下单前资产;参数通过 account_assets 键明确区分缺失与空值。
pub(crate) fn encode_input_assets(
    input_id: &str,
    payload: &Value,
) -> Result<Option<Observation>, String> {
    let Some(assets) = payload.get("account_assets") else {
        return Ok(None);
    };
    let observation = encode_observation(
        assets,
        &format!("input:{input_id}:/before_account_assets"),
        "/before_account_assets",
        "before",
        Some(input_id),
        None,
    )?;
    Ok(Some(observation))
}

/// 在 worker 返回前保存执行后观测,终态记录随后只绑定同一事实。
pub(crate) fn encode_input_after_assets(
    input_id: &str,
    payload: &Map<String, Value>,
) -> Result<Option<Observation>, String> {
    let Some(assets) = payload.get("account_assets") else {
        return Ok(None);
    };
    let observation = encode_observation(
        assets,
        &format!("input:{input_id}:/after_account_assets"),
        "/after_account_assets",
        "after_pending",
        Some(input_id),
        Some(payload),
    )?;
    Ok(Some(observation))
}

/// 手工快照与执行观测复用同一列编码,缺失资产不能制造空观测。
pub(crate) fn encode_snapshot_assets(
    snapshot_id: i64,
    payload: &Value,
    result: Option<&Map<String, Value>>,
) -> Result<Observation, String> {
    let assets = payload
        .get("account_assets")
        .ok_or("账户资产观测缺少 account_assets")?;
    let mut observation = encode_observation(
        assets,
        &format!("snapshot:{snapshot_id}:/account_assets"),
        "/account_assets",
        "manual",
        None,
        result,
    )?;
    observation
        .root
        .insert("account_snapshot_id".into(), json!(snapshot_id));
    Ok(observation)
}

/// 重放比较用:把传入观测按同一编码-解码口径转成业务值。
pub(crate) fn business_asset_value(incoming: &Value) -> Result<Value, String> {
    encode_observation(incoming, "compare", "/account_assets", "after", None, None).and_then(
        |observation| crate::assets::observation_value(&observation.root, &observation.positions),
    )
}

// ---------------------------------------------------------------------------
// JSON fields are explicitly serialized by each table's fixed SQL binding.
// ---------------------------------------------------------------------------

pub(crate) fn json_text(
    value: Option<&Value>,
) -> Result<Option<String>, crate::error::AnalysisError> {
    value
        .map(serde_json::to_string)
        .transpose()
        .map_err(|error| crate::error::AnalysisError::caused_by("BUSINESS_FACTS", error))
}

#[cfg(test)]
pub(super) fn frozen_cases(kind: &str) -> Vec<Value> {
    let fixture: Value =
        serde_json::from_str(include_str!("testdata/head_symbols_inputs_v0037.json")).unwrap();
    fixture["cases"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|case| case["kind"] == kind)
        .cloned()
        .collect()
}

#[cfg(test)]
pub(super) fn input_value((head, targets, symbols, plans): &InputRows) -> Value {
    serde_json::json!({"execution_input":[head],"execution_input_target":targets,"execution_input_symbol":symbols,"execution_input_plan":plans})
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn invalid_scalar_time_is_evidence_and_json_time_stays_opaque() {
        let payload =
            json!({"update_time":"native-unknown", "extra":{"update_time":"native-unknown"}});
        let (columns, evidence) =
            store_columns(payload.as_object().unwrap(), ASSET_FIELDS, &[]).unwrap();
        assert_eq!(columns["update_time"], Value::Null);
        assert_eq!(evidence["invalid_fields"]["update_time"], "native-unknown");
        assert_eq!(columns["extra"], payload["extra"]);
    }

    #[test]
    fn frozen_0037_input_fixtures_match_python_columns_and_evidence() {
        for case in frozen_cases("inputs") {
            let encoded = encode_inputs(
                "frozen",
                &case["payload"],
                3,
                case.get("execution_id").and_then(Value::as_str),
                "historical-time",
                case.get("owner_execution_id").and_then(Value::as_str),
            );
            if let Some(error) = case.get("error") {
                assert_eq!(
                    encoded.err().unwrap(),
                    error.as_str().unwrap(),
                    "{}",
                    case["name"]
                );
            } else {
                assert_eq!(
                    input_value(&encoded.unwrap()),
                    case["expected"],
                    "{}",
                    case["name"]
                );
            }
        }
    }

    #[test]
    fn numeric_columns_and_replays_do_not_round_large_identities() {
        assert!(!valid(&json!(9_007_199_254_740_993_i64), "number"));
        assert!(valid(&json!(9_007_199_254_740_994_i64), "number"));
        assert!(!valid(&json!(u64::MAX), "number"));
        assert!(valid(&json!(u64::MAX), "identity"));
        assert!(!valid(&json!(true), "number"));
        assert!(!py_eq(
            &json!(9_007_199_254_740_993_i64),
            &json!(9_007_199_254_740_992_f64)
        ));
        assert!(!py_eq(&json!(u64::MAX), &json!(u64::MAX - 1)));
        assert!(py_eq(&json!(true), &json!(1.0)));
    }

    #[test]
    fn arbitrary_precision_integers_keep_evidence_and_exact_replay_equality() {
        let exact: Value = serde_json::from_str("1267650600228229401496703205376").unwrap();
        let inexact: Value = serde_json::from_str("1267650600228229401496703205377").unwrap();
        assert!(valid(&exact, "number"));
        assert!(!valid(&inexact, "number"));
        assert!(valid(&inexact, "identity"));
        assert_eq!(audit_value(&inexact), inexact);
        assert!(py_eq(&exact, &json!(2_f64.powi(100))));
        assert!(!py_eq(&inexact, &json!(2_f64.powi(100))));
        assert!(!py_eq(&exact, &inexact));
        let huge: Value = serde_json::from_str(&format!("1{}", "0".repeat(400))).unwrap();
        assert!(is_truthy(&huge));
        assert!(valid(&huge, "identity"));
        assert!(!valid(&huge, "number"));
        assert_eq!(audit_value(&huge), huge);
        assert!(py_eq(&huge, &huge));
        assert!(!py_eq(&huge, &Value::Null));
    }

    #[test]
    fn historical_float_lexemes_follow_python_rounding_and_nonfinite_evidence() {
        let value: Value = serde_json::from_str("-0").unwrap();
        assert_eq!(audit_value(&value), json!(0));
        let value: Value = serde_json::from_str("-0.0").unwrap();
        assert_eq!(audit_value(&value), json!(-0.0));
        let value: Value = serde_json::from_str("1e400").unwrap();
        assert_eq!(audit_value(&value), json!({"invalid_number":"inf"}));
        let value: Value = serde_json::from_str("-1e400").unwrap();
        assert_eq!(audit_value(&value), json!({"invalid_number":"-inf"}));
        let value: Value = serde_json::from_str("1e-400").unwrap();
        assert_eq!(audit_value(&value), json!(0.0));
        let infinity = Value::Number(serde_json::Number::from_string_unchecked("Infinity".into()));
        assert_eq!(audit_value(&infinity), json!({"invalid_number":"inf"}));
        assert!(py_eq(&infinity, &infinity));
        let nan = Value::Number(serde_json::Number::from_string_unchecked("NaN".into()));
        assert_eq!(audit_value(&nan), json!({"invalid_number":"nan"}));
        assert!(!py_eq(&nan, &nan));
    }

    #[test]
    fn explicit_json_binding_distinguishes_missing_null_and_arbitrary_precision() {
        let connection = rusqlite::Connection::open_in_memory().unwrap();
        connection
            .execute_batch("CREATE TABLE sample(value JSON)")
            .unwrap();
        let huge: Value = serde_json::from_str("1267650600228229401496703205377").unwrap();
        let evidence = json!({"id": huge});
        for value in [None, Some(&Value::Null), Some(&evidence)] {
            connection
                .execute("INSERT INTO sample VALUES(?1)", [json_text(value).unwrap()])
                .unwrap();
        }
        let stored = connection
            .prepare("SELECT value FROM sample ORDER BY rowid")
            .unwrap()
            .query_map([], |row| row.get::<_, Option<String>>(0))
            .unwrap()
            .collect::<rusqlite::Result<Vec<_>>>()
            .unwrap();
        assert_eq!(
            stored,
            vec![None, Some("null".into()), Some(evidence.to_string())]
        );
    }

    #[test]
    fn invalid_asset_evidence_fails_instead_of_panicking() {
        let payload =
            json!({"account_assets":{"total_asset":"bad","audit_evidence":{"invalid_fields":[]}}});
        assert_eq!(
            encode_input_assets("i", &payload).err().unwrap(),
            "Invalid audit evidence fields"
        );
    }

    #[test]
    fn legacy_placeholder_requires_exact_source_and_nonnegative_duration() {
        let base = json!({"status":"BLOCKED","error":"当前不在交易时间","memory":{"message":"当前不在交易时间"},"inputs":{},"execution_time":0,"symbol_results":{},"account_assets":{"available_cash":0,"total_asset":0,"market_value":0,"positions":[],"extra":{}}});
        assert!(legacy_placeholder(base.as_object().unwrap()));
        let mut null_source = base.clone();
        null_source["account_assets"]["source"] = Value::Null;
        assert!(!legacy_placeholder(null_source.as_object().unwrap()));
        let mut negative_duration = base;
        negative_duration["execution_time"] = json!(-1);
        assert!(!legacy_placeholder(negative_duration.as_object().unwrap()));
    }
}
