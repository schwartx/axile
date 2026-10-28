//! 执行终态、冻结输入及历史删除的完整存储操作。

use std::path::Path;

use rusqlite::{params, Connection};
use serde_json::{json, Map, Value};

use crate::business_facts_ops::{
    self as facts, facts_error, result_extensions, result_head, result_orders, result_symbols,
    result_trades, sql,
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

/// 首条执行结果、任务终态、补发结算与待发通知在同一事务提交。
pub fn persist_execution_state(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let connection = crate::storage_connection::open_writer(Path::new(path), false)?;
    crate::writer_scope::immediate(&connection, || {
        let record = persist_execute_record_in(&connection, request)?;
        if request["stage_only"] == true {
            return Ok(record);
        }
        finalize_staged_result(&connection, &record, request)?;
        let record = Value::Object(record_row(&connection, record["id"].as_i64().unwrap())?);
        if let Some(execution_id) = record["execution_id"].as_str() {
            let status = record["task_status"]
                .as_str()
                .unwrap_or(if record["is_success"] == 1 {
                    "SUCCEEDED"
                } else {
                    "FAILED"
                });
            let finished = record["termination_finished_at"]
                .as_str()
                .or(record["created_at"].as_str());
            crate::intents::finish_in_connection(
                &connection,
                &json!({
                    "execution_id": execution_id, "account_id": record["account_id"],
                    "status": status, "error": record["display_error"], "finished_at": finished, "now": finished,
                }),
            )?;
        }
        crate::execution_reads::record_status(
            &connection,
            record.as_object().expect("persisted record"),
        )
    })
}

fn finalize_staged_result(
    connection: &Connection,
    record: &Value,
    request: &Value,
) -> Result<(), AnalysisError> {
    if record["result_staged"] != true && record["result_staged"] != 1 {
        return Ok(());
    }
    let is_success = integer(request, "is_success")?;
    if !matches!(is_success, 0 | 1) {
        return Err(facts_error("执行成功标识必须为 0 或 1"));
    }
    let raw_input = request.get("raw_input").filter(|value| !value.is_null());
    if raw_input.is_some_and(|value| !value.is_object()) {
        return Err(facts_error("执行输入必须是对象"));
    }
    attach_inputs(
        connection,
        request,
        record["id"].as_i64().unwrap(),
        raw_input,
        &request["raw_result"],
    )?;
    {
        let head = result_head::execution_head_values(
            &request["raw_result"],
            Some(is_success),
        )?;
        for (name, value) in head.iter().filter(|(name, _)| {
            name.as_str() == "task_status"
                || name.as_str() == "result_input_kind"
                || name.as_str() == "result_input_value"
                || name.starts_with("termination_")
                || name.as_str() == "execution_kind"
                    && request["raw_result"]
                        .get(name)
                        .is_some_and(|v| !v.is_null())
                || (head["task_status"] == "FAILED" && name.as_str() == "display_error")
        }) {
            let value = match value {
                Value::Null => rusqlite::types::Value::Null,
                Value::Bool(v) => rusqlite::types::Value::Integer(i64::from(*v)),
                Value::Number(v) => rusqlite::types::Value::Integer(v.as_i64().unwrap_or_default()),
                Value::String(v) => rusqlite::types::Value::Text(v.clone()),
                _ => rusqlite::types::Value::Text(value.to_string()),
            };
            connection
                .execute(
                    &format!("UPDATE executerecord SET {name}=?1 WHERE id=?2"),
                    params![value, record["id"].as_i64()],
                )
                .map_err(sql)?;
        }
        connection
            .execute(
                "DELETE FROM execution_termination_cancel_failure WHERE record_id=?1",
                [record["id"].as_i64().unwrap()],
            )
            .map_err(sql)?;
        result_head::write_result_cancellations(
            connection,
            record["id"].as_i64().unwrap(),
            &request["raw_result"],
        )?;
        connection
            .execute(
                "UPDATE executerecord SET result_staged=0,is_success=?1 WHERE id=?2",
                params![is_success, record["id"].as_i64()],
            )
            .map_err(sql)?;
    }
    Ok(())
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

    result_head::insert_execution(connection, &head)?;
    let record_id = connection.last_insert_rowid();
    if request["stage_only"] == true {
        connection
            .execute(
                "UPDATE executerecord SET result_staged=1 WHERE id=?1",
                [record_id],
            )
            .map_err(sql)?;
    }
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
            let same = match input_id.as_deref() {
                Some(input_id) => facts::replay_matches(connection, input_id, payload)?,
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
                    payload,
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

/// 隐藏历史但保留审计所需的唯一事实。
pub fn delete_execution_history(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let account_id = integer(request, "account_id")?;
    let connection = crate::storage_connection::open_writer(Path::new(path), false)?;
    crate::writer_scope::immediate(&connection, || {
        require_account(&connection, account_id)?;
        connection
            .execute(
                "DELETE FROM account_asset_snapshot WHERE account_id=?1 AND source='execution'",
                [account_id],
            )
            .map_err(sql)?;
        let count = connection.execute("UPDATE executerecord SET history_visible=0 WHERE account_id=?1 AND history_visible=1", [account_id]).map_err(sql)?;
        Ok(json!({"deleted": count}))
    })
}

/// 账户删除的物理清理由外层事务统一提交。
pub(crate) fn delete_execution_history_in(
    connection: &Connection,
    account_id: i64,
) -> Result<usize, AnalysisError> {
    connection
        .execute(
            "DELETE FROM account_asset_snapshot WHERE account_id=?1 AND source='execution'",
            [account_id],
        )
        .map_err(sql)?;
    // 清理触发器同时移除结果子表，保留仍被审计引用的冻结输入。
    connection
        .execute(
            "DELETE FROM executerecord WHERE account_id=?1",
            [account_id],
        )
        .map_err(sql)
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
