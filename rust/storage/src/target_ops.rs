//! 目标权重快照及明细的事务写入和显式读取。

use rusqlite::{params, Connection, OptionalExtension, Row};
use serde_json::{json, Map, Value};
use std::path::Path;

use crate::business_facts_ops::encode;
use crate::error::AnalysisError;
use crate::writer_scope;

const ROLES: [&str; 2] = ["raw_weights", "normalized_weights"];
const HEAD_COLUMNS: &str = "id,portfolio_id,account_id,raw_weights_kind,normalized_weights_kind,weights_metadata,source,execution_id,calculated_at_us,target_mode";

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
        "target_mode": row.get::<_, String>("target_mode")?,
        "id": row.get::<_, i64>("id")?,
        "portfolio_id": row.get::<_, i64>("portfolio_id")?,
        "account_id": row.get::<_, Option<i64>>("account_id")?,
        "raw_weights_kind": row.get::<_, String>("raw_weights_kind")?,
        "normalized_weights_kind": row.get::<_, String>("normalized_weights_kind")?,
        "weights_metadata": json_column(row, "weights_metadata")?,
        "source": row.get::<_, String>("source")?,
        "execution_id": row.get::<_, Option<String>>("execution_id")?,
        "calculated_at": crate::time_us::read(row, "calculated_at_us")?,
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
    let mode = match request.get("target_mode") {
        None => "weight",
        Some(Value::String(mode)) if matches!(mode.as_str(), "weight" | "quantity") => mode,
        _ => return Err(invalid("invalid target_mode")),
    };
    if !matches!(source, "manual" | "execution" | "signal") {
        return Err(invalid("目标权重快照来源无效"));
    }
    let calculated_at_us =
        crate::time_us::parse(text(request, "calculated_at")?).map_err(invalid)?;
    let calculated_at = crate::time_us::format(calculated_at_us).map_err(invalid)?;
    let execution_id = optional_text(request, "execution_id")?;
    let (columns, mut rows) = encode_snapshot_weights(0, request)?;
    connection.execute(
        "INSERT INTO target_weight_snapshot(portfolio_id,account_id,raw_weights_kind,normalized_weights_kind,weights_metadata,source,execution_id,calculated_at_us,target_mode) VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9)",
        params![portfolio_id, account_id, text(&columns, "raw_weights_kind")?, text(&columns, "normalized_weights_kind")?, columns["weights_metadata"].to_string(), source, execution_id, calculated_at_us, mode],
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
    let mut view = json!({"target_mode": mode,"id": id, "portfolio_id": portfolio_id, "account_id": account_id,
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

fn execution_target_snapshot(
    connection: &Connection,
    execution_id: &str,
) -> Result<Option<Value>, AnalysisError> {
    let stored = connection
        .query_row(
            &format!("SELECT {HEAD_COLUMNS} FROM target_weight_snapshot WHERE execution_id=?1"),
            [execution_id],
            head,
        )
        .optional()
        .map_err(sql)?;
    Ok(views(connection, stored.into_iter().collect())?.pop())
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

fn sizing_row_ready(row: &Value, mode: &str) -> bool {
    if row["status"] != "SIZED"
        || row
            .get("sizing_mode")
            .and_then(Value::as_str)
            .unwrap_or("weight")
            != mode
        || !row["reason_code"].is_string()
        || !row["target_quantity"].as_f64().is_some_and(f64::is_finite)
    {
        return false;
    }
    let numeric_fields = [
        "account_weight",
        "equity",
        "reference_price",
        "unit_multiplier",
        "unit_notional",
        "target_notional",
        "raw_quantity",
        "current_quantity",
        "quantity_step",
        "min_quantity",
        "min_notional",
    ];
    let finite = numeric_fields.iter().all(|field| {
        row.get(*field)
            .is_none_or(|value| value.is_null() || value.as_f64().is_some_and(f64::is_finite))
    });
    // Python validates conversion payloads; ambiguous shapes must retain fallback history.
    let conversion_ready = row.get("value_conversion").is_none_or(|value| {
        value.is_null()
            || (value.is_object()
                && ["price_currency", "value_currency"]
                    .iter()
                    .all(|key| value[*key].as_str().is_some_and(|s| !s.is_empty()))
                && value["rate"]
                    .as_f64()
                    .is_some_and(|rate| rate.is_finite() && rate > 0.0)
                && value.get("reference").is_none_or(Value::is_object))
    });
    finite && conversion_ready
}

fn sizing_complete(snapshot: &Value, evidence: &Value) -> bool {
    if snapshot["source"] != "execution" || evidence["record_id"].is_null() {
        return false;
    }
    let Some(weights) = snapshot["normalized_weights"].as_object() else {
        return false;
    };
    let Some(rows) = evidence["sizings"].as_object() else {
        return false;
    };
    if rows.is_empty() && !weights.is_empty() {
        return false;
    }
    let mode = snapshot["target_mode"].as_str().unwrap_or("weight");
    weights
        .iter()
        .all(|(key, value)| value.as_f64().is_some_and(f64::is_finite) && rows.contains_key(key))
        && rows.values().all(|row| sizing_row_ready(row, mode))
        && snapshot.get("audit_evidence").is_none()
}

fn account_bundle(connection: &Connection, snapshot: Value) -> Result<Value, AnalysisError> {
    if snapshot.is_null() {
        return Ok(json!({"snapshot":null,"previous":[],"executions":{}}));
    }
    let account_id = integer(&snapshot, "account_id")?;
    let mut executions = Map::new();
    let mut load_history = snapshot["normalized_weights"]
        .as_object()
        .is_some_and(|weights| !weights.is_empty());
    if let Some(execution_id) = snapshot["execution_id"].as_str() {
        let evidence =
            crate::execution_reads::target_sizing_on(connection, execution_id, Some(account_id))?;
        let inflight = evidence["record_id"].is_null()
            && (!evidence["input_id"].is_null()
                || evidence["artifact_versions"]["target_snapshot"]
                    .as_i64()
                    .is_some_and(|version| version >= 2));
        load_history = !inflight && !sizing_complete(&snapshot, &evidence);
        executions.insert(execution_id.into(), evidence);
    }
    let previous = if load_history {
        let query = format!(
            "SELECT {HEAD_COLUMNS} FROM target_weight_snapshot
            WHERE account_id=?1 AND portfolio_id=?2 AND id<?3 AND target_mode=?4
                AND normalized_weights_kind NOT IN ('null','missing') AND execution_id IS NOT NULL
            ORDER BY id DESC LIMIT 50"
        );
        let mut statement = connection.prepare(&query).map_err(sql)?;
        let heads = statement
            .query_map(
                params![
                    account_id,
                    integer(&snapshot, "portfolio_id")?,
                    integer(&snapshot, "id")?,
                    text(&snapshot, "target_mode")?
                ],
                head,
            )
            .map_err(sql)?
            .collect::<Result<_, _>>()
            .map_err(sql)?;
        views(connection, heads)?
    } else {
        Vec::new()
    };
    for prior in &previous {
        let execution_id = text(prior, "execution_id")?;
        executions.insert(
            execution_id.into(),
            crate::execution_reads::target_sizing_on(connection, execution_id, Some(account_id))?,
        );
    }
    Ok(json!({"snapshot":snapshot,"previous":previous,"executions":executions}))
}

/// 调用方在同一读事务内批量获取目标及冻结换算证据，不重建执行结果。
pub(crate) fn account_evidence_on(
    connection: &Connection,
    pairs: &Value,
) -> Result<Value, AnalysisError> {
    let pairs = pairs
        .as_array()
        .ok_or_else(|| invalid("pairs 必须是数组"))?;
    let mut seen = std::collections::HashSet::new();
    for pair in pairs {
        let account_id = integer(pair, "account_id")?;
        integer(pair, "portfolio_id")?;
        optional_integer(pair, "snapshot_id")?;
        if !seen.insert(account_id) {
            return Err(invalid("pairs 包含重复账户"));
        }
    }
    if pairs.is_empty() {
        return Ok(json!({}));
    }
    let query = format!("WITH pairs AS (SELECT json_extract(value,'$.account_id') AS account_id,
            json_extract(value,'$.portfolio_id') AS portfolio_id,json_extract(value,'$.snapshot_id') AS snapshot_id FROM json_each(?1))
        SELECT {HEAD_COLUMNS} FROM target_weight_snapshot WHERE id IN (
            SELECT COALESCE(p.snapshot_id,(SELECT id FROM target_weight_snapshot
                WHERE account_id=p.account_id AND portfolio_id=p.portfolio_id
                    AND normalized_weights_kind NOT IN ('null','missing') ORDER BY id DESC LIMIT 1)) FROM pairs p)");
    let mut statement = connection.prepare(&query).map_err(sql)?;
    let heads = statement
        .query_map([json!(pairs).to_string()], head)
        .map_err(sql)?
        .collect::<Result<_, _>>()
        .map_err(sql)?;
    let snapshots = views(connection, heads)?;
    let mut result = Map::new();
    for pair in pairs {
        let selected = match optional_integer(pair, "snapshot_id")? {
            Some(id) => Some(
                snapshots
                    .iter()
                    .find(|snapshot| snapshot["id"] == id)
                    .filter(|snapshot| {
                        snapshot["account_id"] == pair["account_id"]
                            && snapshot["portfolio_id"] == pair["portfolio_id"]
                    })
                    .ok_or_else(|| invalid("目标快照不存在或不属于请求账户及组合"))?,
            ),
            None => snapshots.iter().find(|snapshot| {
                snapshot["account_id"] == pair["account_id"]
                    && snapshot["portfolio_id"] == pair["portfolio_id"]
            }),
        };
        result.insert(
            integer(pair, "account_id")?.to_string(),
            account_bundle(connection, selected.cloned().unwrap_or(Value::Null))?,
        );
    }
    Ok(Value::Object(result))
}

fn commit_signal(connection: &Connection, request: &Value) -> Result<Value, AnalysisError> {
    let account_id = integer(request, "account_id")?;
    let portfolio_id = integer(request, "portfolio_id")?;
    let event_id = text(&request["notification"], "id")?;
    let duplicate: bool = connection.query_row(
        "SELECT EXISTS(SELECT 1 FROM notification_event WHERE id=?1 AND account_id=?2 AND event_type='signal.calculated')",
        params![event_id, account_id],
        |row| row.get(0),
    ).map_err(sql)?;
    if duplicate {
        return Ok(Value::Null);
    }
    crate::accounts::ensure_current_on(connection, account_id)?;
    let account =
        crate::accounts::load_on(connection, account_id)?.ok_or_else(|| invalid("账户不存在"))?;
    if account["trade_channel"] != crate::accounts::SIGNAL_CHANNEL
        || account["is_started"] != true
        || account["portfolio_id"] != portfolio_id
        || account["runtime_sync"]["revision"] != request["revision"]
    {
        return Err(invalid("信号账户配置已变化"));
    }
    let at = text(request, "calculated_at")?;
    let mut event = request["notification"].clone();
    if request["raw_weights"].is_object() {
        let snapshot = append_target_snapshot(connection, request)?;
        let assets = crate::asset_ops::write_manual_snapshot(connection, account_id, at, request)?;
        event["context"]["target_snapshot_id"] = snapshot["id"].clone();
        event["context"]["asset_snapshot_id"] = assets["id"].clone();
    }
    crate::notifications::enqueue_in_connection(connection, &event)
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
    if operation == "signal_commit" {
        let connection = writer_scope::open_writer(Path::new(path))?;
        return writer_scope::immediate(&connection, || commit_signal(&connection, request));
    }
    let connection = crate::storage_connection::open_reader(Path::new(path))?;
    connection.execute_batch("BEGIN DEFERRED;").map_err(sql)?;
    match operation {
        "account_evidence" => account_evidence_on(&connection, &request["pairs"]),
        "execution" => Ok(
            execution_target_snapshot(&connection, text(request, "execution_id")?)?
                .unwrap_or(Value::Null),
        ),
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
        connection.execute_batch("CREATE TABLE target_weight_snapshot(target_mode TEXT NOT NULL DEFAULT 'weight',id INTEGER PRIMARY KEY,portfolio_id INTEGER NOT NULL,account_id INTEGER,raw_weights_kind TEXT NOT NULL,normalized_weights_kind TEXT NOT NULL,weights_metadata TEXT NOT NULL,source TEXT NOT NULL,execution_id TEXT UNIQUE,calculated_at_us INTEGER NOT NULL); CREATE TABLE target_weight_snapshot_weight(snapshot_id INTEGER,role TEXT,map_key TEXT,value REAL,audit_evidence TEXT NOT NULL,PRIMARY KEY(snapshot_id,role,map_key)) WITHOUT ROWID;").unwrap();
    }

    fn evidence_schema(connection: &Connection) {
        schema(connection);
        connection
            .execute_batch("DROP TABLE target_weight_snapshot_weight;")
            .unwrap();
        connection
            .execute_batch(include_str!("migrations/structured/business.sql"))
            .unwrap();
        connection.execute_batch("CREATE TABLE executerecord(id INTEGER PRIMARY KEY,account_id INTEGER,
            execution_id TEXT,input_id TEXT,unrelated_json JSON);
            CREATE TABLE execution_artifact(execution_id TEXT,artifact_type TEXT,schema_version INTEGER,content JSON);").unwrap();
    }

    fn evidence_snapshot(
        connection: &Connection,
        account: i64,
        portfolio: i64,
        mode: &str,
        execution: Option<&str>,
    ) -> Value {
        append_target_snapshot(
            connection,
            &json!({"account_id":account,"portfolio_id":portfolio,
            "target_mode":mode,"normalized_weights":{"native":1},"raw_weights":{"native":1},
            "source":if execution.is_some() {"execution"} else {"manual"},"execution_id":execution,
            "calculated_at":"2026-10-23T09:00:00Z"}),
        )
        .unwrap()
    }

    #[test]
    fn evidence_bundle_is_bounded_and_isolated_by_account_portfolio_and_mode() {
        let connection = Connection::open_in_memory().unwrap();
        evidence_schema(&connection);
        for index in 0..53 {
            evidence_snapshot(&connection, 2, 7, "weight", Some(&format!("e{index}")));
        }
        evidence_snapshot(&connection, 2, 8, "weight", Some("other-portfolio"));
        evidence_snapshot(&connection, 3, 7, "weight", Some("other-account"));
        evidence_snapshot(&connection, 2, 7, "quantity", Some("other-mode"));
        let latest = evidence_snapshot(&connection, 2, 7, "weight", None);
        let result = account_evidence_on(
            &connection,
            &json!([
            {"account_id":2,"portfolio_id":7},{"account_id":99,"portfolio_id":7}]),
        )
        .unwrap();
        let bundle = &result["2"];
        assert_eq!(bundle["snapshot"], latest);
        let previous = bundle["previous"].as_array().unwrap();
        assert_eq!(previous.len(), 50);
        assert_eq!(previous[0]["execution_id"], "e52");
        assert_eq!(previous[49]["execution_id"], "e3");
        assert!(previous.iter().all(|snapshot| snapshot["account_id"] == 2
            && snapshot["portfolio_id"] == 7
            && snapshot["target_mode"] == "weight"));
        assert_eq!(bundle["executions"].as_object().unwrap().len(), 50);
        assert_eq!(
            bundle["executions"]["e52"],
            json!({"artifact_versions":{},"record_id":null,
            "input_id":null,"target_context":{},"sizings":{}})
        );
        assert_eq!(
            result["99"],
            json!({"snapshot":null,"previous":[],"executions":{}})
        );
        assert_eq!(
            account_evidence_on(&connection, &json!([])).unwrap(),
            json!({})
        );
        for pairs in [
            json!(null),
            json!([{"account_id":true,"portfolio_id":7}]),
            json!([{"account_id":2,"portfolio_id":7,"snapshot_id":"1"}]),
            json!([{"account_id":2,"portfolio_id":7},{"account_id":2,"portfolio_id":8}]),
        ] {
            assert!(account_evidence_on(&connection, &pairs).is_err());
        }
    }

    #[test]
    fn evidence_pins_snapshot_and_skips_history_only_for_complete_or_inflight_sizing() {
        let connection = Connection::open_in_memory().unwrap();
        evidence_schema(&connection);
        evidence_snapshot(&connection, 2, 7, "weight", Some("older"));
        let frozen = evidence_snapshot(&connection, 2, 7, "weight", Some("ready"));
        let later = evidence_snapshot(&connection, 2, 7, "quantity", None);
        connection
            .execute_batch("INSERT INTO executerecord VALUES(10,2,'ready',NULL,'not-json');")
            .unwrap();
        crate::business_facts_ops::result_symbols::write_result_symbols(
            &connection,
            10,
            &json!({"symbol_results":{"native":{"sizing":{"sizing_mode":"weight","status":"SIZED",
                "reason_code":"COMMON.SIZING.EXACT","target_quantity":2}}}}),
        )
        .unwrap();
        let pairs = json!([{"account_id":2,"portfolio_id":7,"snapshot_id":frozen["id"]}]);
        let ready = account_evidence_on(&connection, &pairs).unwrap();
        assert_eq!(ready["2"]["snapshot"], frozen);
        assert_eq!(ready["2"]["previous"], json!([]));
        assert_eq!(
            ready["2"]["executions"]["ready"]["sizings"]["native"]["target_quantity"],
            2.0
        );
        connection
            .execute_batch("UPDATE execution_symbol_sizing SET target_quantity=NULL;")
            .unwrap();
        assert_eq!(
            account_evidence_on(&connection, &pairs).unwrap()["2"]["previous"]
                .as_array()
                .unwrap()
                .len(),
            1
        );
        connection
            .execute_batch(
                "UPDATE execution_symbol_sizing SET target_quantity=2,status='UNAVAILABLE';",
            )
            .unwrap();
        assert_eq!(
            account_evidence_on(&connection, &pairs).unwrap()["2"]["previous"]
                .as_array()
                .unwrap()
                .len(),
            1
        );
        connection
            .execute_batch("UPDATE execution_symbol_sizing SET status='SIZED',map_key='alias';")
            .unwrap();
        assert_eq!(
            account_evidence_on(&connection, &pairs).unwrap()["2"]["previous"]
                .as_array()
                .unwrap()
                .len(),
            1
        );
        assert_eq!(
            account_evidence_on(&connection, &json!([{"account_id":2,"portfolio_id":7}])).unwrap()
                ["2"]["snapshot"],
            later
        );
        for (account_id, portfolio_id) in [(3, 7), (2, 8), (2, 7)] {
            let id = if account_id == 2 && portfolio_id == 7 {
                999
            } else {
                frozen["id"].as_i64().unwrap()
            };
            assert!(account_evidence_on(
                &connection,
                &json!([{"account_id":account_id,"portfolio_id":portfolio_id,"snapshot_id":id}])
            )
            .is_err());
        }
        let inflight = evidence_snapshot(&connection, 2, 7, "weight", Some("inflight"));
        connection
            .execute_batch(
                "INSERT INTO execution_artifact VALUES('inflight','target_snapshot',2,'{}');",
            )
            .unwrap();
        let bundle =
            account_evidence_on(&connection, &json!([{"account_id":2,"portfolio_id":7}])).unwrap();
        assert_eq!(bundle["2"]["snapshot"], inflight);
        assert_eq!(bundle["2"]["previous"], json!([]));
        assert!(bundle["2"]["executions"]["inflight"]["record_id"].is_null());
    }

    #[test]
    fn sizing_readiness_rejects_invalid_numbers_and_conversion() {
        let mut snapshot =
            json!({"source":"execution","target_mode":"weight","normalized_weights":{"native":1}});
        let mut evidence = json!({"record_id":1,"sizings":{"native":{"status":"SIZED","reason_code":"exact","target_quantity":0}}});
        assert!(sizing_complete(&snapshot, &evidence));
        evidence["sizings"]["native"]["equity"] = json!("invalid");
        assert!(!sizing_complete(&snapshot, &evidence));
        evidence["sizings"]["native"]["equity"] = Value::Null;
        evidence["sizings"]["native"]["value_conversion"] = json!({"rate":0});
        assert!(!sizing_complete(&snapshot, &evidence));
        evidence["sizings"]["native"]["value_conversion"] = Value::Null;
        snapshot["normalized_weights"]["missing"] = json!(1);
        assert!(!sizing_complete(&snapshot, &evidence));
        snapshot["normalized_weights"] = json!({});
        evidence["sizings"] = json!({});
        assert!(sizing_complete(&snapshot, &evidence));
        evidence["sizings"] = json!({"leftover":{"status":"UNAVAILABLE","reason_code":"missing","target_quantity":null}});
        assert!(!sizing_complete(&snapshot, &evidence));
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
            json!({"portfolio_id": 7,"account_id": 2,"raw_weights":{"native": 0.5},"normalized_weights":{"native":1.0},"source":"execution","execution_id":"e1","calculated_at":"2026-10-23T09:00:00.000000Z"}),
            json!({"portfolio_id": 8,"account_id": 2,"raw_weights":{},"normalized_weights":{"other":1.0},"source":"execution","execution_id":"e2","calculated_at":"2026-10-23T09:00:00.000000Z"}),
            json!({"portfolio_id": 7,"account_id": 2,"raw_weights":{},"normalized_weights":null,"source":"manual","calculated_at":"2026-10-23T09:00:00.000000Z"}),
            json!({"portfolio_id": 7,"account_id": 2,"raw_weights":null,"normalized_weights":{},"source":"execution","execution_id":"e3","calculated_at":"2026-10-23T09:00:00.000000Z"}),
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
    fn execution_lookup_keeps_frozen_account_identity_and_weights() {
        let connection = Connection::open_in_memory().unwrap();
        schema(&connection);
        let request = json!({"portfolio_id":7,"account_id":2,"raw_weights":{"native":0.25},
            "normalized_weights":{"native":0.5},"source":"execution","execution_id":"frozen",
            "calculated_at":"2026-10-23T09:00:00.000000Z"});
        let saved = append_target_snapshot(&connection, &request).unwrap();
        append_target_snapshot(
            &connection,
            &json!({"portfolio_id":8,"account_id":3,
            "raw_weights":{},"normalized_weights":{},"source":"execution","execution_id":"other",
            "calculated_at":"2026-10-23T10:00:00.000000Z"}),
        )
        .unwrap();
        assert_eq!(
            execution_target_snapshot(&connection, "frozen").unwrap(),
            Some(saved)
        );
        assert!(execution_target_snapshot(&connection, "absent")
            .unwrap()
            .is_none());
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
                "execution_id":format!("e{index}"),"calculated_at":"2026-10-23T09:00:00.000000Z"});
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
            "raw_weights":{"native":-0.0},"source":"manual","calculated_at":"2026-10-23T09:00:00.000000Z"}),
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
                &json!({"portfolio_id":7,"raw_weights":{"x":1},"source":"manual","calculated_at":"2026-10-23T09:00:00.000000Z"}),
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
    #[test]
    fn quantity_mode_and_fractional_target_roundtrip() {
        let connection = Connection::open_in_memory().unwrap();
        schema(&connection);
        let saved = append_target_snapshot(&connection, &json!({"portfolio_id":7,"account_id":2,
            "target_mode":"quantity","raw_weights":{"XAUUSD":0.015},"normalized_weights":{"XAUUSD":0.015},
            "source":"execution","execution_id":"quantity","calculated_at":"2026-11-06T00:00:00Z"})).unwrap();
        assert_eq!(saved["target_mode"], "quantity");
        let restored = execution_target_snapshot(&connection, "quantity")
            .unwrap()
            .unwrap();
        assert_eq!(restored["target_mode"], "quantity");
        assert_eq!(restored["raw_weights"]["XAUUSD"], 0.015);
    }
}
