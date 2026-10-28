//! 0037 删除可推导执行摘要；冲突证据阻止整个升级事务。
use super::{invalid, sql, PRIVATE_BACKUP_TABLE, PUBLIC_BACKUP_TABLE};
use crate::business_facts_ops::encode::py_eq;
use crate::error::AnalysisError;
use rusqlite::Connection;
use serde_json::{json, Value};

fn represented(value: &Value, fact: &Value) -> bool {
    match value {
        Value::Object(fields) => fields.iter().all(|(key, value)| {
            represented(value, &fact[key])
                || key == "error" && !value.is_null() && py_eq(value, &fact["outcome_reason"])
        }),
        Value::Array(values) => fact.as_array().is_some_and(|facts| {
            values.len() == facts.len() && values.iter().zip(facts).all(|(v, f)| represented(v, f))
        }),
        _ => value.is_null() || py_eq(value, fact),
    }
}

fn before_assets(connection: &Connection, execution: &str) -> Result<Value, AnalysisError> {
    let mut statement = connection.prepare("SELECT content FROM execution_artifact WHERE execution_id=?1 AND upper(artifact_type)='ACCOUNT_SNAPSHOT_BEFORE'").map_err(sql)?;
    let mut rows = statement.query([execution]).map_err(sql)?;
    let Some(row) = rows.next().map_err(sql)? else {
        return Ok(Value::Null);
    };
    let content: String = row.get(0).map_err(sql)?;
    let content = crate::json_codec::parse(&content).map_err(invalid)?;
    let artifact = json!({"execution_id":execution,"artifact_type":"ACCOUNT_SNAPSHOT_BEFORE","content":content});
    Ok(
        crate::business_facts_ops::summary_refs::resolve_artifact_content(
            connection, &artifact, None,
        )?["account_assets"]
            .clone(),
    )
}

pub(super) fn upgrade(connection: &Connection) -> Result<(), AnalysisError> {
    let mut statement = connection.prepare("SELECT a.execution_id,a.content,r.id,r.account_id FROM execution_artifact a LEFT JOIN executerecord r ON r.execution_id=a.execution_id AND r.facts_complete=1 WHERE upper(a.artifact_type)='EXECUTION_SUMMARY'").map_err(sql)?;
    let rows = statement
        .query_map([], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, Option<i64>>(2)?,
                row.get::<_, Option<i64>>(3)?,
            ))
        })
        .map_err(sql)?;
    for row in rows {
        let (execution, content, record, account) = row.map_err(sql)?;
        let record = record.ok_or_else(|| {
            invalid(format!(
                "0037: {execution}: summary has no complete result facts"
            ))
        })?;
        let summary: Value = serde_json::from_str(&content).map_err(|e| invalid(e.to_string()))?;
        let result = crate::business_detail::read_result(connection, account.unwrap(), record)
            .map_err(invalid)?;
        let view = crate::execution_view::build(&result, &before_assets(connection, &execution)?);
        let object = summary
            .as_object()
            .ok_or_else(|| invalid(format!("0037: {execution}: invalid summary")))?;
        for (key, value) in object {
            let fact = if view.get(key).is_some() {
                &view
            } else {
                &result
            };
            if !represented(&json!({key: value}), fact) {
                return Err(invalid(format!("0037: {execution}: independent summary field {key}; reconcile facts before upgrading")));
            }
        }
    }
    // 旧扩展中的独立对账无法保证重算等价，必须先明确处理。
    let independent: bool = connection.query_row("SELECT EXISTS(SELECT 1 FROM execution_extension WHERE reconciliation IS NOT NULL AND reconciliation!='null')",[],|r|r.get(0)).map_err(sql)?;
    if independent {
        return Err(invalid("0037: independent execution_extension reconciliation requires reconciliation before upgrading"));
    }
    // 摘要删除后不再有恢复目标；事实记录的错误码备份继续保留。
    for table in [PUBLIC_BACKUP_TABLE, PRIVATE_BACKUP_TABLE] {
        let exists: bool = connection
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name=?1)",
                [table],
                |row| row.get(0),
            )
            .map_err(sql)?;
        if exists {
            connection.execute(&format!("DELETE FROM {table} WHERE table_name='execution_artifact' AND json_column='content' AND row_id IN (SELECT CAST(id AS TEXT) FROM execution_artifact WHERE upper(artifact_type)='EXECUTION_SUMMARY')"), []).map_err(sql)?;
        }
    }
    connection.execute_batch("DELETE FROM execution_artifact WHERE upper(artifact_type)='EXECUTION_SUMMARY';
        ALTER TABLE execution_extension DROP COLUMN reconciliation;
        ALTER TABLE executerecord ADD COLUMN history_visible INTEGER NOT NULL DEFAULT 1;
        ALTER TABLE executerecord ADD COLUMN result_staged INTEGER NOT NULL DEFAULT 0;
        CREATE TRIGGER execution_summary_removed BEFORE INSERT ON execution_artifact WHEN upper(NEW.artifact_type)='EXECUTION_SUMMARY' BEGIN SELECT RAISE(ABORT,'EXECUTION_SUMMARY is derived from facts'); END;
        CREATE TRIGGER execution_history_visibility AFTER UPDATE OF history_visible,result_staged ON executerecord WHEN OLD.history_visible!=NEW.history_visible OR OLD.result_staged!=NEW.result_staged BEGIN
        INSERT INTO account_analysis(account_id,source_version) VALUES(NEW.account_id,1) ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL THEN 1 ELSE requested END,failures=0,retry_at=0,error=NULL; END;").map_err(sql)
}

pub(super) fn downgrade(connection: &Connection) -> Result<(), AnalysisError> {
    let hidden: bool = connection
        .query_row(
            "SELECT EXISTS(SELECT 1 FROM executerecord WHERE history_visible=0 OR result_staged=1)",
            [],
            |r| r.get(0),
        )
        .map_err(sql)?;
    if hidden {
        return Err(invalid(
            "0037: hidden history or staged worker results cannot be downgraded",
        ));
    }
    connection
        .execute_batch(
            "DROP TRIGGER execution_history_visibility; DROP TRIGGER execution_summary_removed;
        ALTER TABLE execution_extension ADD COLUMN reconciliation TEXT;
        ALTER TABLE executerecord DROP COLUMN history_visible;
        ALTER TABLE executerecord DROP COLUMN result_staged;",
        )
        .map_err(sql)
}
