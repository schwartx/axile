//! Operation-level ownership of execution business-fact SQLite transactions.
//!
//! Python passes one complete business request (sanitized payloads, generated
//! identities, created_at); this module owns the whole BEGIN IMMEDIATE
//! transaction: equivalence checks, fact encoding and row inserts. Error
//! messages stay identical to the previous Python ValueError contract.

use std::path::Path;

use rusqlite::{Connection, OptionalExtension};
use serde_json::{json, Map, Value};

use crate::error::AnalysisError;
use crate::storage_connection::ConnectionLease;
use crate::writer_scope;

pub(crate) mod encode;
pub(crate) mod result_extensions;
pub(crate) mod result_head;
pub(crate) mod result_orders;
pub(crate) mod result_symbols;
pub(crate) mod result_trades;
pub(crate) mod summary_refs;

use encode::{
    canonical_input, encode_input_after_assets, encode_input_assets, encode_inputs, py_eq,
};

pub(crate) fn facts_error(message: impl Into<String>) -> AnalysisError {
    AnalysisError::new("BUSINESS_FACTS", message.into())
}

pub(crate) fn sql(error: rusqlite::Error) -> AnalysisError {
    AnalysisError::caused_by("BUSINESS_FACTS_SQL", error)
}

/// 事实替换保留关闭外键约束的既有策略，清理由数据库触发器执行。
fn open_connection(path: &str) -> Result<ConnectionLease, AnalysisError> {
    crate::storage_connection::open_writer(Path::new(path), false)
}

/// 冻结执行前输入与观测,或在同事务内附带独立的审计输入变体。
pub fn freeze_execution_input(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let account_id = request
        .get("account_id")
        .and_then(Value::as_i64)
        .ok_or_else(|| facts_error("缺少账户身份"))?;
    let execution_id = request
        .get("execution_id")
        .and_then(Value::as_str)
        .ok_or_else(|| facts_error("缺少执行标识"))?;
    let input_id = request
        .get("input_id")
        .and_then(Value::as_str)
        .ok_or_else(|| facts_error("缺少输入标识"))?;
    let created_at = request
        .get("created_at")
        .and_then(Value::as_str)
        .ok_or_else(|| facts_error("缺少创建时间"))?;
    let payload = request.get("payload").unwrap_or(&Value::Null);
    let audit_payload = request
        .get("audit_payload")
        .filter(|value| !value.is_null());
    let audit_input_id = request.get("audit_input_id").and_then(Value::as_str);
    if audit_payload.is_some() != audit_input_id.is_some() {
        return Err(facts_error("审计输入变体必须同时携带标识"));
    }
    let before_assets = request
        .get("before_assets")
        .filter(|value| !value.is_null());
    let connection = open_connection(path)?;
    writer_scope::immediate(&connection, || {
        let input_id = persist_input(
            &connection,
            account_id,
            Some(execution_id),
            input_id,
            payload,
            created_at,
            None,
        )?;
        if let Some(before) = before_assets {
            persist_before_assets(&connection, &input_id, before)?;
        }
        let audit_id = match audit_payload {
            None => Value::Null,
            Some(audit) => {
                if same_audit_variant(payload, audit)? {
                    json!(input_id)
                } else {
                    json!(persist_owned_variant(
                        &connection,
                        account_id,
                        execution_id,
                        audit_input_id.expect("checked"),
                        audit,
                        created_at,
                    )?)
                }
            }
        };
        Ok(json!({"input_id": input_id, "audit_id": audit_id}))
    })
}

/// 在 worker 回传前冻结执行后资产观测;没有观测时返回 null。
pub fn freeze_execution_after_assets(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let account_id = request
        .get("account_id")
        .and_then(Value::as_i64)
        .ok_or_else(|| facts_error("缺少账户身份"))?;
    let execution_id = request
        .get("execution_id")
        .and_then(Value::as_str)
        .ok_or_else(|| facts_error("缺少执行标识"))?;
    let payload = request
        .get("payload")
        .and_then(Value::as_object)
        .ok_or_else(|| facts_error("执行后观测必须是对象"))?;
    let connection = open_connection(path)?;
    writer_scope::immediate(&connection, || {
        let input_id = find_execution_input(&connection, account_id, execution_id)?
            .ok_or_else(|| facts_error("执行后资产观测缺少已冻结输入"))?;
        if !payload.contains_key("account_assets") {
            return Ok(Value::Null);
        }
        let asset_id = format!("input:{input_id}:/after_account_assets");
        if let Some(existing) = matching_after_asset(&connection, &input_id, payload)? {
            return Ok(json!({"input_id": input_id, "asset_id": existing}));
        }
        let batch = encode_input_after_assets(&input_id, payload).map_err(facts_error)?;
        crate::asset_ops::write_observation(&connection, batch.as_ref())?;
        Ok(json!({"input_id": input_id, "asset_id": asset_id}))
    })
}

pub(crate) fn persist_input(
    connection: &Connection,
    account_id: i64,
    execution_id: Option<&str>,
    input_id: &str,
    payload: &Value,
    created_at: &str,
    owner_execution_id: Option<&str>,
) -> Result<String, AnalysisError> {
    if let (Some(execution), Some(owner)) = (execution_id, owner_execution_id) {
        if execution != owner {
            return Err(facts_error(
                "Execution input primary identity and owner differ",
            ));
        }
    }
    let owner = owner_execution_id.or(execution_id);
    if let Some(execution) = execution_id {
        if let Some(existing) = find_execution_input(connection, account_id, execution)? {
            if !replay_matches(connection, &existing, payload)? {
                return Err(facts_error(
                    "Execution input replay differs from frozen input",
                ));
            }
            return Ok(existing);
        }
    }
    let batch = encode_inputs(
        input_id,
        payload,
        account_id,
        execution_id,
        created_at,
        owner,
    )
    .map_err(facts_error)?;
    // 写锁内已检查执行标识;唯一约束兜底失败时整个事务回滚。
    write_input(connection, &batch)?;
    Ok(input_id.to_owned())
}

/// 以确定性标识冻结独立审计变体;重放必须与已冻结内容一致。
fn persist_owned_variant(
    connection: &Connection,
    account_id: i64,
    owner_execution_id: &str,
    input_id: &str,
    payload: &Value,
    created_at: &str,
) -> Result<String, AnalysisError> {
    let existing = connection
        .query_row(
            "SELECT id, account_id, execution_id, owner_execution_id FROM execution_input WHERE id=?1",
            [input_id],
            |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, i64>(1)?,
                    row.get::<_, Option<String>>(2)?,
                    row.get::<_, Option<String>>(3)?,
                ))
            },
        )
        .optional()
        .map_err(sql)?;
    if let Some((_, stored_account, stored_execution, stored_owner)) = existing {
        if stored_account != account_id
            || stored_execution.is_some()
            || stored_owner.as_deref() != Some(owner_execution_id)
        {
            return Err(facts_error(
                "Execution input variant identity has a different owner",
            ));
        }
        if !replay_matches(connection, input_id, payload)? {
            return Err(facts_error(
                "Execution input variant replay differs from frozen input",
            ));
        }
        return Ok(input_id.to_owned());
    }
    let batch = encode_inputs(
        input_id,
        payload,
        account_id,
        None,
        created_at,
        Some(owner_execution_id),
    )
    .map_err(facts_error)?;
    write_input(connection, &batch)?;
    Ok(input_id.to_owned())
}

/// 按执行标识定位冻结输入并校验归属;匿名输入的属主由 owner 字段决定。
pub(crate) fn find_execution_input(
    connection: &Connection,
    account_id: i64,
    execution_id: &str,
) -> Result<Option<String>, AnalysisError> {
    let found = connection
        .query_row(
            "SELECT id, account_id, owner_execution_id FROM execution_input WHERE execution_id=?1",
            [execution_id],
            |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, i64>(1)?,
                    row.get::<_, Option<String>>(2)?,
                ))
            },
        )
        .optional()
        .map_err(sql)?;
    found
        .map(|(id, stored_account, owner)| {
            if stored_account != account_id {
                return Err(facts_error("Execution input belongs to another account"));
            }
            if owner.as_deref().unwrap_or(execution_id) != execution_id {
                return Err(facts_error("执行输入属于其他执行"));
            }
            Ok(id)
        })
        .transpose()
}

/// 比较已冻结输入与重放输入:两侧都走同一编码-解码口径。
fn replay_matches(
    connection: &Connection,
    input_id: &str,
    payload: &Value,
) -> Result<bool, AnalysisError> {
    let stored = load_input_value(connection, input_id)?;
    Ok(py_eq(
        &stored,
        &canonical_input(payload).map_err(facts_error)?,
    ))
}

pub(crate) fn load_input_value(
    connection: &Connection,
    input_id: &str,
) -> Result<Value, AnalysisError> {
    let id = json!(input_id);
    let head = crate::business_detail::load(connection, "execution_input", "id", &id)
        .map_err(facts_error)?;
    let head = head.first().ok_or_else(|| facts_error("执行输入不存在"))?;
    let targets =
        crate::business_detail::load(connection, "execution_input_target", "input_id", &id)
            .map_err(facts_error)?;
    let symbols =
        crate::business_detail::load(connection, "execution_input_symbol", "input_id", &id)
            .map_err(facts_error)?;
    let plans = crate::business_detail::load(connection, "execution_input_plan", "input_id", &id)
        .map_err(facts_error)?;
    crate::business_detail::decode_input(head, &targets, &symbols, &plans).map_err(facts_error)
}

fn persist_before_assets(
    connection: &Connection,
    input_id: &str,
    before_assets: &Value,
) -> Result<(), AnalysisError> {
    let existing: i64 = connection
        .query_row(
            "SELECT COUNT(*) FROM business_asset_snapshot WHERE input_id=?1",
            [input_id],
            |row| row.get(0),
        )
        .map_err(sql)?;
    if existing == 0 {
        let batch = encode_input_assets(input_id, &json!({"account_assets": before_assets}))
            .map_err(facts_error)?;
        crate::asset_ops::write_observation(connection, batch.as_ref())?;
    }
    Ok(())
}

/// Python 侧 audit 判定:审计输入的归一化键在主输入上取值一致。
fn same_audit_variant(payload: &Value, audit: &Value) -> Result<bool, AnalysisError> {
    let primary = canonical_input(payload).map_err(facts_error)?;
    let normalized = encode::normalize_targets(audit);
    let audit_canonical = canonical_input(audit).map_err(facts_error)?;
    Ok(normalized.keys().all(|key| {
        py_eq(
            primary.get(key).unwrap_or(&Value::Null),
            audit_canonical.get(key).unwrap_or(&Value::Null),
        )
    }))
}

pub(crate) fn matching_after_asset(
    connection: &Connection,
    input_id: &str,
    payload: &Map<String, Value>,
) -> Result<Option<String>, AnalysisError> {
    let asset_id = format!("input:{input_id}:/after_account_assets");
    let id = json!(asset_id.clone());
    let roots =
        crate::business_detail::load(connection, "business_asset_snapshot", "asset_id", &id)
            .map_err(facts_error)?;
    let Some(root) = roots.into_iter().next() else {
        return Ok(None);
    };
    let positions =
        crate::business_detail::load(connection, "business_asset_position", "asset_id", &id)
            .map_err(facts_error)?;
    let actual = crate::assets::observation_value(&root, &positions).map_err(facts_error)?;
    let incoming = payload
        .get("account_assets")
        .ok_or_else(|| facts_error("已持久化执行后观测，但终态结果缺少 account_assets"))?;
    let right = match incoming {
        Value::Object(map) if !map.contains_key("availability") => {
            encode::business_asset_value(incoming).map_err(facts_error)?
        }
        other => other.clone(),
    };
    if py_eq(&actual, &right) {
        return Ok(Some(asset_id));
    }
    Err(facts_error("终态结果与已持久化执行后资产观测不一致"))
}

/// 使用调用方事务直接写入冻结输入及其目标、名单和计划。
pub(crate) fn write_input(
    connection: &Connection,
    (head, targets, symbols, plans): &encode::InputRows,
) -> Result<(), AnalysisError> {
    let mut statement = connection.prepare_cached(r#"INSERT INTO execution_input (
                "id", "account_id", "execution_id", "owner_execution_id", "created_at", "root_kind",
                "root_value", "channel_type", "execution_timeout", "algorithm", "symbol_algorithms", "trade_rules",
                "futures_close_policy", "feishu_key", "feishu_account", "extra", "long_leverage", "short_leverage",
                "weight_precision", "curr_target_state", "last_target_state", "strategy_target_state", "forbidden_symbols_state", "risk_symbols_state",
                "sizing_context_state", "quantities_state", "target_units_state", "curr_target_count", "last_target_count", "strategy_target_count",
                "quantities_count", "target_units_count", "forbidden_symbols_count", "risk_symbols_count", "audit_evidence"
            ) VALUES (
                ?1, ?2, ?3, ?4, ?5, ?6,
                ?7, ?8, ?9, ?10, ?11, ?12,
                ?13, ?14, ?15, ?16, ?17, ?18,
                ?19, ?20, ?21, ?22, ?23, ?24,
                ?25, ?26, ?27, ?28, ?29, ?30,
                ?31, ?32, ?33, ?34, ?35
            )"#).map_err(sql)?;
    statement
        .execute(rusqlite::params![
            head.get("id").and_then(Value::as_str),
            head.get("account_id").and_then(Value::as_i64),
            head.get("execution_id").and_then(Value::as_str),
            head.get("owner_execution_id").and_then(Value::as_str),
            head.get("created_at").and_then(Value::as_str),
            head.get("root_kind").and_then(Value::as_str),
            encode::json_text(head.get("root_value"))?,
            head.get("channel_type").and_then(Value::as_str),
            head.get("execution_timeout").and_then(Value::as_i64),
            encode::json_text(head.get("algorithm"))?,
            encode::json_text(head.get("symbol_algorithms"))?,
            encode::json_text(head.get("trade_rules"))?,
            encode::json_text(head.get("futures_close_policy"))?,
            encode::json_text(head.get("feishu_key"))?,
            encode::json_text(head.get("feishu_account"))?,
            encode::json_text(head.get("extra"))?,
            head.get("long_leverage").and_then(Value::as_f64),
            head.get("short_leverage").and_then(Value::as_f64),
            head.get("weight_precision").and_then(Value::as_f64),
            head.get("curr_target_state").and_then(Value::as_str),
            head.get("last_target_state").and_then(Value::as_str),
            head.get("strategy_target_state").and_then(Value::as_str),
            head.get("forbidden_symbols_state").and_then(Value::as_str),
            head.get("risk_symbols_state").and_then(Value::as_str),
            head.get("sizing_context_state").and_then(Value::as_str),
            head.get("quantities_state").and_then(Value::as_str),
            head.get("target_units_state").and_then(Value::as_str),
            head.get("curr_target_count").and_then(Value::as_i64),
            head.get("last_target_count").and_then(Value::as_i64),
            head.get("strategy_target_count").and_then(Value::as_i64),
            head.get("quantities_count").and_then(Value::as_i64),
            head.get("target_units_count").and_then(Value::as_i64),
            head.get("forbidden_symbols_count").and_then(Value::as_i64),
            head.get("risk_symbols_count").and_then(Value::as_i64),
            encode::json_text(head.get("audit_evidence"))?,
        ])
        .map_err(sql)?;
    let mut statement = connection.prepare_cached("INSERT INTO execution_input_target (\"input_id\",\"role\",\"map_key\",\"value\",\"audit_evidence\") VALUES (?1,?2,?3,?4,?5)").map_err(sql)?;
    for row in targets {
        statement
            .execute(rusqlite::params![
                row.get("input_id").and_then(Value::as_str),
                row.get("role").and_then(Value::as_str),
                row.get("map_key").and_then(Value::as_str),
                row.get("value").and_then(Value::as_f64),
                encode::json_text(row.get("audit_evidence"))?
            ])
            .map_err(sql)?;
    }
    let mut statement = connection.prepare_cached("INSERT INTO execution_input_symbol (\"input_id\",\"role\",\"ordinal\",\"value\",\"audit_evidence\") VALUES (?1,?2,?3,?4,?5)").map_err(sql)?;
    for row in symbols {
        statement
            .execute(rusqlite::params![
                row.get("input_id").and_then(Value::as_str),
                row.get("role").and_then(Value::as_str),
                row.get("ordinal").and_then(Value::as_i64),
                row.get("value").and_then(Value::as_str),
                encode::json_text(row.get("audit_evidence"))?
            ])
            .map_err(sql)?;
    }
    let mut statement = connection
        .prepare_cached(
            r#"INSERT INTO execution_input_plan (
                "input_id", "symbol", "quantity", "units", "quantity_state", "units_state",
                "audit_evidence"
            ) VALUES (
                ?1, ?2, ?3, ?4, ?5, ?6,
                ?7
            )"#,
        )
        .map_err(sql)?;
    for row in plans {
        statement
            .execute(rusqlite::params![
                row.get("input_id").and_then(Value::as_str),
                row.get("symbol").and_then(Value::as_str),
                row.get("quantity").and_then(Value::as_f64),
                row.get("units").and_then(Value::as_f64),
                row.get("quantity_state").and_then(Value::as_str),
                row.get("units_state").and_then(Value::as_str),
                encode::json_text(row.get("audit_evidence"))?,
            ])
            .map_err(sql)?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn schema() -> Connection {
        let connection = Connection::open_in_memory().unwrap();
        connection
            .execute_batch(
                "CREATE TABLE execution_input(id TEXT PRIMARY KEY,account_id INTEGER,
                 execution_id TEXT UNIQUE,owner_execution_id TEXT,created_at TEXT NOT NULL,
                 root_kind TEXT NOT NULL,root_value JSON,channel_type TEXT,execution_timeout INTEGER,
                 algorithm JSON,symbol_algorithms JSON,trade_rules JSON,futures_close_policy JSON,
                 feishu_key JSON,feishu_account JSON,extra JSON,long_leverage REAL,short_leverage REAL,
                 weight_precision REAL,curr_target_state TEXT NOT NULL,last_target_state TEXT NOT NULL,
                 strategy_target_state TEXT NOT NULL,forbidden_symbols_state TEXT NOT NULL,
                 risk_symbols_state TEXT NOT NULL,sizing_context_state TEXT NOT NULL,
                 quantities_state TEXT NOT NULL,target_units_state TEXT NOT NULL,
                 curr_target_count INTEGER,last_target_count INTEGER,strategy_target_count INTEGER,
                 quantities_count INTEGER,target_units_count INTEGER,forbidden_symbols_count INTEGER,
                 risk_symbols_count INTEGER,audit_evidence JSON NOT NULL);
                 CREATE TABLE execution_input_target(input_id TEXT,role TEXT,map_key TEXT,value REAL,
                 audit_evidence JSON NOT NULL,PRIMARY KEY(input_id,role,map_key)) WITHOUT ROWID;
                 CREATE TABLE execution_input_symbol(input_id TEXT,role TEXT,ordinal INTEGER,value TEXT,
                 audit_evidence JSON NOT NULL,PRIMARY KEY(input_id,role,ordinal));
                 CREATE TABLE execution_input_plan(input_id TEXT,symbol TEXT,quantity REAL,units REAL,
                 quantity_state TEXT NOT NULL,units_state TEXT NOT NULL,audit_evidence JSON NOT NULL,
                 PRIMARY KEY(input_id,symbol)) WITHOUT ROWID;
                 CREATE TABLE business_asset_snapshot(asset_id TEXT PRIMARY KEY,record_id INTEGER,
                 input_id TEXT,account_snapshot_id INTEGER,path TEXT NOT NULL,scope TEXT NOT NULL,
                 ordinal INTEGER NOT NULL,map_key TEXT,payload_kind TEXT NOT NULL,payload_value JSON,
                 audit_evidence JSON NOT NULL,cash_state TEXT NOT NULL,equity_state TEXT NOT NULL,
                 market_state TEXT NOT NULL,positions_kind TEXT NOT NULL,positions_count INTEGER,
                 positions_value JSON,observation_valid INTEGER NOT NULL,available_cash REAL,
                 total_asset REAL,market_value REAL,currency TEXT,update_time TEXT,source TEXT,extra JSON);
                 CREATE TABLE business_asset_position(asset_id TEXT,ordinal INTEGER,payload_kind TEXT NOT NULL,
                 payload_value JSON,audit_evidence JSON NOT NULL,volume_state TEXT NOT NULL,
                 available_volume_state TEXT NOT NULL,market_value_state TEXT NOT NULL,
                 avg_price_state TEXT NOT NULL,symbol TEXT,volume REAL,available_volume REAL,
                 market_value REAL,direction TEXT,avg_price REAL,extra JSON,
                 PRIMARY KEY(asset_id,ordinal));",
            )
            .unwrap();
        connection
    }

    #[test]
    fn freeze_and_replay_round_trip_preserves_canonical_input() {
        let connection = schema();
        let payload = json!({
            "channel_type": "custom", "algorithm": {"name": "twap"},
            "normalized_weights": {"A": 0.6, "B": 0.4},
            "forbidden_symbols": ["C"], "sizing_context": {"long_leverage": 2},
            "quantities": {"A": 3}, "unknown_field": {"deep": [1, 2]},
        });
        let batch = encode_inputs("i1", &payload, 7, Some("e1"), "t", None).unwrap();
        write_input(&connection, &batch).unwrap();
        let stored = load_input_value(&connection, "i1").unwrap();
        assert_eq!(stored["curr_target"], json!({"A": 0.6, "B": 0.4}));
        assert_eq!(stored["forbidden_symbols"], json!(["C"]));
        assert_eq!(stored["sizing_context"]["long_leverage"], json!(2.0));
        assert_eq!(stored["plan"]["quantities"], json!({"A": 3.0}));
        assert!(stored["audit_evidence"]["unrecognized_fields"]["unknown_field"].is_object());
        assert!(replay_matches(&connection, "i1", &payload).unwrap());
        let replay = json!({"channel_type": "custom", "algorithm": {"name": "twap"},
            "normalized_weights": {"A": 0.6, "B": 0.4}, "forbidden_symbols": ["C"],
            "sizing_context": {"long_leverage": 2.0}, "quantities": {"A": 3.0},
            "unknown_field": {"deep": [1, 2]}});
        assert!(replay_matches(&connection, "i1", &replay).unwrap());
        let different = json!({"channel_type": "custom", "algorithm": {"name": "vwap"}});
        assert!(!replay_matches(&connection, "i1", &different).unwrap());
    }

    #[test]
    fn scalar_and_malformed_input_roots_round_trip_and_replay() {
        let connection = schema();
        for (index, payload) in [
            json!(null),
            json!(false),
            json!(0),
            json!("legacy"),
            json!([]),
        ]
        .iter()
        .enumerate()
        {
            let input_id = format!("root-{index}");
            let batch = encode_inputs(&input_id, payload, 7, None, "t", None).unwrap();
            write_input(&connection, &batch).unwrap();
            assert_eq!(load_input_value(&connection, &input_id).unwrap(), *payload);
            assert!(replay_matches(&connection, &input_id, payload).unwrap());
        }
    }

    #[test]
    fn sanitize_masks_credentials_before_freezing() {
        let payload = json!({"account_config": {"secret": 1}, "feishu_key": "live-key",
            "execution_notification_code": "code", "algorithm": "x"});
        let batch = encode_inputs("i1", &payload, 7, Some("e1"), "t", None).unwrap();
        let head = &batch.0;
        assert_eq!(head["feishu_key"], json!("***"));
        assert_eq!(head["algorithm"], json!("x"));
        let decoded = load_input_value_on(&batch);
        assert!(decoded.get("account_config").is_none());
        assert_eq!(decoded["feishu_key"], json!("***"));
    }

    fn load_input_value_on((head, targets, symbols, plans): &encode::InputRows) -> Value {
        crate::business_detail::decode_input(head, targets, symbols, plans).unwrap()
    }

    #[test]
    fn audit_variant_key_rule_matches_python_semantics() {
        let payload = json!({"algorithm": "x", "extra": 1});
        let audit_same = json!({"algorithm": "x", "extra": 1.0});
        assert!(same_audit_variant(&payload, &audit_same).unwrap());
        let audit_differ = json!({"algorithm": "y", "extra": 1});
        assert!(!same_audit_variant(&payload, &audit_differ).unwrap());
    }

    #[test]
    fn invalid_audit_variant_is_not_treated_as_null_input() {
        let audit = json!({"execution_timeout":"invalid", "audit_evidence":{"invalid_fields":[]}});
        assert!(same_audit_variant(&Value::Null, &audit).is_err());
    }

    #[test]
    fn after_asset_replay_reuses_identical_observation() {
        let connection = schema();
        let payload = json!({"algorithm": "x"});
        let batch = encode_inputs("i1", &payload, 7, Some("e1"), "t", None).unwrap();
        write_input(&connection, &batch).unwrap();
        let after = json!({"account_assets": {"total_asset": 100.0, "positions": [
            {"symbol": "A", "volume": 2}]}});
        let observation = encode_input_after_assets("i1", after.as_object().unwrap()).unwrap();
        crate::asset_ops::write_observation(&connection, observation.as_ref()).unwrap();
        let same = matching_after_asset(&connection, "i1", after.as_object().unwrap()).unwrap();
        assert_eq!(same.as_deref(), Some("input:i1:/after_account_assets"));
        let changed = json!({"account_assets": {"total_asset": 101.0, "positions": [
            {"symbol": "A", "volume": 2}]}});
        assert!(matching_after_asset(&connection, "i1", changed.as_object().unwrap()).is_err());
    }
}
