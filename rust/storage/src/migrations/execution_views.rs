//! 0033 摘要中的旧订单时间差异保留为稀疏证据，其余独立证据阻止整个事务。
use super::{invalid, sql, PRIVATE_BACKUP_TABLE, PUBLIC_BACKUP_TABLE};
use crate::business_facts_ops::encode::py_eq;
use crate::error::AnalysisError;
use chrono::NaiveDateTime;
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

fn legacy_order_time(order: &Value) -> Option<&str> {
    if order.get("update_timestamp").is_some()
        || order["extra"].as_object()?.contains_key("_update_time_str")
    {
        return None;
    }
    let time = order["update_time"].as_str()?;
    NaiveDateTime::parse_from_str(time, "%Y-%m-%dT%H:%M:%S%.f").ok()?;
    Some(time)
}

fn reconcile_order_times(
    connection: &Connection,
    record: i64,
    summary: &mut Value,
    result: &Value,
) -> Result<Vec<Value>, AnalysisError> {
    let mut statement = connection
        .prepare("SELECT path,ordinal FROM execution_order WHERE record_id=?1 AND kind='object'")
        .map_err(sql)?;
    let rows = statement
        .query_map([record], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?))
        })
        .map_err(sql)?;
    let mut differences = Vec::new();
    for row in rows {
        let (path, ordinal) = row.map_err(sql)?;
        let path = format!("{path}/{ordinal}");
        let Some(order) = summary.pointer_mut(&path) else {
            continue;
        };
        let Some(fact) = result.pointer(&path) else {
            continue;
        };
        let (Some(before), Some(after)) = (legacy_order_time(order), legacy_order_time(fact))
        else {
            continue;
        };
        if before == after || fact["order_id"].as_str().is_none_or(str::is_empty) {
            continue;
        }
        let mut comparable = order.clone();
        comparable["update_time"] = json!(after);
        let mut original_fact = fact.clone();
        // availability 由读取器生成，不属于旧订单载荷。
        original_fact
            .as_object_mut()
            .unwrap()
            .remove("availability");
        if py_eq(&comparable, &original_fact) {
            differences.push(json!({"path":path,"order_id":fact["order_id"],
                "summary_update_time":before,"result_update_time":after}));
            *order = comparable;
        }
    }
    Ok(differences)
}

fn preserve_order_times(
    connection: &Connection,
    record: i64,
    artifact: i64,
    differences: Vec<Value>,
) -> Result<(), AnalysisError> {
    if differences.is_empty() {
        return Ok(());
    }
    let source = connection
        .query_row(
            "SELECT artifact_type,schema_version,created_at FROM execution_artifact WHERE id=?1",
            [artifact],
            |row| {
                Ok(
                    json!({"artifact_id":artifact,"artifact_type":row.get::<_,String>(0)?,
            "schema_version":row.get::<_,i64>(1)?,"created_at":row.get::<_,String>(2)?,
            "orders":differences}),
                )
            },
        )
        .map_err(sql)?;
    let evidence: String = connection
        .query_row(
            "SELECT audit_evidence FROM execution_extension WHERE record_id=?1",
            [record],
            |row| row.get(0),
        )
        .map_err(sql)?;
    let mut evidence: Value =
        serde_json::from_str(&evidence).map_err(|e| invalid(e.to_string()))?;
    evidence
        .as_object_mut()
        .ok_or_else(|| invalid("0034: invalid result audit evidence"))?
        .entry("legacy_order_update_times")
        .or_insert_with(|| json!([]))
        .as_array_mut()
        .ok_or_else(|| invalid("0034: invalid legacy order time evidence"))?
        .push(source);
    connection
        .execute(
            "UPDATE execution_extension SET audit_evidence=?1 WHERE record_id=?2",
            rusqlite::params![evidence.to_string(), record],
        )
        .map_err(sql)?;
    Ok(())
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
    let mut statement = connection.prepare("SELECT a.execution_id,a.content,r.id,r.account_id,a.id FROM execution_artifact a LEFT JOIN executerecord r ON r.execution_id=a.execution_id AND r.facts_complete=1 WHERE upper(a.artifact_type)='EXECUTION_SUMMARY' ORDER BY a.id").map_err(sql)?;
    let rows = statement
        .query_map([], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, Option<i64>>(2)?,
                row.get::<_, Option<i64>>(3)?,
                row.get::<_, i64>(4)?,
            ))
        })
        .map_err(sql)?;
    for row in rows {
        let (execution, content, record, account, artifact) = row.map_err(sql)?;
        let record = record.ok_or_else(|| {
            invalid(format!(
                "0034: {execution}: summary has no complete result facts"
            ))
        })?;
        let mut summary: Value =
            serde_json::from_str(&content).map_err(|e| invalid(e.to_string()))?;
        let result = crate::business_detail::read_result(connection, account.unwrap(), record)
            .map_err(invalid)?;
        let differences = reconcile_order_times(connection, record, &mut summary, &result)?;
        let view = crate::execution_view::build(&result, &before_assets(connection, &execution)?);
        let object = summary
            .as_object()
            .ok_or_else(|| invalid(format!("0034: {execution}: invalid summary")))?;
        for (key, value) in object {
            let fact = if view.get(key).is_some() {
                &view
            } else {
                &result
            };
            if !represented(&json!({key: value}), fact) {
                return Err(invalid(format!("0034: {execution}: independent summary field {key}; reconcile facts before upgrading")));
            }
        }
        preserve_order_times(connection, record, artifact, differences)?;
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
    connection
        .execute(
            "DELETE FROM execution_artifact WHERE upper(artifact_type)='EXECUTION_SUMMARY'",
            [],
        )
        .map_err(sql)?;
    Ok(())
}
