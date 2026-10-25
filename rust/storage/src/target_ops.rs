//! 目标权重快照及明细的事务写入和显式读取。

use rusqlite::{params, Connection, OptionalExtension, Row};
use serde_json::{json, Map, Value};
use std::path::Path;

use crate::business_facts_ops::encode;
use crate::error::AnalysisError;
use crate::writer_scope;

const ROLES: [&str; 2] = ["raw_weights", "normalized_weights"];
const HEAD_COLUMNS: &str = "id,portfolio_id,account_id,raw_weights_kind,normalized_weights_kind,weights_metadata,source,execution_id,calculated_at";

fn invalid(message: impl Into<String>) -> AnalysisError {
    AnalysisError::new("TARGET_INVALID", message)
}

fn sql(error: rusqlite::Error) -> AnalysisError {
    AnalysisError::new("TARGET_SQL", error.to_string())
}

fn integer(request: &Value, key: &str) -> Result<i64, AnalysisError> {
    request[key]
        .as_i64()
        .ok_or_else(|| invalid(format!("缺少整数参数 {key}")))
}

fn text<'a>(request: &'a Value, key: &str) -> Result<&'a str, AnalysisError> {
    request[key]
        .as_str()
        .ok_or_else(|| invalid(format!("缺少文本参数 {key}")))
}

fn optional_integer(request: &Value, key: &str) -> Result<Option<i64>, AnalysisError> {
    match request.get(key) {
        None | Some(Value::Null) => Ok(None),
        Some(_) => integer(request, key).map(Some),
    }
}

fn optional_text<'a>(request: &'a Value, key: &str) -> Result<Option<&'a str>, AnalysisError> {
    match request.get(key) {
        None | Some(Value::Null) => Ok(None),
        Some(_) => text(request, key).map(Some),
    }
}

/// 历史迁移和实时写入共用编码，异常值只进入审计证据。
pub(crate) fn encode_snapshot_weights(
    snapshot_id: i64,
    payload: &Value,
) -> Result<(Value, Vec<Value>), AnalysisError> {
    let payload = payload
        .as_object()
        .ok_or_else(|| invalid("目标权重载荷必须是对象"))?;
    let mut head = Map::new();
    let mut metadata = Map::new();
    let mut rows = Vec::new();
    for role in ROLES {
        let kind = match payload.get(role) {
            None => "missing",
            Some(Value::Null) => "null",
            Some(Value::Object(weights)) => {
                for (key, value) in weights {
                    let valid = encode::valid(value, "number");
                    rows.push(json!({
                        "snapshot_id": snapshot_id, "role": role, "map_key": key,
                        "value": if valid { value.clone() } else { Value::Null },
                        "audit_evidence": if valid { json!({}) } else {
                            json!({"invalid_fields": {"value": value}})
                        },
                    }));
                }
                "mapping"
            }
            Some(value) => {
                metadata.insert(
                    role.into(),
                    json!({"columns": {"value": value}, "audit_evidence": {}}),
                );
                "invalid"
            }
        };
        head.insert(format!("{role}_kind"), json!(kind));
    }
    head.insert("weights_metadata".into(), Value::Object(metadata));
    Ok((Value::Object(head), rows))
}

/// 保留缺失、空值、空映射、异常容器及未规范化的品种键。
pub(crate) fn decode_snapshot_weights(
    head: &Value,
    rows: &[Value],
) -> Result<Map<String, Value>, AnalysisError> {
    let mut result = Map::new();
    let mut invalid_values = Map::<String, Value>::new();
    for role in ROLES {
        let value = match head[format!("{role}_kind")].as_str() {
            Some("missing") => continue,
            Some("null") => Value::Null,
            Some("invalid") => head["weights_metadata"][role]["columns"]
                .get("value")
                .cloned()
                .ok_or_else(|| invalid("缺少目标权重异常证据"))?,
            Some("mapping") => {
                let mut weights = Map::new();
                let mut ordered: Vec<_> = rows.iter().filter(|row| row["role"] == role).collect();
                ordered.sort_by_key(|row| row["map_key"].as_str());
                for row in ordered {
                    let key = text(row, "map_key")?;
                    weights.insert(key.into(), row["value"].clone());
                    let evidence = row["audit_evidence"]
                        .as_object()
                        .ok_or_else(|| invalid("目标权重审计证据必须是对象"))?;
                    if !evidence.is_empty() {
                        invalid_values.entry(role).or_insert_with(|| json!({}))[key] =
                            Value::Object(evidence.clone());
                    }
                }
                Value::Object(weights)
            }
            _ => return Err(invalid("Unknown target weight snapshot container state")),
        };
        result.insert(role.into(), value);
    }
    if !invalid_values.is_empty() {
        result.insert(
            "audit_evidence".into(),
            json!({"invalid_target_values": invalid_values}),
        );
    }
    Ok(result)
}

fn json_column(row: &Row<'_>, name: &str) -> rusqlite::Result<Value> {
    let value: String = row.get(name)?;
    serde_json::from_str(&value).map_err(|error| {
        rusqlite::Error::FromSqlConversionFailure(
            row.as_ref().column_index(name).unwrap_or(0),
            rusqlite::types::Type::Text,
            Box::new(error),
        )
    })
}

fn head(row: &Row<'_>) -> rusqlite::Result<Value> {
    Ok(json!({
        "id": row.get::<_, i64>("id")?,
        "portfolio_id": row.get::<_, i64>("portfolio_id")?,
        "account_id": row.get::<_, Option<i64>>("account_id")?,
        "raw_weights_kind": row.get::<_, String>("raw_weights_kind")?,
        "normalized_weights_kind": row.get::<_, String>("normalized_weights_kind")?,
        "weights_metadata": json_column(row, "weights_metadata")?,
        "source": row.get::<_, String>("source")?,
        "execution_id": row.get::<_, Option<String>>("execution_id")?,
        "calculated_at": row.get::<_, String>("calculated_at")?,
    }))
}

fn snapshot_view(mut head: Value, rows: &[Value]) -> Result<Value, AnalysisError> {
    let weights = decode_snapshot_weights(&head, rows)?;
    let view = head.as_object_mut().expect("snapshot head");
    view.remove("raw_weights_kind");
    view.remove("normalized_weights_kind");
    view.remove("weights_metadata");
    view.extend(weights);
    Ok(head)
}

fn views(connection: &Connection, heads: Vec<Value>) -> Result<Vec<Value>, AnalysisError> {
    if heads.is_empty() {
        return Ok(Vec::new());
    }
    let ids = Value::Array(heads.iter().map(|head| head["id"].clone()).collect()).to_string();
    let mut statement = connection
        .prepare("SELECT snapshot_id,role,map_key,value,audit_evidence FROM target_weight_snapshot_weight WHERE snapshot_id IN (SELECT value FROM json_each(?1)) ORDER BY map_key")
        .map_err(sql)?;
    let rows = statement
        .query_map([ids], |row| {
            Ok(json!({
                "snapshot_id": row.get::<_, i64>(0)?,
                "role": row.get::<_, String>(1)?,
                "map_key": row.get::<_, String>(2)?,
                "value": row.get::<_, Option<f64>>(3)?,
                "audit_evidence": json_column(row, "audit_evidence")?,
            }))
        })
        .map_err(sql)?
        .collect::<Result<Vec<_>, _>>()
        .map_err(sql)?;
    heads
        .into_iter()
        .map(|head| {
            let children: Vec<_> = rows
                .iter()
                .filter(|row| row["snapshot_id"] == head["id"])
                .cloned()
                .collect();
            snapshot_view(head, &children)
        })
        .collect()
}

/// 复用调用方事务；组合试跑与执行终态可以一起保存快照及明细。
pub(crate) fn append_target_snapshot(
    connection: &Connection,
    request: &Value,
) -> Result<Value, AnalysisError> {
    let portfolio_id = integer(request, "portfolio_id")?;
    let account_id = optional_integer(request, "account_id")?;
    let source = text(request, "source")?;
    if !matches!(source, "manual" | "execution") {
        return Err(invalid("目标权重快照来源无效"));
    }
    let calculated_at = text(request, "calculated_at")?;
    let execution_id = optional_text(request, "execution_id")?;
    let (columns, mut rows) = encode_snapshot_weights(0, request)?;
    connection.execute(
        "INSERT INTO target_weight_snapshot(portfolio_id,account_id,raw_weights_kind,normalized_weights_kind,weights_metadata,source,execution_id,calculated_at) VALUES (?1,?2,?3,?4,?5,?6,?7,?8)",
        params![portfolio_id, account_id, text(&columns, "raw_weights_kind")?, text(&columns, "normalized_weights_kind")?, columns["weights_metadata"].to_string(), source, execution_id, calculated_at],
    ).map_err(sql)?;
    let id = connection.last_insert_rowid();
    let mut statement = connection.prepare_cached("INSERT INTO target_weight_snapshot_weight(snapshot_id,role,map_key,value,audit_evidence) VALUES (?1,?2,?3,?4,?5)").map_err(sql)?;
    for row in &mut rows {
        row["snapshot_id"] = json!(id);
        row["value"] = json!(row["value"].as_f64());
        statement
            .execute(params![
                id,
                text(row, "role")?,
                text(row, "map_key")?,
                row["value"].as_f64(),
                row["audit_evidence"].to_string()
            ])
            .map_err(sql)?;
    }
    let mut view = json!({"id": id, "portfolio_id": portfolio_id, "account_id": account_id,
        "source": source, "execution_id": execution_id, "calculated_at": calculated_at});
    view.as_object_mut()
        .expect("snapshot identity")
        .extend(decode_snapshot_weights(&columns, &rows)?);
    Ok(view)
}

fn latest(
    connection: &Connection,
    condition: &str,
    parameters: &[i64],
) -> Result<Option<Value>, AnalysisError> {
    let query = format!("SELECT {HEAD_COLUMNS} FROM target_weight_snapshot WHERE {condition} ORDER BY id DESC LIMIT 1");
    let head = connection
        .query_row(&query, rusqlite::params_from_iter(parameters), head)
        .optional()
        .map_err(sql)?;
    Ok(views(connection, head.into_iter().collect())?.pop())
}

pub(crate) fn latest_portfolio_target_snapshot(
    connection: &Connection,
    portfolio_id: i64,
) -> Result<Option<Value>, AnalysisError> {
    latest(
        connection,
        "portfolio_id=?1 AND raw_weights_kind NOT IN ('null','missing')",
        &[portfolio_id],
    )
}

pub(crate) fn latest_account_target_snapshot(
    connection: &Connection,
    account_id: i64,
    portfolio_id: i64,
) -> Result<Option<Value>, AnalysisError> {
    latest(
        connection,
        "account_id=?1 AND portfolio_id=?2 AND normalized_weights_kind NOT IN ('null','missing')",
        &[account_id, portfolio_id],
    )
}

pub(crate) fn previous_account_target_snapshots(
    connection: &Connection,
    account_id: i64,
    portfolio_id: i64,
    before_id: i64,
    limit: i64,
) -> Result<Vec<Value>, AnalysisError> {
    let query = format!("SELECT {HEAD_COLUMNS} FROM target_weight_snapshot WHERE account_id=?1 AND portfolio_id=?2 AND normalized_weights_kind NOT IN ('null','missing') AND execution_id IS NOT NULL AND id<?3 ORDER BY id DESC LIMIT ?4");
    let mut statement = connection.prepare(&query).map_err(sql)?;
    let heads = statement
        .query_map(params![account_id, portfolio_id, before_id, limit], head)
        .map_err(sql)?
        .collect::<Result<_, _>>()
        .map_err(sql)?;
    views(connection, heads)
}

/// 独立追加拥有一次写事务；读取在同一个只读快照内加载头和明细。
pub fn target_operation(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let operation = text(request, "op")?;
    if operation == "append" {
        let connection = writer_scope::open_writer(Path::new(path))?;
        return writer_scope::immediate(&connection, || {
            append_target_snapshot(&connection, request)
        });
    }
    let connection = crate::storage_connection::open_reader(Path::new(path))?;
    connection.execute_batch("BEGIN DEFERRED;").map_err(sql)?;
    match operation {
        "latest_account" => Ok(latest_account_target_snapshot(
            &connection,
            integer(request, "account_id")?,
            integer(request, "portfolio_id")?,
        )?
        .unwrap_or(Value::Null)),
        "previous_account" => Ok(Value::Array(previous_account_target_snapshots(
            &connection,
            integer(request, "account_id")?,
            integer(request, "portfolio_id")?,
            integer(request, "before_id")?,
            optional_integer(request, "limit")?.unwrap_or(50),
        )?)),
        _ => Err(invalid("未知目标权重快照操作")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn schema(connection: &Connection) {
        connection.execute_batch("CREATE TABLE target_weight_snapshot(id INTEGER PRIMARY KEY,portfolio_id INTEGER NOT NULL,account_id INTEGER,raw_weights_kind TEXT NOT NULL,normalized_weights_kind TEXT NOT NULL,weights_metadata TEXT NOT NULL,source TEXT NOT NULL,execution_id TEXT UNIQUE,calculated_at TEXT NOT NULL); CREATE TABLE target_weight_snapshot_weight(snapshot_id INTEGER,role TEXT,map_key TEXT,value REAL,audit_evidence TEXT NOT NULL,PRIMARY KEY(snapshot_id,role,map_key)) WITHOUT ROWID;").unwrap();
    }

    #[test]
    fn codec_keeps_container_states_native_keys_and_invalid_evidence() {
        for payload in [
            json!({}),
            json!({"raw_weights": null}),
            json!({"raw_weights": {}}),
            json!({"raw_weights": [1,2]}),
        ] {
            let (head, rows) = encode_snapshot_weights(1, &payload).unwrap();
            assert_eq!(
                Value::Object(decode_snapshot_weights(&head, &rows).unwrap()),
                payload
            );
        }
        let payload = json!({"raw_weights": {"rb2610": 0.25, " SHFE.rb2610 ": true, "big": 9007199254740993_u64, "nil": null}, "normalized_weights": {"bad": [1,2], "nf": {"invalid_number": "inf"}}});
        let (head, rows) = encode_snapshot_weights(1, &payload).unwrap();
        let decoded = decode_snapshot_weights(&head, &rows).unwrap();
        assert_eq!(decoded["raw_weights"]["rb2610"], 0.25);
        assert!(decoded["raw_weights"][" SHFE.rb2610 "].is_null());
        assert!(decoded["raw_weights"]["big"].is_null());
        assert_eq!(
            decoded["audit_evidence"]["invalid_target_values"]["raw_weights"][" SHFE.rb2610 "]
                ["invalid_fields"]["value"],
            true
        );
        assert_eq!(
            decoded["audit_evidence"]["invalid_target_values"]["normalized_weights"]["nf"]
                ["invalid_fields"]["value"],
            json!({"invalid_number":"inf"})
        );
    }

    #[test]
    fn latest_and_history_use_account_portfolio_and_eligible_role() {
        let connection = Connection::open_in_memory().unwrap();
        schema(&connection);
        for request in [
            json!({"portfolio_id": 7,"account_id": 2,"raw_weights":{"native": 0.5},"normalized_weights":{"native":1.0},"source":"execution","execution_id":"e1","calculated_at":"t"}),
            json!({"portfolio_id": 8,"account_id": 2,"raw_weights":{},"normalized_weights":{"other":1.0},"source":"execution","execution_id":"e2","calculated_at":"t"}),
            json!({"portfolio_id": 7,"account_id": 2,"raw_weights":{},"normalized_weights":null,"source":"manual","calculated_at":"t"}),
            json!({"portfolio_id": 7,"account_id": 2,"raw_weights":null,"normalized_weights":{},"source":"execution","execution_id":"e3","calculated_at":"t"}),
        ] {
            append_target_snapshot(&connection, &request).unwrap();
        }
        assert_eq!(
            latest_portfolio_target_snapshot(&connection, 7)
                .unwrap()
                .unwrap()["id"],
            3
        );
        assert_eq!(
            latest_account_target_snapshot(&connection, 2, 7)
                .unwrap()
                .unwrap()["id"],
            4
        );
        let previous = previous_account_target_snapshots(&connection, 2, 7, 4, 50).unwrap();
        assert_eq!(previous.len(), 1);
        assert_eq!(previous[0]["execution_id"], "e1");
        assert_eq!(
            latest_account_target_snapshot(&connection, 2, 7)
                .unwrap()
                .unwrap()["normalized_weights"],
            json!({})
        );
        assert_eq!(
            latest_account_target_snapshot(&connection, 99, 7)
                .unwrap()
                .unwrap_or(Value::Null),
            Value::Null
        );
    }

    #[test]
    fn appended_view_matches_persisted_view_without_storage_columns() {
        let connection = Connection::open_in_memory().unwrap();
        schema(&connection);
        for (index, payload) in [
            json!({}),
            json!({"raw_weights":null,"normalized_weights":null}),
            json!({"raw_weights":{},"normalized_weights":{}}),
            json!({"raw_weights":{" native ":1,"invalid":true},"normalized_weights":[1,2]}),
            json!({"raw_weights":[1,2],"normalized_weights":{"native":0.0}}),
        ]
        .into_iter()
        .enumerate()
        {
            let mut request = json!({"portfolio_id":7,"account_id":2,"source":"execution",
                "execution_id":format!("e{index}"),"calculated_at":"t"});
            request
                .as_object_mut()
                .unwrap()
                .extend(payload.as_object().unwrap().clone());
            let saved = append_target_snapshot(&connection, &request).unwrap();
            let stored = connection
                .query_row(
                    &format!("SELECT {HEAD_COLUMNS} FROM target_weight_snapshot WHERE id=?1"),
                    [saved["id"].as_i64().unwrap()],
                    head,
                )
                .unwrap();
            assert_eq!(
                views(&connection, vec![stored]).unwrap(),
                vec![saved.clone()]
            );
            for key in [
                "raw_weights_kind",
                "normalized_weights_kind",
                "weights_metadata",
            ] {
                assert!(saved.get(key).is_none());
            }
            if let Some(value) = saved["raw_weights"].get(" native ") {
                assert_eq!(value.to_string(), "1.0");
                assert_eq!(
                    saved["audit_evidence"]["invalid_target_values"]["raw_weights"]["invalid"]
                        ["invalid_fields"]["value"],
                    true
                );
            }
        }
    }

    #[test]
    fn negative_zero_keeps_append_and_sqlite_read_values() {
        let connection = Connection::open_in_memory().unwrap();
        schema(&connection);
        let saved = append_target_snapshot(
            &connection,
            &json!({"portfolio_id":7,
            "raw_weights":{"native":-0.0},"source":"manual","calculated_at":"t"}),
        )
        .unwrap();
        let stored = latest_portfolio_target_snapshot(&connection, 7)
            .unwrap()
            .unwrap();
        assert_eq!(saved["raw_weights"]["native"].to_string(), "-0.0");
        assert_eq!(stored["raw_weights"]["native"].to_string(), "0.0");
    }

    #[test]
    fn child_failure_rolls_back_head_and_caller_changes() {
        let connection = Connection::open_in_memory().unwrap();
        schema(&connection);
        connection.execute_batch("CREATE TABLE caller(value TEXT); CREATE TRIGGER reject_target BEFORE INSERT ON target_weight_snapshot_weight BEGIN SELECT RAISE(ABORT,'reject child'); END;").unwrap();
        let result = writer_scope::immediate(&connection, || {
            connection
                .execute("INSERT INTO caller VALUES ('changed')", [])
                .map_err(sql)?;
            append_target_snapshot(
                &connection,
                &json!({"portfolio_id":7,"raw_weights":{"x":1},"source":"manual","calculated_at":"t"}),
            )
        });
        assert!(result.is_err());
        for table in [
            "caller",
            "target_weight_snapshot",
            "target_weight_snapshot_weight",
        ] {
            let count: i64 = connection
                .query_row(&format!("SELECT count(*) FROM {table}"), [], |row| {
                    row.get(0)
                })
                .unwrap();
            assert_eq!(count, 0);
        }
    }
}
