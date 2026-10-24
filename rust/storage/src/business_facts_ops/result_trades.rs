//! 成交按出现位置保存，金额、单位与手续费均使用原始观察。

use rusqlite::Connection;
use serde_json::{json, Map, Value};

use super::encode::{audit_value, join_pointer, store_columns, valid, FactBatch};
use super::result_orders::CONVERSION_FIELDS;
use super::{facts_error, insert_batch};
use crate::error::AnalysisError;

const TRADE_FIELDS: &[(&str, &str)] = &[
    ("trade_id", "identity"),
    ("symbol", "text"),
    ("order_id", "identity"),
    ("trade_time", "text"),
    ("trade_volume", "number"),
    ("trade_price", "number"),
    ("trade_value", "number"),
    ("extra", "json"),
];

fn conversion_row(record_id: i64, path: &str, value: &Value) -> Result<Map<String, Value>, String> {
    let (columns, evidence) = store_columns(
        value.as_object().unwrap_or(&Map::new()),
        CONVERSION_FIELDS,
        &[],
    )?;
    let mut row = json!({
        "record_id": record_id, "trade_path": path,
        "state": if value.is_object() { "object" } else { "malformed" },
        "malformed_value": if value.is_object() { Value::Null } else { audit_value(value) },
        "audit_evidence": evidence
    })
    .as_object()
    .expect("object literal")
    .clone();
    row.extend(columns);
    Ok(row)
}

fn trade_row(
    record_id: i64,
    collection_path: &str,
    ordinal: usize,
    value: &Value,
) -> Result<Map<String, Value>, String> {
    let payload = value.as_object();
    let (columns, evidence) = store_columns(
        payload.unwrap_or(&Map::new()),
        TRADE_FIELDS,
        &["value_conversion"],
    )?;
    let extra = payload
        .and_then(|payload| payload.get("extra"))
        .and_then(Value::as_object);
    let commission = extra.and_then(|extra| extra.get("commission"));
    let fee = commission.filter(|value| !value.is_null() && valid(value, "number"));
    let currency = extra
        .and_then(|extra| extra.get("commission_asset"))
        .filter(|value| value.is_string());
    let mut row = json!({
        "record_id": record_id, "path": join_pointer(collection_path, &ordinal.to_string()),
        "collection_path": collection_path, "ordinal": ordinal,
        "state": if payload.is_some() { "object" } else { "malformed" },
        "malformed_value": if payload.is_some() { Value::Null } else { audit_value(value) },
        "conversion_recorded": payload.is_some_and(|payload| payload.contains_key("value_conversion")),
        "fee": fee, "fee_currency": currency,
        "fee_state": match commission {
            None => "absent", Some(Value::Null) => "null",
            Some(_) if fee.is_some() => "value", _ => "invalid"
        },
        "audit_evidence": evidence
    })
    .as_object()
    .expect("object literal")
    .clone();
    row.extend(columns);
    for key in ["trade_id", "order_id"] {
        let state = match payload.and_then(|payload| payload.get(key)) {
            None => "absent",
            Some(Value::Null) => "null",
            Some(_) if row[key].is_null() => "invalid",
            _ => "value",
        };
        row.insert(format!("{key}_state"), json!(state));
    }
    Ok(row)
}

fn append_collection(
    collections: &mut Vec<Map<String, Value>>,
    trades: &mut Vec<Map<String, Value>>,
    conversions: &mut Vec<Map<String, Value>>,
    record_id: i64,
    scope: &str,
    ordinal: usize,
    map_key: Option<&str>,
    group: &Map<String, Value>,
) -> Result<(), String> {
    let Some(value) = group.get("trades") else {
        return Ok(());
    };
    let path = join_pointer(scope, "trades");
    collections.push(
        json!({
            "record_id": record_id, "path": path, "parent_path": scope,
            "ordinal": ordinal, "map_key": map_key,
            "state": if value.is_array() { "array" } else { "malformed" },
            "member_count": value.as_array().map(Vec::len),
            "malformed_value": if value.is_array() { Value::Null } else { audit_value(value) },
            "audit_evidence": {}
        })
        .as_object()
        .expect("object literal")
        .clone(),
    );
    if let Some(values) = value.as_array() {
        for (index, member) in values.iter().enumerate() {
            let row = trade_row(record_id, &path, index, member)?;
            if let Some(conversion) = member.get("value_conversion") {
                conversions.push(conversion_row(
                    record_id,
                    row["path"].as_str().expect("path"),
                    conversion,
                )?);
            }
            trades.push(row);
        }
    }
    Ok(())
}

/// 纯编码供终态事务与 0037 历史转换共用。
pub(crate) fn encode_trades(
    record_id: i64,
    payload: &Map<String, Value>,
) -> Result<FactBatch, String> {
    let mut collections = Vec::new();
    let mut trades = Vec::new();
    let mut conversions = Vec::new();
    append_collection(
        &mut collections,
        &mut trades,
        &mut conversions,
        record_id,
        "",
        0,
        None,
        payload,
    )?;
    if let Some(symbols) = payload.get("symbol_results").and_then(Value::as_object) {
        for (ordinal, (key, value)) in symbols.iter().enumerate() {
            if let Some(group) = value.as_object() {
                append_collection(
                    &mut collections,
                    &mut trades,
                    &mut conversions,
                    record_id,
                    &join_pointer("/symbol_results", key),
                    ordinal,
                    Some(key),
                    group,
                )?;
            }
        }
    }
    let mut batch = FactBatch::new();
    batch.push("business_trade_collection", collections);
    batch.push("business_trade", trades);
    batch.push("business_trade_conversion", conversions);
    Ok(batch)
}

/// 只使用调用方连接，不开启或提交事务。
pub(crate) fn insert_trades(
    connection: &Connection,
    record_id: i64,
    payload: &Map<String, Value>,
) -> Result<(), AnalysisError> {
    insert_batch(
        connection,
        &encode_trades(record_id, payload).map_err(facts_error)?,
    )
}

#[cfg(test)]
mod tests {
    use super::super::result_orders::frozen_fixture;
    use super::*;

    #[test]
    fn trade_codec_matches_frozen_python_occurrences_fees_and_metadata() {
        let frozen = frozen_fixture();
        let batch = encode_trades(19, frozen["payload"].as_object().unwrap()).unwrap();
        for (table, rows) in batch.statements() {
            assert_eq!(json!(rows), frozen["trades"][*table], "{table}");
        }
        let rows = &batch.statements()[1].1;
        assert_eq!(rows[2]["fee"], json!(0));
        assert_eq!(rows[3]["fee"], json!(-0.002));
        assert_eq!(rows[7]["fee_state"], json!("invalid"));
        assert_eq!(rows[8]["fee"], json!(18_014_398_509_481_984_u64));
        assert_eq!(rows[9]["fee"], Value::Null);
        assert_eq!(rows[11]["trade_id"], json!(u64::MAX));
        assert_eq!(rows[0]["trade_value"], json!(70200.0));
    }

    #[test]
    fn trade_insert_uses_caller_transaction_and_native_reader() {
        let frozen = frozen_fixture();
        let connection = Connection::open_in_memory().unwrap();
        connection
            .execute_batch(frozen["schema"].as_str().unwrap())
            .unwrap();
        connection.execute_batch("BEGIN").unwrap();
        insert_trades(&connection, 19, frozen["payload"].as_object().unwrap()).unwrap();
        assert!(!connection.is_autocommit());
        let mut result = Map::new();
        crate::business_detail::collections(&connection, &json!(19), &mut result, true).unwrap();
        assert_eq!(Value::Object(result), frozen["decoded_sql_trades"]);
        connection.execute_batch("ROLLBACK").unwrap();
        for table in [
            "business_trade_collection",
            "business_trade",
            "business_trade_conversion",
        ] {
            assert_eq!(
                connection
                    .query_row(&format!("SELECT COUNT(*) FROM {table}"), [], |row| row
                        .get::<_, i64>(0))
                    .unwrap(),
                0
            );
        }
    }

    #[test]
    fn missing_trade_conversion_and_occurrences_are_rejected() {
        let frozen = frozen_fixture();
        let connection = Connection::open_in_memory().unwrap();
        connection
            .execute_batch(frozen["schema"].as_str().unwrap())
            .unwrap();
        insert_trades(
            &connection,
            19,
            json!({"trades": [{"value_conversion": {"rate": 7}}]})
                .as_object()
                .unwrap(),
        )
        .unwrap();
        connection
            .execute("DELETE FROM business_trade_conversion", [])
            .unwrap();
        assert!(crate::business_detail::collections(
            &connection,
            &json!(19),
            &mut Map::new(),
            true
        )
        .is_err());
        connection
            .execute("DELETE FROM business_trade", [])
            .unwrap();
        assert!(crate::business_detail::collections(
            &connection,
            &json!(19),
            &mut Map::new(),
            true
        )
        .is_err());
    }

    #[test]
    fn invalid_trade_metadata_rejects_entire_encoding() {
        let payload = json!({"trades": [{}, {"value_conversion": {
            "rate": false, "audit_evidence": {"invalid_fields": []}
        }}]});
        assert_eq!(
            encode_trades(19, payload.as_object().unwrap())
                .err()
                .unwrap(),
            "Invalid audit evidence fields"
        );
    }

    #[test]
    fn absent_trade_collections_produce_no_rows() {
        let batch = encode_trades(
            19,
            json!({"symbol_results": {"missing": {}, "bad": []}})
                .as_object()
                .unwrap(),
        )
        .unwrap();
        assert!(batch.statements().iter().all(|(_, rows)| rows.is_empty()));
    }
}
