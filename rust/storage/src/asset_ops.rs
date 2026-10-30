//! 资产观测写入及内部读取；终态辅助函数只使用调用方连接。

use std::path::Path;

use rusqlite::{params, Connection, OptionalExtension};
use serde_json::{json, Map, Value};

use crate::business_facts_ops::{encode, facts_error as invalid, insert_rows, sql};
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
    let created_at_us = crate::time_us::parse(created_at).map_err(invalid)?;
    connection
        .execute(
            "INSERT INTO account_asset_snapshot(account_id,source,created_at_us)
             VALUES(?1,'manual',?2)",
            params![account_id, created_at_us],
        )
        .map_err(sql)?;
    let snapshot_id = connection.last_insert_rowid();
    let batch = encode::encode_snapshot_assets(
        snapshot_id,
        request,
        request.get("result").and_then(Value::as_object),
    )
    .map_err(invalid)?;
    write_observation(connection, Some(&batch))?;
    let asset_id = format!("snapshot:{snapshot_id}:/account_assets");
    connection
        .execute(
            "UPDATE account_asset_snapshot SET asset_fact_id=?1 WHERE id=?2",
            params![asset_id, snapshot_id],
        )
        .map_err(sql)?;
    Ok(
        json!({"id": snapshot_id, "account_id": account_id, "asset_fact_id": asset_id,
            "source": "manual", "execution_id": null, "created_at": crate::time_us::format(created_at_us).map_err(invalid)?}),
    )
}

/// 结果中的每次观测保留各自标识；根观测可复用 worker 已提交的事实。
pub(crate) fn encode_result_assets(
    record_id: i64,
    payload: &Value,
    root_asset_id: Option<&str>,
) -> Result<(encode::Rows, encode::Rows, encode::Rows), String> {
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
    Ok((roots, positions, bindings))
}

pub(crate) fn write_result_assets(
    connection: &Connection,
    record_id: i64,
    payload: &Value,
    root_asset_id: Option<&str>,
) -> Result<(), AnalysisError> {
    let (roots, positions, bindings) =
        encode_result_assets(record_id, payload, root_asset_id).map_err(invalid)?;
    for row in &roots {
        insert_asset(connection, row)?;
    }
    insert_positions(connection, &positions)?;
    insert_rows(
        connection,
        "business_asset_binding",
        &bindings,
        &[
            ("record_id", "integer"),
            ("path", "text"),
            ("asset_id", "text"),
            ("scope", "text"),
            ("ordinal", "integer"),
            ("map_key", "text"),
        ],
    )?;

    validate_result_asset_ownership(connection, record_id)
}

pub(crate) fn write_observation(
    connection: &Connection,
    observation: Option<&encode::Observation>,
) -> Result<(), AnalysisError> {
    if let Some(observation) = observation {
        insert_asset(connection, &observation.root)?;
        insert_positions(connection, &observation.positions)?;
    }
    Ok(())
}

pub(crate) fn insert_asset(
    connection: &Connection,
    row: &Map<String, Value>,
) -> Result<(), AnalysisError> {
    insert_rows(
        connection,
        "business_asset_snapshot",
        std::slice::from_ref(row),
        &[
            ("asset_id", "text"),
            ("record_id", "integer"),
            ("input_id", "text"),
            ("account_snapshot_id", "integer"),
            ("path", "text"),
            ("scope", "text"),
            ("ordinal", "integer"),
            ("map_key", "text"),
            ("payload_kind", "text"),
            ("payload_value", "json"),
            ("audit_evidence", "json"),
            ("cash_state", "text"),
            ("equity_state", "text"),
            ("market_state", "text"),
            ("positions_kind", "text"),
            ("positions_count", "integer"),
            ("positions_value", "json"),
            ("observation_valid", "boolean"),
            ("available_cash", "number"),
            ("total_asset", "number"),
            ("market_value", "number"),
            ("currency", "text"),
            ("update_time", "text"),
            ("source", "text"),
            ("extra", "json"),
        ],
    )
}

fn insert_positions(
    connection: &Connection,
    rows: &[Map<String, Value>],
) -> Result<(), AnalysisError> {
    insert_rows(
        connection,
        "business_asset_position",
        rows,
        &[
            ("asset_id", "text"),
            ("ordinal", "integer"),
            ("payload_kind", "text"),
            ("payload_value", "json"),
            ("audit_evidence", "json"),
            ("volume_state", "text"),
            ("available_volume_state", "text"),
            ("market_value_state", "text"),
            ("avg_price_state", "text"),
            ("symbol", "text"),
            ("volume", "number"),
            ("available_volume", "number"),
            ("market_value", "number"),
            ("direction", "text"),
            ("avg_price", "number"),
            ("extra", "json"),
        ],
    )
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

/// 为已校验归属的结果资产补齐历史索引，与终态事实共用事务。
pub(crate) fn ensure_execution_asset_snapshot(
    connection: &Connection,
    record_id: i64,
) -> Result<(), AnalysisError> {
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
        return Ok(());
    };
    let typed: bool = connection.query_row(
        "SELECT EXISTS(SELECT 1 FROM pragma_table_info('executerecord') WHERE name='created_at_us')",
        [], |row| row.get(0),
    ).map_err(sql)?;
    // 0033 转换先核对原始证据，之后才统一标量时间。
    let time_column = if typed { "created_at_us" } else { "created_at" };
    let (account_id, execution_id, created_at): (i64, Option<String>, rusqlite::types::Value) =
        connection
            .query_row(
                &format!(
                    "SELECT account_id,execution_id,{time_column} FROM executerecord WHERE id=?1"
                ),
                [record_id],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .map_err(sql)?;
    let (lookup, identity) = match execution_id.as_deref() {
        Some(execution) => ("execution_id", execution),
        None => ("asset_fact_id", reference.as_str()),
    };
    let mut statement = connection
        .prepare(&format!(
            "SELECT * FROM account_asset_snapshot WHERE {lookup}=?1"
        ))
        .map_err(sql)?;
    let mut cursor = statement.query([identity]).map_err(sql)?;
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
        return Ok(());
    }
    connection.execute(
        &format!("INSERT INTO account_asset_snapshot(account_id,asset_fact_id,source,execution_id,{time_column})
         VALUES(?1,?2,'execution',?3,?4)"),
        params![account_id, reference, execution_id, created_at],
    ).map_err(sql)?;
    Ok(())
}

fn observation(connection: &Connection, root: &Map<String, Value>) -> Result<Value, AnalysisError> {
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
        "account_assets": observation(connection,root)?}))
        })
        .collect::<Result<Vec<_>, _>>()
        .map(Value::Array)
}

pub fn asset_snapshot_latest(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let account_id = integer(request, "account_id")?;
    crate::with_read_snapshot(Path::new(path), |connection| {
        crate::assets::latest(connection, account_id).map_err(AnalysisError::from)
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
                 execution_id TEXT,input_id TEXT,result_input_id TEXT,created_at_us INTEGER);
             CREATE TABLE account_asset_snapshot(id INTEGER PRIMARY KEY,account_id INTEGER NOT NULL,
                 asset_fact_id TEXT,source TEXT NOT NULL,execution_id TEXT UNIQUE,created_at_us INTEGER NOT NULL);
             CREATE TABLE business_asset_snapshot(asset_id TEXT PRIMARY KEY,record_id INTEGER,
                 input_id TEXT,account_snapshot_id INTEGER,path TEXT NOT NULL,scope TEXT NOT NULL,
                 ordinal INTEGER NOT NULL,map_key TEXT,payload_kind TEXT NOT NULL,payload_value JSON,
                 audit_evidence JSON NOT NULL,cash_state TEXT NOT NULL,equity_state TEXT NOT NULL,
                 market_state TEXT NOT NULL,positions_kind TEXT NOT NULL,positions_count INTEGER,
                 positions_value JSON,observation_valid INTEGER NOT NULL,available_cash REAL,
                 total_asset REAL,market_value REAL,currency TEXT,update_time_us INTEGER,source TEXT,extra JSON,
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
                "INSERT INTO executerecord VALUES(17,1,?1,'i1',NULL,1792746000000000)",
                [execution_id],
            )
            .unwrap();
    }

    #[test]
    fn legacy_conversion_copies_time_before_scalar_normalization() {
        let connection = schema();
        connection
            .execute_batch(
                "ALTER TABLE executerecord RENAME COLUMN created_at_us TO created_at;
            ALTER TABLE account_asset_snapshot RENAME COLUMN created_at_us TO created_at;",
            )
            .unwrap();
        input_and_record(&connection, Some("e1"));
        let original = "2026-10-23T09:00:00.123456+08:00";
        connection
            .execute(
                "UPDATE executerecord SET created_at=?1 WHERE id=17",
                [original],
            )
            .unwrap();
        write_result_assets(
            &connection,
            17,
            &json!({"account_assets":{"total_asset":1,"positions":[]}}),
            None,
        )
        .unwrap();
        ensure_execution_asset_snapshot(&connection, 17).unwrap();
        let copied: String = connection
            .query_row("SELECT created_at FROM account_asset_snapshot", [], |row| {
                row.get(0)
            })
            .unwrap();
        assert_eq!(copied, original);
    }

    #[test]
    fn manual_observation_rolls_back_head_facts_and_positions_together() {
        let connection = schema();
        let first = json!({"account_assets":{"total_asset":50,"positions":[]}});
        crate::writer_scope::immediate(&connection, || {
            write_manual_snapshot(&connection, 1, "2026-10-23T08:00:00.000000Z", &first)
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
            write_manual_snapshot(&connection, 1, "2026-10-23T10:00:00.000000Z", &request)
        });
        assert!(result.is_err());
        assert_eq!(count(&connection, "account_asset_snapshot"), 1);
        assert_eq!(count(&connection, "business_asset_snapshot"), 1);
        assert_eq!(count(&connection, "business_asset_position"), 0);
        assert_eq!(
            crate::assets::page(&connection, 1, 0, 1).unwrap()["data"][0]["created_at"],
            "2026-10-23T08:00:00.000000Z"
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
                write_manual_snapshot(
                    &connection,
                    account_id,
                    "2026-10-23T09:00:00.000000Z",
                    &request,
                )
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
    fn observation_requires_position_facts() {
        let connection = schema();
        let snapshot = write_manual_snapshot(
            &connection,
            1,
            "2026-10-23T09:00:00.000000Z",
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
        let error = observation(&connection, root).unwrap_err();
        assert!(error.error.contains("business_asset_position"));

        connection.authorizer(None::<fn(AuthContext<'_>) -> Authorization>);
        let mut expected = json!({"total_asset":5.0,
            "availability":{"cash":"absent","equity":"value","market_value":"absent"}});
        expected["positions"] = json!([{"symbol":"A","volume":5.0,
            "availability":{"volume":"value","available_volume":"absent",
                "market_value":"absent","avg_price":"absent"}}]);
        assert_eq!(observation(&connection, root).unwrap(), expected);
    }

    #[test]
    fn input_observations_survive_terminal_delete() {
        let connection = schema();
        input_and_record(&connection, Some("e1"));
        let before = encode::encode_input_assets(
            "i1",
            &json!({"account_assets":{"total_asset":100,"positions":null}}),
        )
        .unwrap();
        let after = encode::encode_input_after_assets(
            "i1",
            json!({"account_assets":{"total_asset":0,"positions":[null,"bad"]}})
                .as_object()
                .unwrap(),
        )
        .unwrap();
        write_observation(&connection, before.as_ref()).unwrap();
        write_observation(&connection, after.as_ref()).unwrap();
        connection
            .execute("DELETE FROM executerecord WHERE id=17", [])
            .unwrap();
        let observations = read_input_asset_observations(&connection, "i1").unwrap();
        assert_eq!(observations.as_array().unwrap().len(), 2);
        assert_eq!(observations[0]["scope"], "after_pending");
        assert_eq!(observations[0]["account_assets"]["total_asset"], json!(0.0));
        assert_eq!(
            observations[0]["account_assets"]["positions"],
            json!([null, "bad"])
        );
        assert_eq!(observations[1]["scope"], "before");
        assert_eq!(observations[1]["account_assets"]["positions"], Value::Null);
    }

    #[test]
    fn terminal_history_reuses_observation_and_caller_transaction() {
        for execution_id in [Some("e1"), None] {
            let connection = schema();
            input_and_record(&connection, execution_id);
            let payload = json!({"account_assets":{"total_asset":0,"positions":[]}});
            let after =
                encode::encode_input_after_assets("i1", payload.as_object().unwrap()).unwrap();
            write_observation(&connection, after.as_ref()).unwrap();
            let result: Result<(), AnalysisError> =
                crate::writer_scope::immediate(&connection, || {
                    write_result_assets(
                        &connection,
                        17,
                        &payload,
                        Some("input:i1:/after_account_assets"),
                    )?;
                    let history = || {
                        crate::business_detail::load(
                            &connection,
                            "account_asset_snapshot",
                            "account_id",
                            &json!(1),
                        )
                        .map_err(invalid)
                    };
                    ensure_execution_asset_snapshot(&connection, 17)?;
                    let first = history()?;
                    ensure_execution_asset_snapshot(&connection, 17)?;
                    assert_eq!(first, history()?);
                    assert_eq!(first[0]["created_at"], "2026-10-23T09:00:00.000000Z");
                    assert_eq!(first[0]["source"], "execution");
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
            write_observation(&connection, batch.as_ref()).unwrap();
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
            "SHFE.rb2610/CNY~hedge":{"account_assets":{"positions":null}},
            "":{"account_assets":"invalid"}}});
        write_result_assets(&connection, 17, &payload, None).unwrap();
        assert_eq!(count(&connection, "business_asset_snapshot"), 3);
        ensure_execution_asset_snapshot(&connection, 17).unwrap();
        assert_eq!(count(&connection, "account_asset_snapshot"), 0);
        let roots = crate::business_detail::load(
            &connection,
            "business_asset_snapshot",
            "record_id",
            &json!(17),
        )
        .unwrap();
        let hedged = roots
            .iter()
            .find(|row| row["map_key"] == "SHFE.rb2610/CNY~hedge")
            .unwrap();
        assert_eq!(
            hedged["path"],
            "/symbol_results/SHFE.rb2610~1CNY~0hedge/account_assets"
        );
        assert_eq!(
            observation(&connection, hedged).unwrap()["positions"],
            Value::Null
        );
        let root = roots.iter().find(|row| row["scope"] == "after").unwrap();
        assert_eq!(observation(&connection, root).unwrap(), Value::Null);
    }
}
