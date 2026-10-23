//! 执行终态、冻结输入及历史删除的完整存储操作。

use std::path::Path;

use rusqlite::{params, Connection};
use serde_json::{json, Map, Value};

use crate::business_facts_ops::{
    self as facts, encode, facts_error, result_extensions, result_head, result_orders,
    result_symbols, result_trades, sql, summary_refs,
};
use crate::error::AnalysisError;

fn integer(request: &Value, name: &str) -> Result<i64, AnalysisError> {
    request
        .get(name)
        .and_then(Value::as_i64)
        .ok_or_else(|| facts_error(format!("缺少或无效的 {name}")))
}

fn text<'a>(request: &'a Value, name: &str) -> Result<&'a str, AnalysisError> {
    request
        .get(name)
        .and_then(Value::as_str)
        .ok_or_else(|| facts_error(format!("缺少或无效的 {name}")))
}

fn optional_text<'a>(request: &'a Value, name: &str) -> Result<Option<&'a str>, AnalysisError> {
    match request.get(name) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(value)) => Ok(Some(value)),
        _ => Err(facts_error(format!("无效的 {name}"))),
    }
}

fn require_account(connection: &Connection, account_id: i64) -> Result<(), AnalysisError> {
    let exists: bool = connection
        .query_row(
            "SELECT EXISTS(SELECT 1 FROM account WHERE id=?1)",
            [account_id],
            |row| row.get(0),
        )
        .map_err(sql)?;
    if exists {
        Ok(())
    } else {
        Err(AnalysisError::new("ACCOUNT_NOT_FOUND", "账户不存在"))
    }
}

fn record_row(
    connection: &Connection,
    record_id: i64,
) -> Result<Map<String, Value>, AnalysisError> {
    facts_row(connection, "id", &json!(record_id))?.ok_or_else(|| facts_error("执行主记录不存在"))
}

fn facts_row(
    connection: &Connection,
    column: &str,
    identity: &Value,
) -> Result<Option<Map<String, Value>>, AnalysisError> {
    Ok(
        crate::business_detail::load(connection, "executerecord", column, identity)
            .map_err(facts_error)?
            .into_iter()
            .next(),
    )
}

/// 一个执行首条终态胜出，所有结果事实与历史引用在同一事务提交。
pub fn persist_execute_record(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let connection = crate::storage_connection::open_writer(Path::new(path), false)?;
    crate::writer_scope::immediate(&connection, || {
        persist_execute_record_in(&connection, request)
    })
}

fn persist_execute_record_in(
    connection: &Connection,
    request: &Value,
) -> Result<Value, AnalysisError> {
    let account_id = integer(request, "account_id")?;
    let execution_id = optional_text(request, "execution_id")?;
    if let Some(execution_id) = execution_id {
        if let Some(existing) = facts_row(connection, "execution_id", &json!(execution_id))? {
            if existing.get("account_id").and_then(Value::as_i64) != Some(account_id) {
                return Err(facts_error("执行标识属于其他账户"));
            }
            return Ok(Value::Object(existing));
        }
    }
    require_account(connection, account_id)?;
    let created_at = text(request, "created_at")?;
    let is_success = integer(request, "is_success")?;
    if !matches!(is_success, 0 | 1) {
        return Err(facts_error("执行成功标识必须为 0 或 1"));
    }
    let raw_result = request.get("raw_result").unwrap_or(&Value::Null);
    let raw_input = request.get("raw_input").filter(|value| !value.is_null());
    if raw_input.is_some_and(|value| !value.is_object()) {
        return Err(facts_error("执行输入必须是对象"));
    }
    let mut head = result_head::execution_head_values(raw_result, Some(is_success))?;
    head.insert("account_id".into(), json!(account_id));
    head.insert("execution_id".into(), json!(execution_id));
    head.insert("is_success".into(), json!(is_success));
    head.insert("created_at".into(), json!(created_at));
    head.insert("facts_complete".into(), json!(false));
    let mut batch = encode::FactBatch::new();
    batch.push("executerecord", vec![head]);
    facts::insert_batch(connection, &batch)?;
    let record_id = connection.last_insert_rowid();
    let input_id = attach_inputs(connection, request, record_id, raw_input, raw_result)?;
    let root_asset_id = match (input_id, raw_result.as_object()) {
        (Some(input_id), Some(payload))
            if payload.contains_key("account_assets") || is_success == 1 =>
        {
            facts::matching_after_asset(connection, &input_id, payload)?
        }
        _ => None,
    };
    result_head::write_result_cancellations(connection, record_id, raw_result)?;
    if let Some(payload) = raw_result.as_object() {
        result_symbols::write_result_symbols(connection, record_id, raw_result)?;
        crate::asset_ops::write_result_assets(
            connection,
            record_id,
            raw_result,
            root_asset_id.as_deref(),
        )?;
        result_orders::insert_orders(connection, record_id, payload)?;
        result_trades::insert_trades(connection, record_id, payload)?;
    }
    result_extensions::write_result_extensions(connection, record_id, raw_result)?;
    connection
        .execute(
            "UPDATE executerecord SET facts_complete=1 WHERE id=?1",
            [record_id],
        )
        .map_err(sql)?;
    crate::asset_ops::ensure_execution_asset_snapshot(connection, record_id)?;
    if let Some(execution_id) = execution_id {
        summary_refs::compact_execution_summary(connection, record_id, execution_id, raw_result)?;
    }
    Ok(Value::Object(record_row(connection, record_id)?))
}

fn attach_inputs(
    connection: &Connection,
    request: &Value,
    record_id: i64,
    raw_input: Option<&Value>,
    raw_result: &Value,
) -> Result<Option<String>, AnalysisError> {
    let account_id = integer(request, "account_id")?;
    let execution_id = optional_text(request, "execution_id")?;
    let created_at = text(request, "created_at")?;
    let mut input_id = match execution_id {
        Some(execution_id) => facts::find_execution_input(connection, account_id, execution_id)?,
        None => None,
    };
    if input_id.is_none() {
        if let Some(payload) = raw_input {
            input_id = Some(facts::persist_input(
                connection,
                account_id,
                execution_id,
                text(request, "input_id")?,
                payload,
                created_at,
                None,
            )?);
        }
    }
    let result_input_id = match raw_result.get("inputs").filter(|value| value.is_object()) {
        Some(payload) => {
            let payload = encode::sanitize_payload(payload).map_err(facts_error)?;
            let same = match input_id.as_deref() {
                Some(input_id) => encode::py_eq(
                    &facts::load_input_value(connection, input_id)?,
                    &encode::canonical_input(&payload).map_err(facts_error)?,
                ),
                None => false,
            };
            if same {
                input_id.clone()
            } else {
                Some(facts::persist_input(
                    connection,
                    account_id,
                    None,
                    text(request, "result_input_id")?,
                    &payload,
                    created_at,
                    execution_id,
                )?)
            }
        }
        None => None,
    };
    connection
        .execute(
            "UPDATE executerecord SET input_id=?1,result_input_id=?2 WHERE id=?3",
            params![input_id, result_input_id, record_id],
        )
        .map_err(sql)?;
    Ok(input_id)
}

/// 清理历史前物化摘要，保留人工资产与独立事件、附件审计。
pub fn delete_execution_history(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let account_id = integer(request, "account_id")?;
    let connection = crate::storage_connection::open_writer(Path::new(path), false)?;
    crate::writer_scope::immediate(&connection, || {
        require_account(&connection, account_id)?;
        Ok(json!({"deleted": delete_execution_history_in(&connection, account_id)?}))
    })
}

/// 由历史清理与账户删除的外层事务共用，不能独立提交。
pub(crate) fn delete_execution_history_in(
    connection: &Connection,
    account_id: i64,
) -> Result<usize, AnalysisError> {
    let mut last_id = 0;
    loop {
        let records = connection
            .prepare(
                "SELECT id FROM executerecord WHERE account_id=?1 AND id>?2 ORDER BY id LIMIT 100",
            )
            .map_err(sql)?
            .query_map(params![account_id, last_id], |row| row.get::<_, i64>(0))
            .map_err(sql)?
            .collect::<Result<Vec<_>, _>>()
            .map_err(sql)?;
        if records.is_empty() {
            break;
        }
        for record_id in records {
            summary_refs::detach_execution_summaries(connection, record_id)?;
            last_id = record_id;
        }
    }
    connection
        .execute(
            "DELETE FROM account_asset_snapshot WHERE account_id=?1 AND source='execution'",
            [account_id],
        )
        .map_err(sql)?;
    // 0037 清理触发器同时移除结果子表，保留仍被审计引用的冻结输入。
    connection
        .execute(
            "DELETE FROM executerecord WHERE account_id=?1",
            [account_id],
        )
        .map_err(sql)
}

/// 账户删除先清理审计及所有资产历史，再移除该账户剩余的冻结输入。
pub(crate) fn delete_account_execution_inputs_in(
    connection: &Connection,
    account_id: i64,
) -> Result<(), AnalysisError> {
    connection
        .execute(
            "DELETE FROM execution_input WHERE account_id=?1",
            [account_id],
        )
        .map_err(sql)?;
    Ok(())
}

fn read_snapshot<T>(
    path: &str,
    read: impl FnOnce(&Connection) -> Result<T, AnalysisError>,
) -> Result<T, AnalysisError> {
    crate::with_read_snapshot_errors(
        Path::new(path),
        read,
        ("BUSINESS_FACTS_SQL", "BUSINESS_FACTS_SQL"),
    )
}

/// 内部调用只返回主记录标量，不读取完整历史结果。
pub fn recent_execution_records(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let account_id = integer(request, "account_id")?;
    let limit = integer(request, "limit")?;
    if limit < 0 {
        return Err(facts_error("执行记录数量不能为负"));
    }
    let success_only = request
        .get("success_only")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    read_snapshot(path, |connection| {
        let mut statement = connection
            .prepare(
                "SELECT * FROM executerecord WHERE account_id=?1 AND (?2=0 OR is_success=1)
                 ORDER BY id DESC LIMIT ?3",
            )
            .map_err(sql)?;
        let mut rows = statement
            .query(params![account_id, success_only, limit])
            .map_err(sql)?;
        let mut records = Vec::new();
        while let Some(row) = rows.next().map_err(sql)? {
            records.push(Value::Object(
                crate::business_detail::decode(row, "executerecord").map_err(facts_error)?,
            ));
        }
        Ok(Value::Array(records))
    })
}

pub fn read_execution_input(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let input_id = text(request, "input_id")?;
    read_snapshot(path, |connection| {
        facts::load_input_value(connection, input_id)
    })
}

fn execution_detail(connection: &Connection, request: &Value) -> Result<Value, AnalysisError> {
    let record_id = integer(request, "record_id")?;
    let account_id = integer(request, "account_id")?;
    let head = record_row(connection, record_id)?;
    let payload =
        crate::business_detail::read(connection, account_id, record_id).map_err(facts_error)?;
    let mut detail = Map::new();
    for name in [
        "id",
        "account_id",
        "execution_id",
        "created_at",
        "is_success",
    ] {
        detail.insert(name.into(), head.get(name).cloned().unwrap_or(Value::Null));
    }
    detail.insert("input".into(), payload["input"].clone());
    detail.insert("result".into(), payload["result"].clone());
    Ok(Value::Object(detail))
}

pub fn read_execution_detail(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    read_snapshot(path, |connection| execution_detail(connection, request))
}

pub fn read_execution_result(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let record_id = integer(request, "record_id")?;
    let account_id = integer(request, "account_id")?;
    read_snapshot(path, |connection| {
        crate::business_detail::read_result(connection, account_id, record_id).map_err(facts_error)
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn existing_record() -> Connection {
        let connection = Connection::open_in_memory().unwrap();
        let head_columns = result_head::execution_head_values(&json!({}), Some(0))
            .unwrap()
            .keys()
            .map(|name| format!("\"{name}\""))
            .collect::<Vec<_>>()
            .join(",");
        connection
            .execute_batch(&format!(
                "CREATE TABLE executerecord(id INTEGER PRIMARY KEY,account_id INTEGER,execution_id TEXT UNIQUE,
                 is_success INTEGER,created_at TEXT,input_id TEXT,result_input_id TEXT,facts_complete INTEGER,{head_columns});"
            ))
            .unwrap();
        connection.execute_batch(
            "CREATE TABLE account(id INTEGER PRIMARY KEY);
             INSERT INTO account VALUES(1);
             INSERT INTO executerecord(id,account_id,execution_id,is_success,created_at,facts_complete,
             result_input_kind,audit_evidence,termination_evidence,termination_kind,termination_cancel_ids_kind)
             VALUES(9,1,'terminal',0,'first',1,'absent','{}','{}','absent','absent');",
        ).unwrap();
        connection
    }

    #[test]
    fn repeated_terminal_returns_first_without_rewriting_facts() {
        let connection = existing_record();
        let request =
            json!({"account_id":1,"execution_id":"terminal","raw_result":{"changed":true}});
        let row = crate::writer_scope::immediate(&connection, || {
            persist_execute_record_in(&connection, &request)
        })
        .unwrap();
        assert_eq!(row["id"], json!(9));
        assert_eq!(row["created_at"], json!("first"));
        assert_eq!(row["facts_complete"], json!(true));
    }

    #[test]
    fn repeated_terminal_rejects_another_account() {
        let connection = existing_record();
        let request = json!({"account_id":2,"execution_id":"terminal"});
        let error = crate::writer_scope::immediate(&connection, || {
            persist_execute_record_in(&connection, &request)
        })
        .unwrap_err();
        assert!(error.error.contains("其他账户"));
        assert_eq!(record_row(&connection, 9).unwrap()["account_id"], json!(1));
    }

    #[test]
    fn failure_after_record_allocation_rolls_back_the_record() {
        let connection = existing_record();
        // 缺少事实表模拟编码或事实插入失败，不能留下未完成的终态头。
        let request = json!({"account_id":1,"execution_id":null,"created_at":"second",
            "is_success":0,"raw_result":{"inputs":{}},"result_input_id":"new-input"});
        let error = crate::writer_scope::immediate(&connection, || {
            persist_execute_record_in(&connection, &request)
        })
        .unwrap_err();
        assert!(error.error.contains("execution_input"));
        let count: i64 = connection
            .query_row("SELECT COUNT(*) FROM executerecord", [], |row| row.get(0))
            .unwrap();
        assert_eq!(count, 1);
    }

    #[test]
    fn missing_account_does_not_allocate_a_record() {
        let connection = existing_record();
        let error = crate::writer_scope::immediate(&connection, || {
            persist_execute_record_in(&connection, &json!({"account_id":2,"execution_id":"new"}))
        })
        .unwrap_err();
        assert_eq!(error.code, "ACCOUNT_NOT_FOUND");
    }
}
