//! 资产观测写入及内部读取；终态辅助函数只使用调用方连接。

use std::path::Path;

use rusqlite::{params, Connection, OptionalExtension};
use serde_json::{json, Map, Value};

use crate::business_facts_ops::{encode, facts_error as invalid, insert_batch, sql};
use crate::error::AnalysisError;

fn integer(request: &Value, key: &str) -> Result<i64, AnalysisError> {
    request
        .get(key)
        .and_then(Value::as_i64)
        .ok_or_else(|| invalid(format!("缺少整数参数: {key}")))
}

fn text<'a>(request: &'a Value, key: &str) -> Result<&'a str, AnalysisError> {
    request
        .get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| invalid(format!("缺少字符串参数: {key}")))
}

/// 手工观测头、持仓事实及引用在一个事务内提交。
pub fn asset_snapshot_write(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let account_id = integer(request, "account_id")?;
    let created_at = text(request, "created_at")?;
    if request.get("account_assets").is_none() {
        return Err(invalid("账户资产观测缺少 account_assets"));
    }
    if request
        .get("result")
        .is_some_and(|result| !result.is_null() && !result.is_object())
    {
        return Err(invalid("资产观测上下文必须是对象"));
    }
    let connection = crate::storage_connection::open_writer(Path::new(path), false)?;
    crate::writer_scope::immediate(&connection, || {
        write_manual_snapshot(&connection, account_id, created_at, request)
    })
}

fn write_manual_snapshot(
    connection: &Connection,
    account_id: i64,
    created_at: &str,
    request: &Value,
) -> Result<Value, AnalysisError> {
    let exists: bool = connection
        .query_row(
            "SELECT EXISTS(SELECT 1 FROM account WHERE id=?1)",
            [account_id],
            |row| row.get(0),
        )
        .map_err(sql)?;
    if !exists {
        return Err(AnalysisError::new("ACCOUNT_NOT_FOUND", "账户不存在"));
    }
    connection
        .execute(
            "INSERT INTO account_asset_snapshot(account_id,source,created_at)
             VALUES(?1,'manual',?2)",
            params![account_id, created_at],
        )
        .map_err(sql)?;
    let snapshot_id = connection.last_insert_rowid();
    let batch = encode::encode_snapshot_assets(
        snapshot_id,
        request,
        request.get("result").and_then(Value::as_object),
    )
    .map_err(invalid)?;
    insert_batch(connection, &batch)?;
    let asset_id = format!("snapshot:{snapshot_id}:/account_assets");
    connection
        .execute(
            "UPDATE account_asset_snapshot SET asset_fact_id=?1 WHERE id=?2",
            params![asset_id, snapshot_id],
        )
        .map_err(sql)?;
    Ok(
        json!({"id": snapshot_id, "account_id": account_id, "asset_fact_id": asset_id,
            "source": "manual", "execution_id": null, "created_at": created_at}),
    )
}

/// 结果中的每次观测保留各自标识；根观测可复用 worker 已提交的事实。
pub(crate) fn encode_result_assets(
    record_id: i64,
    payload: &Value,
    root_asset_id: Option<&str>,
) -> Result<encode::FactBatch, String> {
    let mut batch = encode::FactBatch::new();
    let mut roots = Vec::new();
    let mut positions = Vec::new();
    let mut bindings = Vec::new();
    let mut append =
        |path: String, scope: &str, ordinal: usize, map_key: Option<&str>, value: &Value| {
            let linked = if scope == "after" {
                root_asset_id
            } else {
                None
            };
            let asset_id = linked
                .map(str::to_owned)
                .unwrap_or_else(|| format!("record:{record_id}:{path}"));
            bindings.push(
                json!({"record_id": record_id, "path": path, "asset_id": asset_id,
                "scope": scope, "ordinal": ordinal, "map_key": map_key})
                .as_object()
                .expect("object")
                .clone(),
            );
            if linked.is_none() {
                let mut observation = encode::encode_observation(
                    value,
                    &asset_id,
                    &path,
                    scope,
                    None,
                    if scope == "after" {
                        payload.as_object()
                    } else {
                        None
                    },
                )?;
                observation
                    .root
                    .insert("record_id".into(), json!(record_id));
                observation.root.insert("ordinal".into(), json!(ordinal));
                observation.root.insert("map_key".into(), json!(map_key));
                roots.push(observation.root);
                positions.extend(observation.positions);
            }
            Ok::<(), String>(())
        };
    if let Some(assets) = payload.get("account_assets") {
        append("/account_assets".into(), "after", 0, None, assets)?;
    }
    if let Some(symbols) = payload.get("symbol_results").and_then(Value::as_object) {
        for (ordinal, (key, item)) in symbols.iter().enumerate() {
            if let Some(assets) = item.get("account_assets") {
                append(
                    encode::join_pointer(
                        &encode::join_pointer("/symbol_results", key),
                        "account_assets",
                    ),
                    "symbol",
                    ordinal,
                    Some(key),
                    assets,
                )?;
            }
        }
    }
    batch.push("business_asset_snapshot", roots);
    batch.push("business_asset_position", positions);
    batch.push("business_asset_binding", bindings);
    Ok(batch)
}

pub(crate) fn write_result_assets(
    connection: &Connection,
    record_id: i64,
    payload: &Value,
    root_asset_id: Option<&str>,
) -> Result<(), AnalysisError> {
    let batch = encode_result_assets(record_id, payload, root_asset_id).map_err(invalid)?;
    insert_batch(connection, &batch)?;
    validate_result_asset_ownership(connection, record_id)
}

/// 只查询归属标量，供审计在装载大载荷前拒绝越权引用。
pub(crate) fn validate_result_asset_ownership(
    connection: &Connection,
    record_id: i64,
) -> Result<(), AnalysisError> {
    let exists: bool = connection
        .query_row(
            "SELECT EXISTS(SELECT 1 FROM executerecord WHERE id=?1)",
            [record_id],
            |row| row.get(0),
        )
        .map_err(sql)?;
    if !exists {
        return Err(invalid("资产事实所属执行记录不存在"));
    }
    let error: Option<String> = connection.query_row(
        "SELECT error FROM (SELECT CASE
         WHEN a.asset_id IS NULL THEN '资产绑定缺少观测事实'
         WHEN a.record_id IS NOT NULL AND a.record_id IS NOT r.id THEN '资产观测属于其他执行记录'
         WHEN a.record_id IS NOT NULL AND (b.path IS NOT a.path OR b.scope IS NOT a.scope
             OR b.ordinal IS NOT a.ordinal OR b.map_key IS NOT a.map_key)
             THEN '资产绑定与原始观测发生不一致'
         WHEN a.record_id IS NULL AND (i.id IS NULL
             OR (a.input_id IS NOT r.input_id AND a.input_id IS NOT r.result_input_id))
             THEN '资产观测输入与执行记录不匹配'
         WHEN a.record_id IS NULL AND i.account_id IS NOT r.account_id THEN '资产观测属于其他账户'
         WHEN a.record_id IS NULL AND coalesce(i.owner_execution_id,i.execution_id) IS NOT r.execution_id
             THEN '资产观测属于其他执行'
         WHEN a.record_id IS NULL AND i.execution_id IS NOT NULL AND i.execution_id IS NOT r.execution_id
             THEN '资产观测输入执行标识不匹配'
         WHEN a.record_id IS NULL AND (a.path IS NOT '/after_account_assets' OR a.scope IS NOT 'after_pending'
             OR b.path IS NOT '/account_assets' OR b.scope IS NOT 'after')
             THEN '执行前资产不能绑定为执行后结果' END AS error
         FROM business_asset_binding b JOIN executerecord r ON r.id=b.record_id
         LEFT JOIN business_asset_snapshot a ON a.asset_id=b.asset_id
         LEFT JOIN execution_input i ON i.id=a.input_id WHERE b.record_id=?1)
         WHERE error IS NOT NULL LIMIT 1",
        [record_id], |row| row.get(0),
    ).optional().map_err(sql)?;
    error.map_or(Ok(()), |message| Err(invalid(message)))
}

/// 与终态事实共用事务，重复收尾复用同一历史索引。
pub(crate) fn ensure_execution_asset_snapshot(
    connection: &Connection,
    record_id: i64,
) -> Result<Option<Value>, AnalysisError> {
    validate_result_asset_ownership(connection, record_id)?;
    let reference = connection
        .query_row(
            "SELECT a.asset_id FROM business_asset_binding b
             JOIN business_asset_snapshot a ON a.asset_id=b.asset_id
             WHERE b.record_id=?1 AND b.path='/account_assets' AND a.observation_valid IS TRUE",
            [record_id],
            |row| row.get::<_, String>(0),
        )
        .optional()
        .map_err(sql)?;
    let Some(reference) = reference else {
        return Ok(None);
    };
    let (account_id, execution_id, created_at): (i64, Option<String>, String) = connection
        .query_row(
            "SELECT account_id,execution_id,created_at FROM executerecord WHERE id=?1",
            [record_id],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )
        .map_err(sql)?;
    let mut statement = connection
        .prepare(
            "SELECT * FROM account_asset_snapshot WHERE CASE WHEN ?1 IS NOT NULL
         THEN execution_id=?1 ELSE asset_fact_id=?2 END",
        )
        .map_err(sql)?;
    let mut cursor = statement
        .query(params![execution_id, reference])
        .map_err(sql)?;
    if let Some(row) = cursor.next().map_err(sql)? {
        let existing =
            crate::business_detail::decode(row, "account_asset_snapshot").map_err(invalid)?;
        if existing.get("account_id") != Some(&json!(account_id))
            || existing.get("asset_fact_id") != Some(&json!(reference))
            || existing.get("source") != Some(&json!("execution"))
        {
            return Err(invalid("执行资产历史索引与原观测不一致"));
        }
        if cursor.next().map_err(sql)?.is_some() {
            return Err(invalid("执行资产历史索引不唯一"));
        }
        return Ok(Some(Value::Object(existing)));
    }
    connection.execute(
        "INSERT INTO account_asset_snapshot(account_id,asset_fact_id,source,execution_id,created_at)
         VALUES(?1,?2,'execution',?3,?4)",
        params![account_id, reference, execution_id, created_at],
    ).map_err(sql)?;
    Ok(Some(
        json!({"id": connection.last_insert_rowid(), "account_id": account_id,
        "asset_fact_id": reference, "source": "execution", "execution_id": execution_id,
        "created_at": created_at}),
    ))
}

fn observation(
    connection: &Connection,
    root: &Map<String, Value>,
    include_positions: bool,
) -> Result<Value, AnalysisError> {
    if !include_positions {
        let mut scalar = root.clone();
        scalar.insert("payload_kind".into(), json!("object"));
        scalar.insert("positions_kind".into(), json!("absent"));
        return crate::assets::observation_value(&scalar, &[]).map_err(invalid);
    }
    let positions = crate::business_detail::load(
        connection,
        "business_asset_position",
        "asset_id",
        root.get("asset_id")
            .ok_or_else(|| invalid("资产观测缺少标识"))?,
    )
    .map_err(invalid)?;
    crate::assets::observation_value(root, &positions).map_err(invalid)
}

pub(crate) fn read_input_asset_observations(
    connection: &Connection,
    input_id: &str,
) -> Result<Value, AnalysisError> {
    let mut roots = crate::business_detail::load(
        connection,
        "business_asset_snapshot",
        "input_id",
        &json!(input_id),
    )
    .map_err(invalid)?;
    roots.sort_by(|a, b| {
        a.get("ordinal")
            .and_then(Value::as_i64)
            .cmp(&b.get("ordinal").and_then(Value::as_i64))
            .then_with(|| {
                a.get("path")
                    .and_then(Value::as_str)
                    .cmp(&b.get("path").and_then(Value::as_str))
            })
    });
    roots
        .iter()
        .map(|root| {
            Ok(json!({"asset_id": root.get("asset_id"),
        "path": root.get("path"), "scope": root.get("scope"),
        "account_assets": observation(connection,root,true)?}))
        })
        .collect::<Result<Vec<_>, _>>()
        .map(Value::Array)
}

pub fn asset_input_observations(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let input_id = text(request, "input_id")?;
    crate::with_read_snapshot(Path::new(path), |connection| {
        read_input_asset_observations(connection, input_id)
    })
}

pub fn asset_snapshot_latest(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let account_id = integer(request, "account_id")?;
    crate::with_read_snapshot(Path::new(path), |connection| {
        let page =
            crate::assets::page(connection, account_id, 0, 1).map_err(AnalysisError::from)?;
        Ok(page["data"]
            .as_array()
            .and_then(|rows| rows.first())
            .cloned()
            .unwrap_or(Value::Null))
    })
}

pub fn asset_snapshots_read(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let snapshots = request
        .get("snapshots")
        .and_then(Value::as_array)
        .ok_or_else(|| invalid("缺少资产快照列表"))?;
    let include_positions = match request.get("include_positions") {
        None => true,
        Some(Value::Bool(value)) => *value,
        _ => return Err(invalid("include_positions 必须是布尔值")),
    };
    crate::with_read_snapshot(Path::new(path), |connection| {
        let owner_query = format!(
            "SELECT {} FROM account_asset_snapshot s
            JOIN business_asset_snapshot a ON a.asset_id=s.asset_fact_id WHERE s.id=?1",
            crate::assets::OWNER_VALID
        );
        let mut output = Vec::with_capacity(snapshots.len());
        for snapshot in snapshots {
            let id = integer(snapshot, "id")?;
            let heads = crate::business_detail::load(
                connection,
                "account_asset_snapshot",
                "id",
                &json!(id),
            )
            .map_err(invalid)?;
            let head = heads.first().ok_or_else(|| invalid("账户资产观测不存在"))?;
            for key in ["account_id", "asset_fact_id", "execution_id"] {
                if head.get(key) != snapshot.get(key) {
                    return Err(invalid("账户资产观测引用与账户或执行不匹配"));
                }
            }
            let valid: Option<bool> = connection
                .query_row(&owner_query, [id], |row| row.get(0))
                .optional()
                .map_err(sql)?;
            if valid != Some(true) {
                return Err(invalid("账户资产观测引用与账户或执行不匹配"));
            }
            let reference = head
                .get("asset_fact_id")
                .filter(|value| !value.is_null())
                .ok_or_else(|| invalid("账户资产观测缺少结构化事实引用"))?;
            let roots = crate::business_detail::load(
                connection,
                "business_asset_snapshot",
                "asset_id",
                reference,
            )
            .map_err(invalid)?;
            let root = roots
                .first()
                .ok_or_else(|| invalid("账户资产观测结构化事实缺失"))?;
            let assets = observation(connection, root, include_positions)?;
            if !assets.is_object() {
                return Err(invalid("账户资产观测不是对象"));
            }
            output.push(json!({"id":id,"assets":assets}));
        }
        Ok(Value::Array(output))
    })
}

#[cfg(test)]
mod tests {
    use rusqlite::hooks::{AuthAction, AuthContext, Authorization};

    use super::*;

    fn schema() -> Connection {
        let connection = Connection::open_in_memory().unwrap();
        connection.execute_batch(
            "CREATE TABLE account(id INTEGER PRIMARY KEY);
             INSERT INTO account VALUES(1),(2);
             CREATE TABLE execution_input(id TEXT PRIMARY KEY,account_id INTEGER,
                 execution_id TEXT,owner_execution_id TEXT);
             CREATE TABLE executerecord(id INTEGER PRIMARY KEY,account_id INTEGER,
                 execution_id TEXT,input_id TEXT,result_input_id TEXT,created_at TEXT);
             CREATE TABLE account_asset_snapshot(id INTEGER PRIMARY KEY,account_id INTEGER NOT NULL,
                 asset_fact_id TEXT,source TEXT NOT NULL,execution_id TEXT UNIQUE,created_at TEXT NOT NULL);
             CREATE TABLE business_asset_snapshot(asset_id TEXT PRIMARY KEY,record_id INTEGER,
                 input_id TEXT,account_snapshot_id INTEGER,path TEXT NOT NULL,scope TEXT NOT NULL,
                 ordinal INTEGER NOT NULL,map_key TEXT,payload_kind TEXT NOT NULL,payload_value JSON,
                 audit_evidence JSON NOT NULL,cash_state TEXT NOT NULL,equity_state TEXT NOT NULL,
                 market_state TEXT NOT NULL,positions_kind TEXT NOT NULL,positions_count INTEGER,
                 positions_value JSON,observation_valid INTEGER NOT NULL,available_cash REAL,
                 total_asset REAL,market_value REAL,currency TEXT,update_time TEXT,source TEXT,extra JSON,
                 CHECK ((record_id IS NOT NULL)+(input_id IS NOT NULL)+(account_snapshot_id IS NOT NULL)=1));
             CREATE TABLE business_asset_position(asset_id TEXT,ordinal INTEGER,payload_kind TEXT NOT NULL,
                 payload_value JSON,audit_evidence JSON NOT NULL,volume_state TEXT NOT NULL,
                 available_volume_state TEXT NOT NULL,market_value_state TEXT NOT NULL,
                 avg_price_state TEXT NOT NULL,symbol TEXT,volume REAL,available_volume REAL,
                 market_value REAL,direction TEXT,avg_price REAL,extra JSON,PRIMARY KEY(asset_id,ordinal));
             CREATE TABLE business_asset_binding(record_id INTEGER,path TEXT,asset_id TEXT NOT NULL,
                 scope TEXT NOT NULL,ordinal INTEGER NOT NULL,map_key TEXT,PRIMARY KEY(record_id,path));"
        ).unwrap();
        connection
    }

    fn count(connection: &Connection, table: &str) -> i64 {
        connection
            .query_row(&format!("SELECT count(*) FROM {table}"), [], |row| {
                row.get(0)
            })
            .unwrap()
    }

    fn input_and_record(connection: &Connection, execution_id: Option<&str>) {
        connection
            .execute(
                "INSERT INTO execution_input VALUES('i1',1,?1,?1)",
                [execution_id],
            )
            .unwrap();
        connection
            .execute(
                "INSERT INTO executerecord VALUES(17,1,?1,'i1',NULL,'terminal-time')",
                [execution_id],
            )
            .unwrap();
    }

    #[test]
    fn manual_observation_rolls_back_head_facts_and_positions_together() {
        let connection = schema();
        let first = json!({"account_assets":{"total_asset":50,"positions":[]}});
        crate::writer_scope::immediate(&connection, || {
            write_manual_snapshot(&connection, 1, "first", &first)
        })
        .unwrap();
        connection
            .execute_batch(
                "CREATE TRIGGER fail_second_position BEFORE INSERT ON business_asset_position
             WHEN NEW.ordinal=1 BEGIN SELECT RAISE(ABORT,'position failed'); END;",
            )
            .unwrap();
        let request =
            json!({"account_assets":{"total_asset":0,"positions":[{"symbol":"A"},{"symbol":"B"}]}});
        let result = crate::writer_scope::immediate(&connection, || {
            write_manual_snapshot(&connection, 1, "failed", &request)
        });
        assert!(result.is_err());
        assert_eq!(count(&connection, "account_asset_snapshot"), 1);
        assert_eq!(count(&connection, "business_asset_snapshot"), 1);
        assert_eq!(count(&connection, "business_asset_position"), 0);
        assert_eq!(
            crate::assets::page(&connection, 1, 0, 1).unwrap()["data"][0]["created_at"],
            "first"
        );
    }

    #[test]
    fn latest_trusted_observation_keeps_real_zero_and_null_invalid_states() {
        let connection = schema();
        for (account_id, value) in [
            (1, json!({"total_asset":10})),
            (
                1,
                json!({"total_asset":0,"available_cash":null,"market_value":"bad",
                "positions":[{"symbol":"A","volume":0,"available_volume":null,"avg_price":"bad"},null,"bad"]}),
            ),
            (1, json!({"total_asset":null})),
            (1, json!({"total_asset":100,"source":"unavailable"})),
            (2, json!({"total_asset":500})),
        ] {
            let request = json!({"account_assets":value});
            crate::writer_scope::immediate(&connection, || {
                write_manual_snapshot(&connection, account_id, "t", &request)
            })
            .unwrap();
        }
        let page = crate::assets::page(&connection, 1, 0, 1).unwrap();
        assert_eq!(page["count"], 2);
        let assets = &page["data"][0]["assets"];
        assert_eq!(assets["total_asset"], json!(0.0));
        assert_eq!(assets["available_cash"], Value::Null);
        assert!(assets.get("market_value").is_none());
        assert_eq!(
            assets["availability"],
            json!({"cash":"null","equity":"value","market_value":"invalid"})
        );
        assert_eq!(
            assets["positions"][0]["availability"]["available_volume"],
            "null"
        );
        assert_eq!(
            assets["positions"][0]["audit_evidence"]["invalid_fields"]["avg_price"],
            "bad"
        );
        assert_eq!(assets["positions"][1], Value::Null);
        assert_eq!(assets["positions"][2], "bad");
        assert_eq!(
            crate::assets::page(&connection, 2, 0, 1).unwrap()["count"],
            1
        );
    }

    #[test]
    fn scalar_projection_does_not_read_position_facts() {
        let connection = schema();
        let snapshot = write_manual_snapshot(
            &connection,
            1,
            "t",
            &json!({"account_assets":{"total_asset":5,"positions":[{"symbol":"A","volume":5}]}}),
        )
        .unwrap();
        let roots = crate::business_detail::load(
            &connection,
            "business_asset_snapshot",
            "asset_id",
            &snapshot["asset_fact_id"],
        )
        .unwrap();
        let root = &roots[0];
        connection.authorizer(Some(|context: AuthContext<'_>| match context.action {
            AuthAction::Read {
                table_name: "business_asset_position",
                ..
            } => Authorization::Deny,
            _ => Authorization::Allow,
        }));
        let mut expected = json!({"total_asset":5.0,
            "availability":{"cash":"absent","equity":"value","market_value":"absent"}});
        assert_eq!(observation(&connection, root, false).unwrap(), expected);
        let error = observation(&connection, root, true).unwrap_err();
        assert!(error.error.contains("business_asset_position"));

        connection.authorizer(None::<fn(AuthContext<'_>) -> Authorization>);
        expected["positions"] = json!([{"symbol":"A","volume":5.0,
            "availability":{"volume":"value","available_volume":"absent",
                "market_value":"absent","avg_price":"absent"}}]);
        assert_eq!(observation(&connection, root, true).unwrap(), expected);
    }

    #[test]
    fn terminal_history_reuses_observation_and_caller_transaction() {
        for execution_id in [Some("e1"), None] {
            let connection = schema();
            input_and_record(&connection, execution_id);
            let payload = json!({"account_assets":{"total_asset":0,"positions":[]}});
            let after =
                encode::encode_input_after_assets("i1", payload.as_object().unwrap()).unwrap();
            insert_batch(&connection, &after).unwrap();
            let result: Result<(), AnalysisError> =
                crate::writer_scope::immediate(&connection, || {
                    write_result_assets(
                        &connection,
                        17,
                        &payload,
                        Some("input:i1:/after_account_assets"),
                    )?;
                    let first = ensure_execution_asset_snapshot(&connection, 17)?.unwrap();
                    let second = ensure_execution_asset_snapshot(&connection, 17)?.unwrap();
                    assert_eq!(first, second);
                    assert_eq!(first["created_at"], "terminal-time");
                    assert_eq!(first["source"], "execution");
                    assert_eq!(count(&connection, "account_asset_snapshot"), 1);
                    assert_eq!(count(&connection, "business_asset_snapshot"), 1);
                    Err(invalid("terminal failed"))
                });
            assert!(result.is_err());
            assert_eq!(count(&connection, "account_asset_snapshot"), 0);
            assert_eq!(count(&connection, "business_asset_binding"), 0);
            assert_eq!(count(&connection, "business_asset_snapshot"), 1);
            connection
                .execute("DELETE FROM executerecord WHERE id=17", [])
                .unwrap();
            let retained = read_input_asset_observations(&connection, "i1").unwrap();
            assert_eq!(retained[0]["asset_id"], "input:i1:/after_account_assets");
            assert_eq!(retained[0]["account_assets"]["total_asset"], json!(0.0));
        }
    }

    #[test]
    fn wrong_asset_owner_and_before_binding_are_rejected() {
        for mutation in ["account", "execution", "input", "before"] {
            let connection = schema();
            input_and_record(&connection, Some("e1"));
            let payload = json!({"account_assets":{"total_asset":5}});
            let batch = if mutation == "before" {
                encode::encode_input_assets("i1", &payload).unwrap()
            } else {
                encode::encode_input_after_assets("i1", payload.as_object().unwrap()).unwrap()
            };
            insert_batch(&connection, &batch).unwrap();
            let change = match mutation {
                "account" => "UPDATE execution_input SET account_id=2",
                "execution" => "UPDATE execution_input SET owner_execution_id='e2'",
                "input" => "UPDATE executerecord SET input_id='other-input'",
                _ => "SELECT 1",
            };
            connection.execute_batch(change).unwrap();
            let reference = if mutation == "before" {
                "input:i1:/before_account_assets"
            } else {
                "input:i1:/after_account_assets"
            };
            let result = crate::writer_scope::immediate(&connection, || {
                write_result_assets(&connection, 17, &payload, Some(reference))
            });
            assert!(result.is_err(), "{mutation}");
            assert_eq!(count(&connection, "business_asset_binding"), 0);
            assert_eq!(count(&connection, "business_asset_snapshot"), 1);
        }
    }

    #[test]
    fn result_occurrences_preserve_symbol_key_paths_and_container_values() {
        let connection = schema();
        input_and_record(&connection, Some("e1"));
        let payload = json!({"account_assets":null,"symbol_results":{
            "BTC/USDT~hedge":{"account_assets":{"positions":null}},
            "":{"account_assets":"invalid"}}});
        write_result_assets(&connection, 17, &payload, None).unwrap();
        assert_eq!(count(&connection, "business_asset_snapshot"), 3);
        assert!(ensure_execution_asset_snapshot(&connection, 17)
            .unwrap()
            .is_none());
        let roots = crate::business_detail::load(
            &connection,
            "business_asset_snapshot",
            "record_id",
            &json!(17),
        )
        .unwrap();
        let hedged = roots
            .iter()
            .find(|row| row["map_key"] == "BTC/USDT~hedge")
            .unwrap();
        assert_eq!(
            hedged["path"],
            "/symbol_results/BTC~1USDT~0hedge/account_assets"
        );
        assert_eq!(
            observation(&connection, hedged, true).unwrap()["positions"],
            Value::Null
        );
        assert!(observation(&connection, hedged, false)
            .unwrap()
            .get("positions")
            .is_none());
        let root = roots.iter().find(|row| row["scope"] == "after").unwrap();
        assert_eq!(observation(&connection, root, true).unwrap(), Value::Null);
    }
}
