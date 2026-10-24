//! 执行审计的持久化与读取;逐行隔离只适用于审计批次。

use std::path::Path;

use rusqlite::{params, types::ValueRef, Connection, Row};
use serde_json::{json, Map, Value};

use crate::business_facts_ops::{result_head, summary_refs};
use crate::{error::AnalysisError, storage_connection, writer_scope};

fn invalid(message: impl ToString) -> AnalysisError {
    AnalysisError::new("EXECUTION_AUDIT", message.to_string())
}

fn sql(error: rusqlite::Error) -> AnalysisError {
    AnalysisError::new("EXECUTION_AUDIT_SQL", error.to_string())
}

fn text<'a>(row: &'a Value, key: &str) -> Result<&'a str, AnalysisError> {
    row.get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| invalid(format!("审计字段 {key} 必须是字符串")))
}

fn integer(row: &Value, key: &str) -> Result<i64, AnalysisError> {
    row.get(key)
        .and_then(Value::as_i64)
        .ok_or_else(|| invalid(format!("审计字段 {key} 必须是整数")))
}

fn optional_text<'a>(row: &'a Value, key: &str) -> Result<Option<&'a str>, AnalysisError> {
    match row.get(key).unwrap_or(&Value::Null) {
        Value::Null => Ok(None),
        Value::String(value) => Ok(Some(value)),
        _ => Err(invalid(format!("审计字段 {key} 必须是字符串或 null"))),
    }
}

fn object_json(row: &Value, key: &str) -> Result<String, AnalysisError> {
    let value = row
        .get(key)
        .ok_or_else(|| invalid(format!("审计字段 {key} 缺失")))?;
    if !value.is_object() {
        return Err(invalid(format!("审计字段 {key} 必须是对象")));
    }
    serde_json::to_string(value).map_err(invalid)
}

fn inserted(result: rusqlite::Result<usize>, tokens: &[&str]) -> Result<bool, AnalysisError> {
    match result {
        Ok(_) => Ok(true),
        Err(error) => {
            let message = error.to_string().to_lowercase();
            if message.contains("unique") && tokens.iter().any(|token| message.contains(token)) {
                Ok(false)
            } else {
                Err(sql(error))
            }
        }
    }
}

fn insert_event(connection: &Connection, row: &Value) -> Result<bool, AnalysisError> {
    let event_type = text(row, "event_type")?.to_ascii_uppercase();
    inserted(
        connection.execute(
            "INSERT INTO execution_event(execution_id,account_id,channel,algorithm,schema_version,
             event_uid,event_type,status,reason_family,reason_code,symbol,intent_id,order_id,
             client_order_id,ts_local_created,ts_exchange,ts_persisted,seq,details)
             VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14,?15,?16,?17,?18,?19)",
            params![
                text(row, "execution_id")?,
                integer(row, "account_id")?,
                text(row, "channel")?,
                text(row, "algorithm")?,
                integer(row, "schema_version")?,
                text(row, "event_uid")?,
                event_type,
                text(row, "status")?,
                text(row, "reason_family")?,
                text(row, "reason_code")?,
                optional_text(row, "symbol")?,
                optional_text(row, "intent_id")?,
                optional_text(row, "order_id")?,
                optional_text(row, "client_order_id")?,
                text(row, "ts_local_created")?,
                optional_text(row, "ts_exchange")?,
                text(row, "ts_persisted")?,
                integer(row, "seq")?,
                object_json(row, "details")?,
            ],
        ),
        &["execution_event.event_uid", "uq_execution_event_event_uid"],
    )
}

fn insert_artifact(connection: &Connection, row: &Value) -> Result<bool, AnalysisError> {
    inserted(
        connection.execute(
            "INSERT INTO execution_artifact(execution_id,artifact_type,schema_version,content,created_at)
             VALUES(?1,?2,?3,?4,?5)",
            params![text(row, "execution_id")?, text(row, "artifact_type")?.to_ascii_uppercase(),
                integer(row, "schema_version")?, object_json(row, "content")?, text(row, "created_at")?],
        ),
        &["execution_artifact.execution_id", "execution_artifact.artifact_type",
            "uq_execution_artifact_execution_type"],
    )
}

pub fn append_execution_event(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let connection = storage_connection::open_writer(Path::new(path), false)?;
    writer_scope::immediate(&connection, || {
        insert_event(&connection, request).map(Value::Bool)
    })
}

pub fn append_execution_artifact(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let connection = storage_connection::open_writer(Path::new(path), false)?;
    writer_scope::immediate(&connection, || {
        insert_artifact(&connection, request).map(Value::Bool)
    })
}

fn notification(row: &Value) -> Value {
    json!({"execution_id":row["execution_id"],"account_id":row["account_id"],
        "event_type":row["event_type"],"symbol":row.get("symbol"),"seq":row["seq"]})
}

fn persist_batch(connection: &Connection, entries: &[Value]) -> Result<Value, AnalysisError> {
    writer_scope::immediate(connection, || {
        let mut count = 0;
        let mut notifications = Vec::new();
        let mut failures = Vec::new();
        for (index, entry) in entries.iter().enumerate() {
            connection
                .execute_batch("SAVEPOINT audit_row")
                .map_err(sql)?;
            let row = entry.get("row").unwrap_or(&Value::Null);
            let kind = entry.get("kind").and_then(Value::as_str);
            let result = match kind {
                Some("event") => insert_event(connection, row),
                Some("artifact") => insert_artifact(connection, row),
                _ => Err(invalid(
                    entry
                        .get("error")
                        .and_then(Value::as_str)
                        .unwrap_or("审计行类型无效"),
                )),
            };
            match result {
                Ok(inserted) => {
                    connection.execute_batch("RELEASE audit_row").map_err(sql)?;
                    if inserted {
                        count += 1;
                        if kind == Some("event") {
                            notifications.push(notification(row));
                        }
                    }
                }
                Err(error) => {
                    connection
                        .execute_batch("ROLLBACK TO audit_row; RELEASE audit_row")
                        .map_err(sql)?;
                    failures.push(json!({"index":index,"error":error.error}));
                }
            }
        }
        Ok(json!({"count":count,"notifications":notifications,"failures":failures}))
    })
}

pub fn append_execution_audit_batch(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let entries = request
        .get("entries")
        .and_then(Value::as_array)
        .ok_or_else(|| invalid("审计批次必须包含 entries"))?;
    if entries.len() > 8 {
        return Err(invalid("审计批次超过固定上限 8"));
    }
    if entries.is_empty() {
        return Ok(json!({"count":0,"notifications":[],"failures":[]}));
    }
    let connection = storage_connection::open_writer(Path::new(path), false)?;
    persist_batch(&connection, entries)
}

pub(crate) fn decode_row(row: &Row<'_>, table: &str) -> Result<Value, AnalysisError> {
    let mut result = Map::new();
    for (index, name) in row.as_ref().column_names().iter().enumerate() {
        let json_column = matches!(
            (table, *name),
            ("execution_event", "details")
                | ("execution_artifact", "content")
                | ("execution_intent", "payload")
                | ("account_control_event", "metadata")
        );
        let mut value = match row.get_ref(index).map_err(sql)? {
            ValueRef::Null => Value::Null,
            ValueRef::Integer(value) if table == "account_control_event" && *name == "counted" => {
                json!(value != 0)
            }
            ValueRef::Integer(value) => json!(value),
            ValueRef::Real(value) => Value::Number(
                serde_json::Number::from_f64(value).ok_or_else(|| invalid("审计数字不是有限值"))?,
            ),
            ValueRef::Text(bytes) => {
                let value = std::str::from_utf8(bytes).map_err(invalid)?;
                if json_column {
                    serde_json::from_str(value).map_err(invalid)?
                } else {
                    json!(value)
                }
            }
            ValueRef::Blob(_) => return Err(invalid("审计列不是文本")),
        };
        if matches!(
            (table, *name),
            ("execution_event", "event_type")
                | ("execution_artifact", "artifact_type")
                | ("account_control_event", "decision")
                | (
                    "account_control_counter_delta",
                    "bucket_type" | "scope_type"
                )
        ) {
            if let Some(value_text) = value.as_str() {
                value = json!(value_text.to_ascii_lowercase());
            }
        }
        result.insert((*name).to_owned(), value);
    }
    Ok(Value::Object(result))
}

fn list(
    connection: &Connection,
    request: &Value,
    table: &str,
    public: bool,
) -> Result<Value, AnalysisError> {
    let execution_id = text(request, "execution_id")?;
    let skip = request.get("skip").and_then(Value::as_i64).unwrap_or(0);
    let limit = request.get("limit").and_then(Value::as_i64).unwrap_or(100);
    if skip < 0 || !(1..=500).contains(&limit) {
        return Err(invalid("审计分页范围无效"));
    }
    let count: i64 = connection
        .query_row(
            &format!("SELECT COUNT(*) FROM {table} WHERE execution_id=?1"),
            [execution_id],
            |row| row.get(0),
        )
        .map_err(sql)?;
    let columns = match (table, public) {
        ("execution_event", true) => "id,execution_id,account_id,channel,algorithm,schema_version,event_uid,event_type,status,reason_family,reason_code,symbol,intent_id,order_id,client_order_id,ts_local_created,ts_exchange,ts_persisted,seq,details",
        ("execution_artifact", true) => "id,execution_id,artifact_type,schema_version,content,created_at",
        _ => "*",
    };
    let mut statement = connection
        .prepare(&format!(
            "SELECT {columns} FROM {table} WHERE execution_id=?1 ORDER BY id LIMIT ?2 OFFSET ?3"
        ))
        .map_err(sql)?;
    let mut cursor = statement
        .query(params![execution_id, limit, skip])
        .map_err(sql)?;
    let mut data = Vec::new();
    while let Some(row) = cursor.next().map_err(sql)? {
        let mut item = decode_row(row, table)?;
        if public {
            integer(&item, "id")?;
            integer(&item, "schema_version")?;
        }
        if table == "execution_event" {
            item["details"] = summary_refs::resolve_event_details(connection, &item, None)?;
            if public {
                public_event(&mut item)?;
            }
        } else {
            item["content"] = summary_refs::resolve_artifact_content(connection, &item, None)?;
            if public {
                enum_field(&item, "artifact_type", "standard_input|target_snapshot|account_snapshot|account_snapshot_before|trade_rules_snapshot|market_snapshot|execution_summary|environment_snapshot")?;
                if !item["content"].is_object() {
                    return Err(invalid("附件内容必须是对象"));
                }
                if item["artifact_type"] == "execution_summary" {
                    item["content"] = result_head::normalize_legacy_result(&item["content"])?;
                }
            }
        }
        data.push(item);
    }
    Ok(json!({"count":count,"data":data}))
}

fn enum_field(item: &Value, key: &str, choices: &str) -> Result<(), AnalysisError> {
    if choices
        .split('|')
        .any(|choice| item[key].as_str() == Some(choice))
    {
        Ok(())
    } else {
        Err(invalid(format!("审计字段 {key} 无效")))
    }
}

fn public_event(item: &mut Value) -> Result<(), AnalysisError> {
    integer(item, "account_id")?;
    integer(item, "seq")?;
    enum_field(item, "event_type", "execution_started|execution_termination_requested|execution_termination_acked|execution_terminated|input_snapshotted|instruments_waiting|instruments_ready|instruments_failed|target_computed|futures_close_planned|symbol_decision_made|symbol_skipped|order_submitted|order_acknowledged|order_terminal|execution_completed|execution_failed")?;
    enum_field(item, "status", "INFO|SUCCESS|WARNING|ERROR")?;
    enum_field(
        item,
        "reason_family",
        "INPUT|RISK|MARKET_RULE|ACCOUNT_STATE|EXECUTION_STRATEGY|EXCHANGE|SYSTEM",
    )?;
    let channel = text(item, "channel")?
        .trim_matches(|ch: char| ch.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&ch))
        .to_lowercase();
    if channel.is_empty() || !item["details"].is_object() {
        return Err(invalid("事件渠道或详情无效"));
    }
    item["channel"] = json!(channel);
    result_head::normalize_event_details(&mut item["details"]);
    Ok(())
}

fn list_snapshot(
    path: &str,
    request: &Value,
    table: &str,
    public: bool,
) -> Result<Value, AnalysisError> {
    crate::with_read_snapshot_errors(
        Path::new(path),
        |connection| list(connection, request, table, public),
        ("EXECUTION_AUDIT_SQL", "EXECUTION_AUDIT_SQL"),
    )
}

pub fn list_execution_events(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    list_snapshot(path, request, "execution_event", false)
}

pub fn list_execution_artifacts(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    list_snapshot(path, request, "execution_artifact", false)
}

pub fn list_execution_events_json(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    list_snapshot(path, request, "execution_event", true)
}

pub fn list_execution_artifacts_json(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    list_snapshot(path, request, "execution_artifact", true)
}

pub fn resolve_execution_artifact(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let artifact = request
        .get("artifact")
        .filter(|value| value.is_object())
        .ok_or_else(|| AnalysisError::new("BUSINESS_FACTS_REFERENCE", "审计附件不是对象"))?;
    let account_id =
        match request.get("account_id") {
            None | Some(Value::Null) => None,
            Some(value) => Some(value.as_i64().ok_or_else(|| {
                AnalysisError::new("BUSINESS_FACTS_REFERENCE", "审计账户身份无效")
            })?),
        };
    let connection = storage_connection::open_reader(Path::new(path))?;
    connection.execute_batch("BEGIN").map_err(sql)?;
    let result = summary_refs::resolve_artifact_content(&connection, artifact, account_id);
    let rollback = connection.execute_batch("ROLLBACK").map_err(sql);
    match result {
        Ok(value) => rollback.map(|_| value),
        Err(error) => Err(error),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn schema() -> Connection {
        let connection = Connection::open_in_memory().unwrap();
        connection
            .execute_batch(
                "CREATE TABLE execution_event(id INTEGER PRIMARY KEY,
            execution_id TEXT NOT NULL,account_id INTEGER NOT NULL,channel TEXT NOT NULL,
            algorithm TEXT NOT NULL,schema_version INTEGER NOT NULL,event_uid TEXT UNIQUE NOT NULL,
            event_type TEXT NOT NULL,status TEXT NOT NULL,reason_family TEXT NOT NULL,
            reason_code TEXT NOT NULL CHECK(reason_code!='SQL.FAIL'),symbol TEXT,intent_id TEXT,order_id TEXT,client_order_id TEXT,
            ts_local_created TEXT NOT NULL,ts_exchange TEXT,ts_persisted TEXT NOT NULL,
            seq INTEGER NOT NULL,details JSON NOT NULL);
            CREATE TABLE execution_artifact(id INTEGER PRIMARY KEY,execution_id TEXT NOT NULL,
            artifact_type TEXT NOT NULL,schema_version INTEGER NOT NULL,content JSON NOT NULL,
            created_at TEXT NOT NULL,UNIQUE(execution_id,artifact_type));",
            )
            .unwrap();
        connection
    }

    fn event(uid: &str, seq: i64) -> Value {
        json!({"execution_id":"e1","account_id":7,"channel":"ctp","algorithm":"x",
            "schema_version":1,"event_uid":uid,"event_type":"execution_started","status":"INFO",
            "reason_family":"SYSTEM","reason_code":"started","ts_local_created":"t",
            "ts_persisted":"t","seq":seq,"details":{}})
    }

    #[test]
    fn batch_isolates_duplicate_and_bad_rows_and_returns_only_committed_notifications() {
        let connection = schema();
        let first = event("first", 1);
        let mut bad = event("bad", 2);
        bad["reason_code"] = Value::Null;
        let mut rejected = event("rejected", 2);
        rejected["reason_code"] = json!("SQL.FAIL");
        let artifact = json!({"execution_id":"e1","artifact_type":"standard_input",
            "schema_version":1,"content":{},"created_at":"t"});
        let receipt = persist_batch(
            &connection,
            &[
                json!({"kind":"event","row":first}),
                json!({"kind":"event","row":first}),
                json!({"kind":"artifact","row":artifact}),
                json!({"kind":"artifact","row":artifact}),
                json!({"kind":"event","row":bad}),
                json!({"kind":"invalid","error":"invalid JSON"}),
                json!({"kind":"event","row":rejected}),
                json!({"kind":"event","row":event("last",0)}),
            ],
        )
        .unwrap();
        assert_eq!(receipt["count"], 3);
        assert_eq!(receipt["failures"].as_array().unwrap().len(), 3);
        assert_eq!(receipt["notifications"].as_array().unwrap().len(), 2);
        assert!(connection.is_autocommit());
        let page = list(
            &connection,
            &json!({"execution_id":"e1","skip":1,"limit":1}),
            "execution_event",
            false,
        )
        .unwrap();
        assert_eq!(page["count"], 2);
        assert_eq!(page["data"][0]["seq"], 0);
        assert_eq!(page["data"][0]["event_type"], "execution_started");
    }

    #[test]
    fn failed_commit_rolls_back_every_audit_row() {
        let connection = schema();
        connection.execute_batch("CREATE TABLE frozen_stage(name TEXT PRIMARY KEY,payload JSON);
            BEGIN; INSERT INTO frozen_stage VALUES('pre','{\"target\":0}'); COMMIT;
            BEGIN; INSERT INTO frozen_stage VALUES('post','{\"asset\":101}'); COMMIT;
            PRAGMA foreign_keys=ON; CREATE TABLE parent(id INTEGER PRIMARY KEY);
            CREATE TABLE deferred(parent_id INTEGER REFERENCES parent(id) DEFERRABLE INITIALLY DEFERRED);
            CREATE TRIGGER fail_commit AFTER INSERT ON execution_event BEGIN
              INSERT INTO deferred VALUES(999); END;").unwrap();
        assert!(persist_batch(
            &connection,
            &[json!({"kind":"event","row":event("first",1)})]
        )
        .is_err());
        assert_eq!(
            connection
                .query_row("SELECT COUNT(*) FROM execution_event", [], |row| row
                    .get::<_, i64>(0))
                .unwrap(),
            0
        );
        assert!(connection.is_autocommit());
        let stages: i64 = connection
            .query_row("SELECT COUNT(*) FROM frozen_stage", [], |row| row.get(0))
            .unwrap();
        assert_eq!(stages, 2);
    }

    #[test]
    fn artifact_resolver_is_read_only_and_checks_explicit_account_and_execution() {
        let path = std::env::temp_dir().join(format!(
            "axile-audit-artifact-{}.sqlite",
            std::process::id()
        ));
        let connection = Connection::open(&path).unwrap();
        connection
            .execute_batch(
                "CREATE TABLE execution_input(id TEXT PRIMARY KEY,account_id INTEGER,
            execution_id TEXT,owner_execution_id TEXT,root_kind TEXT,root_value JSON);
            INSERT INTO execution_input VALUES('input',7,'e1','e1','scalar','\"frozen\"');",
            )
            .unwrap();
        let before = std::fs::read(&path).unwrap();
        let artifact = json!({"execution_id":"e1","artifact_type":"standard_input","content":{
            "business_facts_ref":{"version":1,"view":"input","input_id":"input"}}});
        let result = resolve_execution_artifact(
            path.to_str().unwrap(),
            &json!({"artifact":artifact,"account_id":7}),
        )
        .unwrap();
        assert_eq!(result["input"], "frozen");
        assert_eq!(
            result["business_facts_ref"],
            artifact["content"]["business_facts_ref"]
        );
        let wrong_account = resolve_execution_artifact(
            path.to_str().unwrap(),
            &json!({"artifact":artifact,"account_id":8}),
        )
        .unwrap_err();
        assert_eq!(wrong_account.code, "BUSINESS_FACTS_REFERENCE");
        let mut foreign = artifact;
        foreign["execution_id"] = json!("e2");
        let wrong_execution = resolve_execution_artifact(
            path.to_str().unwrap(),
            &json!({"artifact":foreign,"account_id":7}),
        )
        .unwrap_err();
        assert_eq!(wrong_execution.code, "BUSINESS_FACTS_REFERENCE");
        assert_eq!(std::fs::read(&path).unwrap(), before);
        drop(connection);
        std::fs::remove_file(path).unwrap();
    }
}
