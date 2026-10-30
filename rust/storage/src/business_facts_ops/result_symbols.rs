//! 按出现位置拆分品种、数量换算及五档行情,不重复订单和资产事实。

use rusqlite::Connection;
use serde_json::{json, Map, Value};

use super::encode::{audit_value, join_pointer, store_columns, valid, Rows};
use super::result_orders::CONVERSION_FIELDS;
use super::{facts_error, sql};
use crate::error::AnalysisError;

const SYMBOL_FIELDS: &[(&str, &str)] = &[
    ("symbol", "text"),
    ("algorithm", "text"),
    ("status", "text"),
    ("error", "text"),
    ("technical_detail", "text"),
    ("reason_code", "text"),
    ("outcome", "text"),
    ("outcome_reason", "text"),
    ("target_volume", "number"),
    ("final_volume", "number"),
    ("success", "boolean"),
    ("memory", "json"),
];
const SIZING_FIELDS: &[(&str, &str)] = &[
    ("symbol", "text"),
    ("sizing_mode", "text"),
    ("status", "text"),
    ("reason_code", "text"),
    ("account_weight", "number"),
    ("equity", "number"),
    ("reference_price", "number"),
    ("unit_multiplier", "number"),
    ("unit_notional", "number"),
    ("target_notional", "number"),
    ("raw_quantity", "number"),
    ("target_quantity", "number"),
    ("current_quantity", "number"),
    ("quantity_step", "number"),
    ("min_quantity", "number"),
    ("min_notional", "number"),
];
const TICK_FIELDS: &[(&str, &str)] = &[
    ("symbol", "text"),
    ("last_price", "number"),
    ("bid_price", "number"),
    ("bid_price_2", "number"),
    ("bid_price_3", "number"),
    ("bid_price_4", "number"),
    ("bid_price_5", "number"),
    ("bid_volume", "number"),
    ("bid_volume_2", "number"),
    ("bid_volume_3", "number"),
    ("bid_volume_4", "number"),
    ("bid_volume_5", "number"),
    ("ask_price", "number"),
    ("ask_price_2", "number"),
    ("ask_price_3", "number"),
    ("ask_price_4", "number"),
    ("ask_price_5", "number"),
    ("ask_volume", "number"),
    ("ask_volume_2", "number"),
    ("ask_volume_3", "number"),
    ("ask_volume_4", "number"),
    ("ask_volume_5", "number"),
    ("volume", "number"),
    ("turnover", "number"),
    ("timestamp", "integer"),
    ("update_time", "time"),
    ("timestamp_datetime", "time"),
    ("book_valid", "boolean"),
    ("extra", "json"),
];
const DELEGATED_FIELDS: &[&str] = &[
    "orders",
    "trades",
    "account_assets",
    "sizing",
    "first_tick",
    "availability",
];
const TARGET_FIELDS: &[(&str, &str)] = &[("value", "number")];

fn volume_state(value: &Value, key: &str) -> &'static str {
    let Some(payload) = value.as_object() else {
        return "absent";
    };
    if let Some(state) = payload
        .get("availability")
        .and_then(Value::as_object)
        .and_then(|states| states.get(key))
        .and_then(Value::as_str)
    {
        match state {
            "absent" => return "absent",
            "null" => return "null",
            "value" => return "value",
            "invalid" => return "invalid",
            _ => {}
        }
    }
    match payload.get(key) {
        None => "absent",
        Some(Value::Null) => "null",
        Some(value) if valid(value, "number") => "value",
        _ => "invalid",
    }
}

fn encode_object(
    record_id: i64,
    path: &str,
    ordinal: usize,
    map_key: Option<&str>,
    value: &Value,
    specs: &[(&str, &str)],
    child_keys: &[&str],
) -> Result<Map<String, Value>, AnalysisError> {
    let mut row = Map::from_iter([
        ("record_id".into(), json!(record_id)),
        ("path".into(), json!(path)),
        ("ordinal".into(), json!(ordinal)),
        (
            "map_key".into(),
            map_key.map_or(Value::Null, |key| json!(key)),
        ),
    ]);
    if ["/symbol_results", "/first_ticks", "/target_volume"].contains(&path) {
        row.insert(
            "member_count".into(),
            value
                .as_object()
                .map_or(Value::Null, |members| json!(members.len())),
        );
    }
    let object = value.as_object();
    if specs == SYMBOL_FIELDS {
        for key in ["target_volume", "final_volume"] {
            row.insert(format!("{key}_state"), json!(volume_state(value, key)));
        }
        row.insert(
            "sizing_recorded".into(),
            json!(object.is_some_and(|payload| payload.contains_key("sizing"))),
        );
        row.insert(
            "tick_recorded".into(),
            json!(object.is_some_and(|payload| payload.contains_key("first_tick"))),
        );
    }
    if specs == SIZING_FIELDS {
        row.insert(
            "conversion_recorded".into(),
            json!(object.is_some_and(|payload| payload.contains_key("value_conversion"))),
        );
    }
    if let Some(payload) = object {
        let (columns, evidence) = store_columns(payload, specs, child_keys).map_err(facts_error)?;
        row.extend(columns);
        row.insert("node_kind".into(), json!("object"));
        row.insert("invalid_value".into(), Value::Null);
        row.insert("audit_evidence".into(), Value::Object(evidence));
    } else {
        row.extend(specs.iter().map(|(key, _)| (key.to_string(), Value::Null)));
        row.insert("invalid_value".into(), audit_value(value));
        row.insert("node_kind".into(), json!("value"));
        row.insert("audit_evidence".into(), json!({}));
    }
    Ok(row)
}

/// 顺序、空映射、异常节点以及映射键和原生品种身份分别保留。
pub(crate) fn encode_symbols(
    record_id: i64,
    result: &Value,
) -> Result<(Rows, Rows, Rows, Rows, Rows, Rows), AnalysisError> {
    let mut collections = Vec::new();
    let mut results = Vec::new();
    let mut sizings = Vec::new();
    let mut conversions = Vec::new();
    let mut ticks = Vec::new();
    let mut targets = Vec::new();
    for name in ["symbol_results", "first_ticks", "target_volume"] {
        let Some(collection) = result.get(name) else {
            continue;
        };
        let path = join_pointer("", name);
        let keys = collection
            .as_object()
            .map(|members| members.keys().map(String::as_str).collect::<Vec<_>>())
            .unwrap_or_default();
        collections.push(encode_object(
            record_id,
            &path,
            0,
            None,
            collection,
            &[],
            &keys,
        )?);
        let Some(members) = collection.as_object() else {
            continue;
        };
        for (ordinal, (key, value)) in members.iter().enumerate() {
            let item_path = join_pointer(&path, key);
            match name {
                "symbol_results" => {
                    results.push(encode_object(
                        record_id,
                        &item_path,
                        ordinal,
                        Some(key),
                        value,
                        SYMBOL_FIELDS,
                        DELEGATED_FIELDS,
                    )?);
                    if let Some(sizing) = value.get("sizing") {
                        let sizing_path = join_pointer(&item_path, "sizing");
                        sizings.push(encode_object(
                            record_id,
                            &sizing_path,
                            ordinal,
                            Some(key),
                            sizing,
                            SIZING_FIELDS,
                            &["value_conversion"],
                        )?);
                        if let Some(conversion) = sizing.get("value_conversion") {
                            conversions.push(encode_object(
                                record_id,
                                &join_pointer(&sizing_path, "value_conversion"),
                                ordinal,
                                Some(key),
                                conversion,
                                CONVERSION_FIELDS,
                                &[],
                            )?);
                        }
                    }
                    if let Some(tick) = value.get("first_tick") {
                        ticks.push(encode_object(
                            record_id,
                            &join_pointer(&item_path, "first_tick"),
                            ordinal,
                            Some(key),
                            tick,
                            TICK_FIELDS,
                            &[],
                        )?);
                    }
                }
                "first_ticks" => ticks.push(encode_object(
                    record_id,
                    &item_path,
                    ordinal,
                    Some(key),
                    value,
                    TICK_FIELDS,
                    &[],
                )?),
                _ => targets.push(encode_object(
                    record_id,
                    &item_path,
                    ordinal,
                    Some(key),
                    &json!({"value":value}),
                    TARGET_FIELDS,
                    &[],
                )?),
            }
        }
    }
    Ok((collections, results, sizings, conversions, ticks, targets))
}

pub(crate) fn write_result_symbols(
    connection: &Connection,
    record_id: i64,
    result: &Value,
) -> Result<(), AnalysisError> {
    let (collections, results, sizings, conversions, ticks, targets) =
        encode_symbols(record_id, result)?;
    super::insert_rows(
        connection,
        "execution_symbol_collection",
        &collections,
        &[
            ("record_id", "integer"),
            ("path", "text"),
            ("ordinal", "integer"),
            ("map_key", "text"),
            ("node_kind", "text"),
            ("invalid_value", "json"),
            ("audit_evidence", "json"),
            ("member_count", "integer"),
        ],
    )?;
    super::insert_rows(
        connection,
        "execution_symbol_result",
        &results,
        &[
            ("record_id", "integer"),
            ("path", "text"),
            ("ordinal", "integer"),
            ("map_key", "text"),
            ("node_kind", "text"),
            ("invalid_value", "json"),
            ("audit_evidence", "json"),
            ("sizing_recorded", "boolean"),
            ("tick_recorded", "boolean"),
            ("target_volume_state", "text"),
            ("final_volume_state", "text"),
            ("symbol", "text"),
            ("algorithm", "text"),
            ("status", "text"),
            ("error", "text"),
            ("technical_detail", "text"),
            ("reason_code", "text"),
            ("outcome", "text"),
            ("outcome_reason", "text"),
            ("target_volume", "number"),
            ("final_volume", "number"),
            ("success", "boolean"),
            ("memory", "json"),
        ],
    )?;
    super::insert_rows(
        connection,
        "execution_symbol_sizing",
        &sizings,
        &[
            ("record_id", "integer"),
            ("path", "text"),
            ("ordinal", "integer"),
            ("map_key", "text"),
            ("node_kind", "text"),
            ("invalid_value", "json"),
            ("audit_evidence", "json"),
            ("conversion_recorded", "boolean"),
            ("symbol", "text"),
            ("sizing_mode", "text"),
            ("status", "text"),
            ("reason_code", "text"),
            ("account_weight", "number"),
            ("equity", "number"),
            ("reference_price", "number"),
            ("unit_multiplier", "number"),
            ("unit_notional", "number"),
            ("target_notional", "number"),
            ("raw_quantity", "number"),
            ("target_quantity", "number"),
            ("current_quantity", "number"),
            ("quantity_step", "number"),
            ("min_quantity", "number"),
            ("min_notional", "number"),
        ],
    )?;
    super::insert_rows(
        connection,
        "execution_symbol_conversion",
        &conversions,
        &[
            ("record_id", "integer"),
            ("path", "text"),
            ("ordinal", "integer"),
            ("map_key", "text"),
            ("node_kind", "text"),
            ("invalid_value", "json"),
            ("audit_evidence", "json"),
            ("price_currency", "text"),
            ("value_currency", "text"),
            ("rate", "number"),
            ("reference", "json"),
        ],
    )?;
    super::insert_rows(
        connection,
        "execution_symbol_tick",
        &ticks,
        &[
            ("record_id", "integer"),
            ("path", "text"),
            ("ordinal", "integer"),
            ("map_key", "text"),
            ("node_kind", "text"),
            ("invalid_value", "json"),
            ("audit_evidence", "json"),
            ("symbol", "text"),
            ("last_price", "number"),
            ("bid_price", "number"),
            ("bid_price_2", "number"),
            ("bid_price_3", "number"),
            ("bid_price_4", "number"),
            ("bid_price_5", "number"),
            ("bid_volume", "number"),
            ("bid_volume_2", "number"),
            ("bid_volume_3", "number"),
            ("bid_volume_4", "number"),
            ("bid_volume_5", "number"),
            ("ask_price", "number"),
            ("ask_price_2", "number"),
            ("ask_price_3", "number"),
            ("ask_price_4", "number"),
            ("ask_price_5", "number"),
            ("ask_volume", "number"),
            ("ask_volume_2", "number"),
            ("ask_volume_3", "number"),
            ("ask_volume_4", "number"),
            ("ask_volume_5", "number"),
            ("volume", "number"),
            ("turnover", "number"),
            ("timestamp", "integer"),
            ("update_time", "text"),
            ("timestamp_datetime", "text"),
            ("book_valid", "boolean"),
            ("extra", "json"),
        ],
    )?;
    super::insert_rows(
        connection,
        "execution_symbol_target",
        &targets,
        &[
            ("record_id", "integer"),
            ("path", "text"),
            ("ordinal", "integer"),
            ("map_key", "text"),
            ("node_kind", "text"),
            ("invalid_value", "json"),
            ("audit_evidence", "json"),
            ("value", "number"),
        ],
    )?;
    Ok(())
}

fn decode_object(row: &Map<String, Value>, specs: &[(&str, &str)]) -> Result<Value, AnalysisError> {
    let evidence = row
        .get("audit_evidence")
        .and_then(Value::as_object)
        .ok_or_else(|| {
            facts_error(format!(
                "Invalid symbol mapping at {}/audit_evidence",
                row.get("path").and_then(Value::as_str).unwrap_or("")
            ))
        })?;
    if row.get("node_kind").and_then(Value::as_str) == Some("value") {
        return Ok(row.get("invalid_value").cloned().unwrap_or(Value::Null));
    }
    let mut value = Map::new();
    for (key, _) in specs {
        if let Some(field) = row
            .get(*key)
            .filter(|value| !value.is_null() || specs.len() == 1)
        {
            value.insert((*key).into(), field.clone());
        }
    }
    if !evidence.is_empty() {
        value.insert("audit_evidence".into(), Value::Object(evidence.clone()));
    }
    Ok(Value::Object(value))
}

/// 只读取状态和结论,不展开逐品种的订单、成交、memory 或行情。
pub(crate) fn read_outcome_symbols(
    connection: &Connection,
    record_id: i64,
) -> Result<Value, AnalysisError> {
    let mut statement = connection.prepare("SELECT path,map_key,node_kind,status,outcome FROM execution_symbol_result WHERE record_id=?1 ORDER BY ordinal").map_err(sql)?;
    let mut cursor = statement.query([record_id]).map_err(sql)?;
    let mut result = Vec::new();
    while let Some(row) = cursor.next().map_err(sql)? {
        let row =
            crate::business_detail::decode(row, "execution_symbol_result").map_err(facts_error)?;
        if row.get("node_kind").and_then(Value::as_str) != Some("object") {
            continue;
        }
        let fields = ["status", "outcome"]
            .iter()
            .filter_map(|key| {
                row.get(*key)
                    .filter(|value| !value.is_null())
                    .map(|value| (key.to_string(), value.clone()))
            })
            .collect::<Map<_, _>>();
        let normalized = super::result_head::normalize_legacy_result(&Value::Object(fields))?;
        if matches!(
            normalized.get("outcome").and_then(Value::as_str),
            Some("not_reached" | "blocked")
        ) {
            let key = row.get("map_key").and_then(Value::as_str).ok_or_else(|| {
                facts_error(format!(
                    "Invalid symbol map key at {}",
                    row.get("path").and_then(Value::as_str).unwrap_or("")
                ))
            })?;
            result.push(json!(key));
        }
    }
    Ok(Value::Array(result))
}

/// 显式加载换算证据并验证已记录汇率子行,不加载完整执行详情。
pub(crate) fn read_sizing_rows(
    connection: &Connection,
    record_id: i64,
) -> Result<Value, AnalysisError> {
    let id = json!(record_id);
    let mut sizings =
        crate::business_detail::load(connection, "execution_symbol_sizing", "record_id", &id)
            .map_err(facts_error)?;
    let conversions =
        crate::business_detail::load(connection, "execution_symbol_conversion", "record_id", &id)
            .map_err(facts_error)?;
    let mut by_path = Map::new();
    for row in conversions {
        let path = row
            .get("path")
            .and_then(Value::as_str)
            .ok_or_else(|| facts_error("Invalid symbol conversion path"))?
            .to_owned();
        if by_path.insert(path.clone(), Value::Object(row)).is_some() {
            return Err(facts_error(format!("Duplicate symbol fact at {path}")));
        }
    }
    sizings.sort_by_key(|row| row.get("ordinal").and_then(Value::as_i64).unwrap_or(0));
    let mut result = Map::new();
    for row in sizings {
        let sizing = decode_object(&row, SIZING_FIELDS)?;
        let Value::Object(mut sizing) = sizing else {
            continue;
        };
        let path = row
            .get("path")
            .and_then(Value::as_str)
            .ok_or_else(|| facts_error("Invalid symbol sizing path"))?;
        let conversion_path = join_pointer(path, "value_conversion");
        let conversion = by_path.get(&conversion_path).and_then(Value::as_object);
        if row
            .get("conversion_recorded")
            .is_some_and(super::encode::is_truthy)
            && conversion.is_none()
        {
            return Err(facts_error(format!(
                "Missing sizing conversion fact at {conversion_path}"
            )));
        }
        if let Some(conversion) = conversion {
            sizing.insert(
                "value_conversion".into(),
                decode_object(conversion, CONVERSION_FIELDS)?,
            );
        }
        let key = row
            .get("map_key")
            .and_then(Value::as_str)
            .ok_or_else(|| facts_error(format!("Invalid symbol map key at {path}")))?;
        result.insert(key.into(), Value::Object(sizing));
    }
    Ok(Value::Object(result))
}

#[cfg(test)]
mod tests {
    use super::super::encode::frozen_cases;
    use super::*;

    #[test]
    fn frozen_0037_symbol_fixtures_preserve_occurrences_sizing_and_five_level_ticks() {
        for case in frozen_cases("symbols") {
            let encoded = encode_symbols(7, &case["payload"]);
            if let Some(error) = case.get("error") {
                assert_eq!(
                    encoded.err().unwrap().error,
                    error.as_str().unwrap(),
                    "{}",
                    case["name"]
                );
            } else {
                let mut expected = case["expected"].clone();
                for tick in expected["execution_symbol_tick"].as_array_mut().unwrap() {
                    for name in ["update_time", "timestamp_datetime"] {
                        if let Some(value) = tick[name]
                            .as_str()
                            .filter(|value| {
                                [
                                    "native-time-without-timezone",
                                    "update_time",
                                    "timestamp_datetime",
                                ]
                                .contains(value)
                            })
                            .map(str::to_owned)
                        {
                            tick[name] = Value::Null;
                            let evidence = tick["audit_evidence"].as_object_mut().unwrap();
                            let invalid = evidence.entry("invalid_fields").or_insert(json!({}));
                            invalid[name] = json!(value);
                        }
                    }
                }
                assert_eq!(
                    {
                        let (collections, results, sizings, conversions, ticks, targets) =
                            encoded.unwrap();
                        json!({"execution_symbol_collection":collections,"execution_symbol_result":results,"execution_symbol_sizing":sizings,"execution_symbol_conversion":conversions,"execution_symbol_tick":ticks,"execution_symbol_target":targets})
                    },
                    expected,
                    "{}",
                    case["name"]
                );
            }
        }
    }

    #[test]
    fn narrow_readers_preserve_order_and_reject_missing_conversion() {
        let connection = Connection::open_in_memory().unwrap();
        let prototype = encode_symbols(7, &json!({"symbol_results":{"schema":{"sizing":{"value_conversion":{}},"first_tick":{}}},"first_ticks":{},"target_volume":{"schema":0}})).unwrap();
        for (table, rows) in [
            ("execution_symbol_collection", &prototype.0),
            ("execution_symbol_result", &prototype.1),
            ("execution_symbol_sizing", &prototype.2),
            ("execution_symbol_conversion", &prototype.3),
            ("execution_symbol_tick", &prototype.4),
            ("execution_symbol_target", &prototype.5),
        ] {
            let columns = rows[0]
                .keys()
                .map(|key| format!("\"{key}\""))
                .collect::<Vec<_>>()
                .join(",");
            connection
                .execute_batch(&format!("CREATE TABLE {table}({columns})"))
                .unwrap();
        }
        let payload = json!({"symbol_results":{
            "Z/~":{"status":"PARTIAL","sizing":{"target_quantity":0,"value_conversion":{"rate":1.25}}},
            "A":{"outcome":"blocked"},"malformed":null,
            "done":{"status":"NOOP"},"explicit":{"status":"PARTIAL","outcome":"completed"}
        }});
        write_result_symbols(&connection, 7, &payload).unwrap();
        assert_eq!(
            read_outcome_symbols(&connection, 7).unwrap(),
            json!(["Z/~", "A"])
        );
        assert_eq!(
            read_sizing_rows(&connection, 7).unwrap(),
            json!({"Z/~":{"target_quantity":0.0,"value_conversion":{"rate":1.25}}})
        );
        connection
            .execute_batch("DELETE FROM execution_symbol_conversion")
            .unwrap();
        assert_eq!(
            read_sizing_rows(&connection, 7).unwrap_err().error,
            "Missing sizing conversion fact at /symbol_results/Z~1~0/sizing/value_conversion"
        );
    }
}
