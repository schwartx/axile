//! 按来源路径与出现顺序保存订单观察，不合并重复的原生身份。

use rusqlite::Connection;
use serde_json::{json, Map, Value};

use super::encode::{audit_value, join_pointer, store_columns, Rows};
use super::facts_error;
use super::{encode, sql};
use crate::error::AnalysisError;

const ORDER_FIELDS: &[(&str, &str)] = &[
    ("order_id", "identity"),
    ("symbol", "text"),
    ("direction", "text"),
    ("order_type", "text"),
    ("volume", "number"),
    ("price", "number"),
    ("status", "text"),
    ("filled_volume", "number"),
    ("avg_price", "number"),
    ("create_time", "text"),
    ("update_time", "text"),
    ("update_timestamp", "number"),
    ("remaining_volume", "number"),
    ("filled_ratio", "number"),
    ("extra", "json"),
];
pub(super) const CONVERSION_FIELDS: &[(&str, &str)] = &[
    ("price_currency", "text"),
    ("value_currency", "text"),
    ("rate", "number"),
    ("reference", "json"),
];

fn object_kind(value: &Value) -> &'static str {
    if value.is_null() {
        "null"
    } else if value.is_object() {
        "object"
    } else {
        "invalid"
    }
}

fn order_row(
    record_id: i64,
    path: &str,
    map_key: Option<&str>,
    ordinal: usize,
    value: &Value,
) -> Result<Map<String, Value>, String> {
    let payload = value.as_object();
    let (columns, evidence) = store_columns(
        payload.unwrap_or(&Map::new()),
        ORDER_FIELDS,
        &["value_conversion"],
    )?;
    let mut row = json!({
        "record_id": record_id, "path": path, "map_key": map_key,
        "ordinal": ordinal, "kind": object_kind(value),
        "invalid_value": if payload.is_some() { Value::Null } else { audit_value(value) },
        "order_id_state": match payload.and_then(|payload| payload.get("order_id")) {
            None => "absent",
            Some(Value::Null) => "null",
            Some(_) if columns["order_id"].is_null() => "invalid",
            _ => "value",
        },
        "audit_evidence": evidence,
        "conversion_kind": null, "conversion_invalid_value": null,
        "conversion_evidence": null
    })
    .as_object()
    .expect("object literal")
    .clone();
    row.extend(columns);
    for (name, _) in CONVERSION_FIELDS {
        row.insert(format!("conversion_{name}"), Value::Null);
    }
    if let Some(conversion) = payload.and_then(|payload| payload.get("value_conversion")) {
        row.insert("conversion_kind".into(), json!(object_kind(conversion)));
        let (columns, evidence) = store_columns(
            conversion.as_object().unwrap_or(&Map::new()),
            CONVERSION_FIELDS,
            &[],
        )?;
        for (name, value) in columns {
            row.insert(format!("conversion_{name}"), value);
        }
        if !conversion.is_object() {
            row.insert("conversion_invalid_value".into(), audit_value(conversion));
        }
        row.insert("conversion_evidence".into(), Value::Object(evidence));
    }
    Ok(row)
}

fn append_collection(
    collections: &mut Vec<Map<String, Value>>,
    orders: &mut Vec<Map<String, Value>>,
    record_id: i64,
    scope: &str,
    map_key: Option<&str>,
    ordinal: usize,
    group: &Map<String, Value>,
) -> Result<(), String> {
    let Some(value) = group.get("orders") else {
        return Ok(());
    };
    let path = join_pointer(scope, "orders");
    let mut collection = json!({
        "record_id": record_id, "path": path, "map_key": map_key,
        "ordinal": ordinal,
        "kind": if value.is_array() { "array" } else if value.is_null() { "null" } else { "invalid" },
        "invalid_value": null,
        "item_count": value.as_array().map(Vec::len), "audit_evidence": {}
    })
    .as_object()
    .expect("object literal")
    .clone();
    if let Some(values) = value.as_array() {
        for (index, member) in values.iter().enumerate() {
            orders.push(order_row(record_id, &path, map_key, index, member)?);
        }
    } else {
        collection.insert("invalid_value".into(), audit_value(value));
    }
    collections.push(collection);
    Ok(())
}

/// 纯编码供终态事务与 0037 历史转换共用。
pub(crate) fn encode_orders(
    record_id: i64,
    payload: &Map<String, Value>,
) -> Result<(Rows, Rows), String> {
    let mut collections = Vec::new();
    let mut orders = Vec::new();
    append_collection(
        &mut collections,
        &mut orders,
        record_id,
        "",
        None,
        0,
        payload,
    )?;
    if let Some(symbols) = payload.get("symbol_results").and_then(Value::as_object) {
        for (ordinal, (key, value)) in symbols.iter().enumerate() {
            if let Some(group) = value.as_object() {
                append_collection(
                    &mut collections,
                    &mut orders,
                    record_id,
                    &join_pointer("/symbol_results", key),
                    Some(key),
                    ordinal,
                    group,
                )?;
            }
        }
    }
    Ok((collections, orders))
}

/// 只使用调用方连接，不开启或提交事务。
pub(crate) fn insert_orders(
    connection: &Connection,
    record_id: i64,
    payload: &Map<String, Value>,
) -> Result<(), AnalysisError> {
    let (collections, orders) = encode_orders(record_id, payload).map_err(facts_error)?;
    let mut statement = connection
        .prepare_cached(
            r#"INSERT INTO execution_order_collection (
                "record_id", "path", "ordinal", "map_key", "kind", "invalid_value",
                "item_count", "audit_evidence"
            ) VALUES (
                ?1, ?2, ?3, ?4, ?5, ?6,
                ?7, ?8
            )"#,
        )
        .map_err(sql)?;
    for row in &collections {
        statement
            .execute(rusqlite::params![
                row.get("record_id").and_then(Value::as_i64),
                row.get("path").and_then(Value::as_str),
                row.get("ordinal").and_then(Value::as_i64),
                row.get("map_key").and_then(Value::as_str),
                row.get("kind").and_then(Value::as_str),
                encode::json_text(row.get("invalid_value"))?,
                row.get("item_count").and_then(Value::as_i64),
                encode::json_text(row.get("audit_evidence"))?,
            ])
            .map_err(sql)?;
    }
    let mut statement = connection.prepare_cached(r#"INSERT INTO execution_order (
                "record_id", "path", "ordinal", "map_key", "kind", "invalid_value",
                "order_id", "symbol", "direction", "order_type", "volume", "price",
                "status", "filled_volume", "avg_price", "create_time", "update_time", "update_timestamp",
                "remaining_volume", "filled_ratio", "extra", "audit_evidence", "order_id_state", "conversion_kind",
                "conversion_invalid_value", "conversion_price_currency", "conversion_value_currency", "conversion_rate", "conversion_reference", "conversion_evidence"
            ) VALUES (
                ?1, ?2, ?3, ?4, ?5, ?6,
                ?7, ?8, ?9, ?10, ?11, ?12,
                ?13, ?14, ?15, ?16, ?17, ?18,
                ?19, ?20, ?21, ?22, ?23, ?24,
                ?25, ?26, ?27, ?28, ?29, ?30
            )"#).map_err(sql)?;
    for row in &orders {
        statement
            .execute(rusqlite::params![
                row.get("record_id").and_then(Value::as_i64),
                row.get("path").and_then(Value::as_str),
                row.get("ordinal").and_then(Value::as_i64),
                row.get("map_key").and_then(Value::as_str),
                row.get("kind").and_then(Value::as_str),
                encode::json_text(row.get("invalid_value"))?,
                encode::json_text(row.get("order_id"))?,
                row.get("symbol").and_then(Value::as_str),
                row.get("direction").and_then(Value::as_str),
                row.get("order_type").and_then(Value::as_str),
                row.get("volume").and_then(Value::as_f64),
                row.get("price").and_then(Value::as_f64),
                row.get("status").and_then(Value::as_str),
                row.get("filled_volume").and_then(Value::as_f64),
                row.get("avg_price").and_then(Value::as_f64),
                row.get("create_time").and_then(Value::as_str),
                row.get("update_time").and_then(Value::as_str),
                row.get("update_timestamp").and_then(Value::as_f64),
                row.get("remaining_volume").and_then(Value::as_f64),
                row.get("filled_ratio").and_then(Value::as_f64),
                encode::json_text(row.get("extra"))?,
                encode::json_text(row.get("audit_evidence"))?,
                row.get("order_id_state").and_then(Value::as_str),
                row.get("conversion_kind").and_then(Value::as_str),
                encode::json_text(row.get("conversion_invalid_value"))?,
                row.get("conversion_price_currency").and_then(Value::as_str),
                row.get("conversion_value_currency").and_then(Value::as_str),
                row.get("conversion_rate").and_then(Value::as_f64),
                encode::json_text(row.get("conversion_reference"))?,
                encode::json_text(row.get("conversion_evidence"))?,
            ])
            .map_err(sql)?;
    }
    Ok(())
}

#[cfg(test)]
pub(super) fn frozen_fixture() -> Value {
    serde_json::from_str(include_str!(
        "../../../../tests/fixtures/rust_store/result_orders_trades.json"
    ))
    .unwrap()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn order_codec_matches_frozen_python_columns_and_evidence() {
        let frozen = frozen_fixture();
        let payload = frozen["payload"].as_object().unwrap();
        let batch = encode_orders(19, payload).unwrap();
        for (table, rows) in [
            ("execution_order_collection", &batch.0),
            ("execution_order", &batch.1),
        ] {
            assert_eq!(json!(rows), frozen["orders"][table], "{table}");
        }
        assert_eq!(Value::Object(payload.clone()), frozen["payload"]);
    }

    #[test]
    fn order_insert_uses_caller_transaction_and_native_reader() {
        let frozen = frozen_fixture();
        let connection = Connection::open_in_memory().unwrap();
        connection
            .execute_batch(frozen["schema"].as_str().unwrap())
            .unwrap();
        connection.execute_batch("BEGIN").unwrap();
        insert_orders(&connection, 19, frozen["payload"].as_object().unwrap()).unwrap();
        assert!(!connection.is_autocommit());
        let mut result = Map::new();
        crate::business_detail::collections(&connection, &json!(19), &mut result, false).unwrap();
        assert_eq!(Value::Object(result), frozen["decoded_sql_orders"]);
        connection.execute_batch("ROLLBACK").unwrap();
        assert_eq!(
            connection
                .query_row("SELECT COUNT(*) FROM execution_order", [], |row| row
                    .get::<_, i64>(0))
                .unwrap(),
            0
        );
        assert_eq!(
            connection
                .query_row(
                    "SELECT COUNT(*) FROM execution_order_collection",
                    [],
                    |row| row.get::<_, i64>(0)
                )
                .unwrap(),
            0
        );
    }

    #[test]
    fn incomplete_order_occurrences_and_orphan_rows_are_rejected() {
        let frozen = frozen_fixture();
        let connection = Connection::open_in_memory().unwrap();
        connection
            .execute_batch(frozen["schema"].as_str().unwrap())
            .unwrap();
        insert_orders(
            &connection,
            19,
            json!({"orders": [{}, {}]}).as_object().unwrap(),
        )
        .unwrap();
        connection
            .execute("DELETE FROM execution_order WHERE ordinal=1", [])
            .unwrap();
        assert!(crate::business_detail::collections(
            &connection,
            &json!(19),
            &mut Map::new(),
            false
        )
        .is_err());
        // 暂停外键检查以制造归属损坏，保留集合与订单行。
        connection
            .execute_batch(
                "PRAGMA foreign_keys=OFF;
                 INSERT INTO executerecord(id) VALUES(20);
                 UPDATE execution_order_collection SET record_id=20;
                 PRAGMA foreign_keys=ON;",
            )
            .unwrap();
        assert_eq!(
            crate::business_detail::collections(&connection, &json!(19), &mut Map::new(), false)
                .unwrap_err(),
            "INVALID_DATA: orphan order facts"
        );
    }

    #[test]
    fn invalid_order_evidence_fails_without_mutating_payload() {
        for payload in [
            json!({"orders": [{"price": false, "audit_evidence": {"invalid_fields": []}}]}),
            json!({"orders": [{"native": 0, "audit_evidence": {"unrecognized_fields": false}}]}),
        ] {
            let original = payload.clone();
            assert!(encode_orders(19, payload.as_object().unwrap()).is_err());
            assert_eq!(payload, original);
        }
    }

    #[test]
    fn absent_and_invalid_symbol_nodes_do_not_manufacture_order_collections() {
        let batch = encode_orders(
            19,
            json!({"symbol_results": {"missing": {}, "bad": null}})
                .as_object()
                .unwrap(),
        )
        .unwrap();
        assert!(batch.0.is_empty() && batch.1.is_empty());
    }
}
