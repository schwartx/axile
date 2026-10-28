//! Account-scoped raw facts, packed across tables and executions in bounded pages.
use crate::{
    browser_fact_schema::{FactTable, TABLES},
    error::AnalysisError,
};
use rusqlite::{
    params, params_from_iter,
    types::{Value as SqlValue, ValueRef},
    Connection, OptionalExtension,
};
use serde_json::{json, Value};
const MAX_BYTES: usize = 2 * 1024 * 1024;
const MAX_ROWS: usize = 4096;
fn sql(e: rusqlite::Error) -> AnalysisError {
    AnalysisError::caused_by("ANALYSIS_FACT_READ", e)
}
fn invalid(s: &str) -> AnalysisError {
    AnalysisError::new("INVALID_REQUEST", s)
}
fn integer(v: &Value) -> Result<i64, AnalysisError> {
    v.as_str()
        .and_then(|s| s.parse().ok())
        .filter(|v| *v >= 0)
        .ok_or_else(|| invalid("invalid synchronization cursor"))
}
fn account(request: &Value) -> Result<i64, AnalysisError> {
    request["account_id"]
        .as_i64()
        .filter(|v| *v > 0)
        .ok_or_else(|| invalid("invalid account_id"))
}
fn identity(c: &Connection) -> Result<String, AnalysisError> {
    c.query_row("SELECT identity FROM analysis_source", [], |r| r.get(0))
        .map_err(sql)
}
pub fn state(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let account = account(request)?;
    crate::with_read_snapshot(std::path::Path::new(path), |c| {
        let (source, channel, settings): (i64,String,Option<String>) = c.query_row("SELECT COALESCE(q.source_version,1),a.trade_channel,s.backtest FROM account a LEFT JOIN account_analysis q ON q.account_id=a.id LEFT JOIN account_settings s ON s.account_id=a.id WHERE a.id=?1", [account], |r| Ok((r.get(0)?,r.get(1)?,r.get(2)?))).optional().map_err(sql)?.ok_or_else(|| AnalysisError::new("ACCOUNT_NOT_FOUND", "账户不存在"))?;
        let settings: Value = settings
            .map(|s| serde_json::from_str(&s))
            .transpose()
            .map_err(|e| AnalysisError::caused_by("INVALID_ACCOUNT_SETTINGS", e))?
            .unwrap_or(json!({}));
        let cursor: i64 = c
            .query_row(
                "SELECT COALESCE(max(seq),0) FROM analysis_change WHERE account_id=?1",
                [account],
                |r| r.get(0),
            )
            .map_err(sql)?;
        Ok(
            json!({"schema_version":2,"source_id":identity(c)?,"source_version":source,"cursor":cursor.to_string(),"account_id":account,"trade_channel":channel,"settings":{"backtest_weight_type":"cs","backtest_fee_rate":settings["backtest_fee_rate"].as_f64().unwrap_or(0.0)}}),
        )
    })
}
fn scope(table: &FactTable, record: i64) -> String {
    if record == 0 {
        return if [
            "account",
            "portfolioaccount",
            "schedule_skip",
            "analysis_calendar_day",
        ]
        .contains(&table.name)
        {
            table.scope.into()
        } else {
            "0".into()
        };
    }
    match table.name {
        "account" | "portfolioaccount" | "schedule_skip" | "analysis_calendar_day" => "0".into(),
        "executerecord" => "id=?2 AND account_id=?1 AND facts_complete=1".into(),
        "execution_input" => "account_id=?1 AND id IN (SELECT input_id FROM executerecord WHERE id=?2 AND account_id=?1 UNION SELECT result_input_id FROM executerecord WHERE id=?2 AND account_id=?1)".into(),
        "execution_input_target" => "input_id IN (SELECT input_id FROM executerecord WHERE id=?2 AND account_id=?1 UNION SELECT result_input_id FROM executerecord WHERE id=?2 AND account_id=?1)".into(),
        "business_asset_snapshot" | "business_asset_position" => "asset_id IN (SELECT asset_id FROM business_asset_binding WHERE record_id=?2)".into(),
        "target_weight_snapshot" => "account_id=?1 AND execution_id=(SELECT execution_id FROM executerecord WHERE id=?2 AND account_id=?1)".into(),
        "target_weight_snapshot_weight" => "snapshot_id IN (SELECT id FROM target_weight_snapshot WHERE account_id=?1 AND execution_id=(SELECT execution_id FROM executerecord WHERE id=?2 AND account_id=?1))".into(),
        _ => "record_id=?2".into(),
    }
}
fn cell(row: &rusqlite::Row<'_>, i: usize) -> Result<Value, AnalysisError> {
    Ok(match row.get_ref(i).map_err(sql)? {
        ValueRef::Null => Value::Null,
        ValueRef::Integer(v) => json!(v),
        ValueRef::Real(v) => json!(v),
        ValueRef::Text(v) => {
            json!(std::str::from_utf8(v).map_err(|_| invalid("invalid fact text"))?)
        }
        _ => return Err(invalid("unsupported fact value")),
    })
}
pub fn page(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let account = account(request)?;
    let until = integer(&request["until"])?;
    let mut seq = integer(&request["cursor"]["seq"])?;
    let mut table_index = request["cursor"]["table"]
        .as_u64()
        .filter(|v| *v <= TABLES.len() as u64)
        .ok_or_else(|| invalid("invalid table cursor"))? as usize;
    let mut after = request["cursor"]["after"].clone();
    if seq > until {
        return Err(invalid("cursor is ahead of synchronization boundary"));
    }
    crate::with_read_snapshot(std::path::Path::new(path), |c| {
        if request["source_id"].as_str() != Some(&identity(c)?) {
            return Err(AnalysisError::new("SOURCE_CHANGED", "数据源已变化"));
        }
        if !c
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM account WHERE id=?1)",
                [account],
                |r| r.get::<_, bool>(0),
            )
            .map_err(sql)?
        {
            return Err(AnalysisError::new("ACCOUNT_NOT_FOUND", "账户不存在"));
        }
        let mut parts = Vec::new();
        let (mut bytes, mut count, mut completed) = (1024, 0, 0);
        let mut complete = false;
        loop {
            let current = if table_index < TABLES.len() {
                c.query_row(
                    "SELECT record_id FROM analysis_change WHERE account_id=?1 AND seq=?2",
                    params![account, seq],
                    |r| r.get::<_, i64>(0),
                )
                .optional()
                .map_err(sql)?
            } else {
                None
            };
            let record = if let Some(record) = current {
                record
            } else {
                let next = c.query_row("SELECT seq,record_id FROM analysis_change WHERE account_id=?1 AND seq>?2 AND seq<=?3 ORDER BY seq LIMIT 1", params![account,seq,until], |r| Ok((r.get::<_,i64>(0)?,r.get::<_,i64>(1)?))).optional().map_err(sql)?;
                let Some((next, record)) = next else {
                    seq = until;
                    table_index = TABLES.len();
                    after = Value::Null;
                    complete = true;
                    break;
                };
                seq = next;
                table_index = 0;
                after = Value::Null;
                record
            };
            let deleted = record != 0 && !c.query_row("SELECT EXISTS(SELECT 1 FROM executerecord WHERE id=?1 AND account_id=?2 AND facts_complete=1)", params![record,account], |r| r.get::<_,bool>(0)).map_err(sql)?;
            while !deleted && table_index < TABLES.len() {
                let table = &TABLES[table_index];
                let predicate = scope(table, record);
                if predicate == "0" {
                    table_index += 1;
                    continue;
                }
                let names = table.names();
                let mut args = vec![SqlValue::Integer(account), SqlValue::Integer(record)];
                let keys = table.key.split(',').collect::<Vec<_>>();
                let tail = if after.is_null() {
                    String::new()
                } else {
                    let values = after
                        .as_array()
                        .filter(|a| a.len() == keys.len())
                        .ok_or_else(|| invalid("invalid row cursor"))?;
                    for v in values {
                        args.push(SqlValue::Text(
                            v.as_str()
                                .filter(|s| s.len() <= 8192)
                                .ok_or_else(|| invalid("invalid row key"))?
                                .into(),
                        ));
                    }
                    format!(
                        " AND ({}) > ({})",
                        table.key,
                        (3..=args.len())
                            .map(|i| format!("?{i}"))
                            .collect::<Vec<_>>()
                            .join(",")
                    )
                };
                // Keep both bound parameters even for auxiliary scopes using only account_id.
                let query=format!("SELECT {} FROM {} WHERE (?2 IS NOT NULL) AND ({predicate}){tail} ORDER BY {} LIMIT {}",names.join(","),table.name,table.key,MAX_ROWS-count+1);
                let mut statement = c.prepare(&query).map_err(sql)?;
                let mut rows = statement.query(params_from_iter(args)).map_err(sql)?;
                let mut values = Vec::new();
                let mut full = false;
                while let Some(row) = rows.next().map_err(sql)? {
                    let v = (0..names.len())
                        .map(|i| cell(row, i))
                        .collect::<Result<Vec<_>, _>>()?;
                    let size = serde_json::to_vec(&v).unwrap().len() + 256;
                    if size + 1024 > MAX_BYTES {
                        return Err(AnalysisError::new(
                            "FACT_TOO_LARGE",
                            "单条事实超过 2 MiB 同步上限",
                        ));
                    }
                    if count == MAX_ROWS || bytes + size > MAX_BYTES {
                        full = true;
                        break;
                    }
                    after = json!(keys
                        .iter()
                        .map(
                            |key| match &v[names.iter().position(|n| n == key).unwrap()] {
                                Value::String(s) => s.clone(),
                                v => v.to_string(),
                            }
                        )
                        .collect::<Vec<_>>());
                    values.push(v);
                    bytes += size;
                    count += 1;
                }
                if !values.is_empty() {
                    parts.push(json!({"seq":seq.to_string(),"record_id":record.to_string(),"table":table.name,"rows":values}));
                }
                if full {
                    break;
                }
                table_index += 1;
                after = Value::Null;
            }
            if deleted || table_index == TABLES.len() {
                parts.push(json!({"seq":seq.to_string(),"record_id":record.to_string(),"table":null,"deleted":deleted}));
                table_index = TABLES.len();
                after = Value::Null;
                completed += 1;
            }
            if table_index < TABLES.len()
                || count == MAX_ROWS
                || completed == 32
                || bytes + 1024 > MAX_BYTES
            {
                break;
            }
        }
        Ok(
            json!({"schema_version":2,"source_id":request["source_id"],"from":request["cursor"],"until":request["until"],"parts":parts,"cursor":{"seq":seq.to_string(),"table":table_index,"after":after},"complete":complete}),
        )
    })
}
