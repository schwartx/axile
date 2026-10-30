//! 组合配置、当前绑定上下文及试跑结果的原子保存。

use std::path::Path;

use rusqlite::{params, Connection, OptionalExtension, Row};
use serde_json::{json, Value};

use crate::error::AnalysisError;
use crate::{accounts, target_ops, writer_scope};

const COLUMNS: &str =
    "id,name,market,description,custom_calc_py_code,status,tag,updated_at_us,created_at_us";
const UPDATE_FIELDS: [&str; 6] = [
    "name",
    "market",
    "description",
    "custom_calc_py_code",
    "status",
    "tag",
];

fn sql(error: rusqlite::Error) -> AnalysisError {
    AnalysisError::new("PORTFOLIO_SQL", error.to_string())
}

fn invalid(message: impl Into<String>) -> AnalysisError {
    AnalysisError::new("VALIDATION", message)
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

fn portfolio_row(row: &Row<'_>) -> rusqlite::Result<Value> {
    Ok(json!({
        "id": row.get::<_, i64>("id")?,
        "name": row.get::<_, String>("name")?,
        "market": row.get::<_, String>("market")?,
        "description": row.get::<_, Option<String>>("description")?,
        "custom_calc_py_code": row.get::<_, String>("custom_calc_py_code")?,
        "status": row.get::<_, Option<String>>("status")?,
        "tag": row.get::<_, Option<String>>("tag")?,
        "updated_at": crate::time_us::read(row, "updated_at_us")?,
        "created_at": crate::time_us::read(row, "created_at_us")?,
    }))
}

pub(crate) fn portfolio_on(
    connection: &Connection,
    portfolio_id: i64,
) -> Result<Option<Value>, AnalysisError> {
    connection
        .query_row(
            &format!("SELECT {COLUMNS} FROM portfolio WHERE id=?1"),
            [portfolio_id],
            portfolio_row,
        )
        .optional()
        .map_err(sql)
}

fn require_portfolio(connection: &Connection, id: i64) -> Result<Value, AnalysisError> {
    portfolio_on(connection, id)?.ok_or_else(|| AnalysisError::new("NOT_FOUND", "组合不存在"))
}

fn public_portfolio(connection: &Connection, mut portfolio: Value) -> Result<Value, AnalysisError> {
    let id = integer(&portfolio, "id")?;
    portfolio["account_id"] = json!(accounts::latest_account_on(connection, id)?);
    Ok(portfolio)
}

fn create_on(connection: &Connection, request: &Value) -> Result<Value, AnalysisError> {
    let portfolio = &request["portfolio"];
    let code = text(portfolio, "custom_calc_py_code")?;
    if code.trim().is_empty() {
        return Err(invalid("自定义组合计算函数不能为空"));
    }
    connection.execute(
        "INSERT INTO portfolio(name,market,description,custom_calc_py_code,status,tag,updated_at_us,created_at_us) VALUES (?1,?2,?3,?4,?5,?6,?7,?8)",
        params![text(portfolio, "name")?, text(portfolio, "market")?, portfolio["description"].as_str(), code,
            portfolio["status"].as_str(), portfolio["tag"].as_str(), crate::time_us::parse(text(portfolio, "updated_at")?).map_err(invalid)?, crate::time_us::parse(text(portfolio, "created_at")?).map_err(invalid)?],
    ).map_err(sql)?;
    let mut saved = require_portfolio(connection, connection.last_insert_rowid())?;
    saved["account_id"] = Value::Null;
    Ok(saved)
}

fn update_on(connection: &Connection, request: &Value) -> Result<Value, AnalysisError> {
    let id = integer(request, "portfolio_id")?;
    let existing = require_portfolio(connection, id)?;
    let changes = request["changes"]
        .as_object()
        .ok_or_else(|| invalid("组合更新载荷必须是对象"))?;
    if changes
        .keys()
        .any(|key| !UPDATE_FIELDS.contains(&key.as_str()))
    {
        return Err(invalid("组合更新包含未知字段"));
    }
    for (key, value) in changes {
        if matches!(key.as_str(), "name" | "market") && value.is_null() {
            return Err(invalid(format!("组合字段 {key} 不可清空")));
        }
        if !value.is_null() && !value.is_string() {
            return Err(invalid(format!("组合字段 {key} 必须是文本或空值")));
        }
    }
    if let Some(code) = changes.get("custom_calc_py_code") {
        if code.as_str().is_none_or(|code| code.trim().is_empty()) {
            return Err(invalid("自定义组合计算函数不可清空"));
        }
    }
    if changes.is_empty() {
        return Ok(existing);
    }
    connection.execute(
        "UPDATE portfolio SET
        name=CASE WHEN json_type(?1,'$.name') IS NULL THEN name ELSE json_extract(?1,'$.name') END,
        market=CASE WHEN json_type(?1,'$.market') IS NULL THEN market ELSE json_extract(?1,'$.market') END,
        description=CASE WHEN json_type(?1,'$.description') IS NULL THEN description ELSE json_extract(?1,'$.description') END,
        custom_calc_py_code=CASE WHEN json_type(?1,'$.custom_calc_py_code') IS NULL THEN custom_calc_py_code ELSE json_extract(?1,'$.custom_calc_py_code') END,
        status=CASE WHEN json_type(?1,'$.status') IS NULL THEN status ELSE json_extract(?1,'$.status') END,
        tag=CASE WHEN json_type(?1,'$.tag') IS NULL THEN tag ELSE json_extract(?1,'$.tag') END,
        updated_at_us=?2 WHERE id=?3",
        params![Value::Object(changes.clone()).to_string(), crate::time_us::parse(text(request, "updated_at")?).map_err(invalid)?, id],
    ).map_err(sql)?;
    require_portfolio(connection, id)
}

fn delete_on(connection: &Connection, id: i64) -> Result<Value, AnalysisError> {
    require_portfolio(connection, id)?;
    if accounts::latest_account_on(connection, id)?.is_some() {
        return Err(AnalysisError::new(
            "CONFLICT",
            "该组合与账户关联，必须解绑才能删除组合",
        ));
    }
    connection
        .execute("DELETE FROM portfolio WHERE id=?1", [id])
        .map_err(sql)?;
    Ok(json!({"message": "成功删除组合"}))
}

fn context_on(connection: &Connection, request: &Value) -> Result<Value, AnalysisError> {
    let id = integer(request, "portfolio_id")?;
    let portfolio = require_portfolio(connection, id)?;
    let explicit = request.get("account_id");
    let account_id = match explicit {
        Some(Value::Null) => None,
        Some(_) => Some(integer(request, "account_id")?),
        None => accounts::latest_account_on(connection, id)?,
    };
    let account = match account_id {
        None => Value::Null,
        Some(id) => accounts::snapshot_on(connection, id)?.ok_or_else(|| {
            AnalysisError::new(
                "NOT_FOUND",
                if explicit.is_some() {
                    "账户不存在"
                } else {
                    "绑定账户不存在"
                },
            )
        })?,
    };
    Ok(json!({"portfolio": portfolio, "account_id": account_id, "account": account}))
}

fn bound_context_on(connection: &Connection, request: &Value) -> Result<Value, AnalysisError> {
    let account_id = integer(request, "account_id")?;
    let account = accounts::snapshot_on(connection, account_id)?;
    let portfolio_id = accounts::latest_portfolio_on(connection, account_id)?;
    let portfolio = portfolio_id
        .map(|id| portfolio_on(connection, id))
        .transpose()?
        .flatten();
    Ok(json!({"account": account, "portfolio_id": portfolio_id, "portfolio": portfolio}))
}

fn target_public(snapshot: Option<Value>) -> Result<Value, AnalysisError> {
    let has_snapshot = snapshot.is_some();
    let snapshot = snapshot.unwrap_or(Value::Null);
    let weights = match &snapshot["raw_weights"] {
        Value::Object(weights) if weights.values().all(Value::is_number) => json!(weights),
        Value::Null | Value::Bool(false) => json!({}),
        Value::String(value) if value.is_empty() => json!({}),
        Value::Array(value) if value.is_empty() => json!({}),
        Value::Number(value) if value.as_f64() == Some(0.0) => json!({}),
        _ => return Err(AnalysisError::new("TARGET_INVALID", "组合目标快照权重无效")),
    };
    if has_snapshot && !matches!(snapshot["source"].as_str(), Some("manual" | "execution")) {
        return Err(AnalysisError::new("TARGET_INVALID", "组合目标快照来源无效"));
    }
    Ok(json!({"weights": weights,
        "quantities":null, "strategy_weights":null,"account_weights":null,"sizing":null,
        "calculated_at":snapshot["calculated_at"],"source":snapshot["source"],
        "execution_id":snapshot["execution_id"],"context_account_id":snapshot["account_id"]}))
}

/// 纯 HTTP 查询返回最终视图，由绑定层直接编码为 JSON 字节。
pub fn portfolio_query(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let connection = crate::storage_connection::open_reader(Path::new(path))?;
    connection.execute_batch("BEGIN DEFERRED;").map_err(sql)?;
    match text(request, "op")? {
        "list" => {
            let mut statement = connection
                .prepare(&format!("SELECT {COLUMNS} FROM portfolio"))
                .map_err(sql)?;
            let data = statement
                .query_map([], portfolio_row)
                .map_err(sql)?
                .collect::<Result<Vec<_>, _>>()
                .map_err(sql)?;
            Ok(json!({"data": data}))
        }
        "get" => public_portfolio(
            &connection,
            require_portfolio(&connection, integer(request, "portfolio_id")?)?,
        ),
        "target_snapshot" => {
            let id = integer(request, "portfolio_id")?;
            require_portfolio(&connection, id)?;
            target_public(target_ops::latest_portfolio_target_snapshot(
                &connection,
                id,
            )?)
        }
        _ => Err(invalid("未知组合查询")),
    }
}

/// 策略执行前只读取上下文；策略完成后一次写入组合和目标快照。
pub fn portfolio_operation(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let operation = text(request, "op")?;
    if matches!(
        operation,
        "context" | "bound_context" | "validation_context"
    ) {
        let connection = crate::storage_connection::open_reader(Path::new(path))?;
        connection.execute_batch("BEGIN DEFERRED;").map_err(sql)?;
        return match operation {
            "context" => context_on(&connection, request),
            "bound_context" => bound_context_on(&connection, request),
            _ => accounts::snapshot_on(&connection, integer(request, "account_id")?)?
                .ok_or_else(|| AnalysisError::new("NOT_FOUND", "账户不存在")),
        };
    }
    let connection = writer_scope::open_writer(Path::new(path))?;
    writer_scope::immediate(&connection, || match operation {
        "create" => create_on(&connection, request),
        "update" => public_portfolio(&connection, update_on(&connection, request)?),
        "delete" => delete_on(&connection, integer(request, "portfolio_id")?),
        "save_trial" => {
            update_on(&connection, request)?;
            target_ops::append_target_snapshot(&connection, request)?;
            Ok(Value::Null)
        }
        _ => Err(invalid("未知组合操作")),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn schema(connection: &Connection) {
        connection.execute_batch("CREATE TABLE portfolio(id INTEGER PRIMARY KEY,name TEXT NOT NULL,market TEXT NOT NULL,description TEXT,custom_calc_py_code TEXT NOT NULL,status TEXT,tag TEXT,updated_at_us INTEGER NOT NULL,created_at_us INTEGER NOT NULL);").unwrap();
    }

    fn initial(connection: &Connection) -> Value {
        create_on(connection, &json!({"portfolio":{"name":"old","market":"futures","custom_calc_py_code":"def calculate_portfolio(context): return {}","updated_at":"2026-10-23T09:00:00.000000Z","created_at":"2026-10-23T08:00:00.000000Z"}})).unwrap()
    }

    #[test]
    fn patch_distinguishes_omitted_null_and_explicit_fields() {
        let connection = Connection::open_in_memory().unwrap();
        schema(&connection);
        let saved = initial(&connection);
        let id = saved["id"].clone();
        let updated = update_on(&connection, &json!({"portfolio_id":id,"changes":{"description":"desc","tag":"tag"},"updated_at":"2026-10-23T10:00:00.000000Z"})).unwrap();
        assert_eq!(updated["updated_at"], "2026-10-23T10:00:00.000000Z");
        let unchanged = update_on(
            &connection,
            &json!({"portfolio_id":id,"changes":{"tag":"tag"},"updated_at":"2026-10-23T11:00:00.000000Z"}),
        )
        .unwrap();
        assert_eq!(unchanged["updated_at"], "2026-10-23T11:00:00.000000Z");
        let cleared = update_on(
            &connection,
            &json!({"portfolio_id":id,"changes":{"description":null},"updated_at":"2026-10-23T11:00:00.000000Z"}),
        )
        .unwrap();
        assert!(cleared["description"].is_null());
        assert_eq!(cleared["tag"], "tag");
        assert_eq!(cleared["created_at"], "2026-10-23T08:00:00.000000Z");
    }

    #[test]
    fn trial_snapshot_failure_rolls_back_portfolio_update() {
        let connection = Connection::open_in_memory().unwrap();
        schema(&connection);
        let saved = initial(&connection);
        connection.execute_batch("CREATE TABLE target_weight_snapshot(id INTEGER PRIMARY KEY,portfolio_id INTEGER,account_id INTEGER,raw_weights_kind TEXT,normalized_weights_kind TEXT,weights_metadata TEXT,source TEXT,execution_id TEXT,calculated_at_us INTEGER); CREATE TABLE target_weight_snapshot_weight(snapshot_id INTEGER,role TEXT,map_key TEXT,value REAL,audit_evidence TEXT); CREATE TRIGGER reject_weight BEFORE INSERT ON target_weight_snapshot_weight BEGIN SELECT RAISE(ABORT,'reject weight'); END;").unwrap();
        let request = json!({"portfolio_id":saved["id"],"changes":{"name":"changed","custom_calc_py_code":"new code"},"updated_at":"2026-10-23T10:00:00.000000Z","raw_weights":{"rb2610":1.0},"normalized_weights":null,"source":"manual","calculated_at":"2026-10-23T10:00:00.000000Z"});
        let result = writer_scope::immediate(&connection, || {
            update_on(&connection, &request)?;
            target_ops::append_target_snapshot(&connection, &request)
        });
        assert!(result.is_err());
        let portfolio = require_portfolio(&connection, saved["id"].as_i64().unwrap()).unwrap();
        assert_eq!(portfolio["name"], "old");
        assert_eq!(portfolio["updated_at"], "2026-10-23T09:00:00.000000Z");
        assert_eq!(
            connection
                .query_row("SELECT count(*) FROM target_weight_snapshot", [], |row| row
                    .get::<_, i64>(0))
                .unwrap(),
            0
        );
    }

    #[test]
    fn deletion_checks_each_accounts_latest_binding() {
        let connection = Connection::open_in_memory().unwrap();
        schema(&connection);
        initial(&connection);
        connection.execute_batch("PRAGMA foreign_keys=ON; CREATE TABLE portfolioaccount(id INTEGER PRIMARY KEY,account_id INTEGER,portfolio_id INTEGER REFERENCES portfolio(id) ON DELETE CASCADE); INSERT INTO portfolioaccount VALUES(1,10,1),(2,20,1),(3,20,NULL);").unwrap();
        assert_eq!(delete_on(&connection, 1).unwrap_err().code, "CONFLICT");
        connection
            .execute("INSERT INTO portfolioaccount VALUES(4,10,NULL)", [])
            .unwrap();
        assert_eq!(
            delete_on(&connection, 1).unwrap()["message"],
            "成功删除组合"
        );
        assert!(portfolio_on(&connection, 1).unwrap().is_none());
        assert_eq!(
            connection
                .query_row(
                    "SELECT count(*) FROM portfolioaccount WHERE portfolio_id=1",
                    [],
                    |row| row.get::<_, i64>(0)
                )
                .unwrap(),
            0
        );
        assert_eq!(delete_on(&connection, 1).unwrap_err().code, "NOT_FOUND");
    }

    #[test]
    fn target_http_view_keeps_empty_fallback_and_rejects_invalid_weights() {
        assert_eq!(
            target_public(None).unwrap(),
            json!({"weights":{},"quantities":null,
            "strategy_weights":null,"account_weights":null,"sizing":null,"calculated_at":null,
            "source":null,"execution_id":null,"context_account_id":null})
        );
        for raw in [
            json!({}),
            json!([]),
            json!(null),
            json!(0),
            json!(false),
            json!(""),
        ] {
            assert_eq!(
                target_public(Some(json!({"raw_weights":raw,"source":"manual"}))).unwrap()
                    ["weights"],
                json!({})
            );
        }
        assert!(
            target_public(Some(json!({"raw_weights":{"bad":null},"source":"manual"}))).is_err()
        );
        assert!(target_public(Some(json!({"raw_weights":[1],"source":"manual"}))).is_err());
    }

    #[test]
    fn target_http_view_keeps_metadata_and_rejects_malformed_snapshots() {
        for snapshot in [
            Value::Null,
            json!({}),
            json!({"raw_weights":{},"source":"invalid"}),
        ] {
            let error = target_public(Some(snapshot)).unwrap_err();
            assert_eq!(error.code, "TARGET_INVALID");
            assert_eq!(error.error, "组合目标快照来源无效");
        }
        assert_eq!(
            target_public(Some(
                json!({"raw_weights":{"native":0.5},"source":"execution",
            "calculated_at":"2026-10-23T09:00:00.000000Z","execution_id":"e1","account_id":2})
            ))
            .unwrap(),
            json!({"weights":{"native":0.5},"quantities":null,"strategy_weights":null,
                "account_weights":null,"sizing":null,"calculated_at":"2026-10-23T09:00:00.000000Z","source":"execution",
                "execution_id":"e1","context_account_id":2})
        );
    }
}
