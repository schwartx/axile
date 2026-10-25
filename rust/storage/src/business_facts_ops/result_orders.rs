//! 按来源路径与出现顺序保存订单观察，不合并重复的原生身份。

use rusqlite::Connection;
use serde_json::{json, Map, Value};

use super::encode::{audit_value, join_pointer, store_columns, Rows};
use super::facts_error;
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
    let Value::Object(mut row) = json!({
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
    }) else {
        unreachable!("object literal")
    };
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
    let Value::Object(mut collection) = json!({
        "record_id": record_id, "path": path, "map_key": map_key,
        "ordinal": ordinal,
        "kind": if value.is_array() { "array" } else if value.is_null() { "null" } else { "invalid" },
        "invalid_value": null,
        "item_count": value.as_array().map(Vec::len), "audit_evidence": {}
    }) else {
        unreachable!("object literal")
    };
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
    super::insert_rows(
        connection,
        "execution_order_collection",
        &collections,
        &[
            ("record_id", "integer"),
            ("path", "text"),
            ("ordinal", "integer"),
            ("map_key", "text"),
            ("kind", "text"),
            ("invalid_value", "json"),
            ("item_count", "integer"),
            ("audit_evidence", "json"),
        ],
    )?;
    super::insert_rows(
        connection,
        "execution_order",
        &orders,
        &[
            ("record_id", "integer"),
            ("path", "text"),
            ("ordinal", "integer"),
            ("map_key", "text"),
            ("kind", "text"),
            ("invalid_value", "json"),
            ("order_id", "json"),
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
            ("audit_evidence", "json"),
            ("order_id_state", "text"),
            ("conversion_kind", "text"),
            ("conversion_invalid_value", "json"),
            ("conversion_price_currency", "text"),
            ("conversion_value_currency", "text"),
            ("conversion_rate", "number"),
            ("conversion_reference", "json"),
            ("conversion_evidence", "json"),
        ],
    )?;
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
