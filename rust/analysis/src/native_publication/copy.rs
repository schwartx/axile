//! Copy frozen scalar source facts without materializing historical rows.

use chrono::{DateTime, NaiveDateTime};
use rusqlite::{params, Connection};

use crate::error::AnalysisError;

const EXECUTION_COLUMNS: &str = "record_id,time,success,noop,key,execution_id,created_at,is_success,duration_sec,trade_count,symbol_count,position_count,positions_present,reason,raw_present,has_symbol_results,raw_status,raw_task_status,raw_error,raw_outcome,raw_outcome_reason,raw_reason_code,raw_msg,raw_execution_kind";
const TRADE_COLUMNS: &str = "record_id,symbol,time,day,value,cost,loss_bp,fee,fee_currency,estimated,side,quantity,price,reference,reference_source";
const CHILDREN: &[(&str, &str)] = &[
    ("execution_symbol", "record_id,symbol,present,status,error,outcome,outcome_reason,reason_code"),
    ("position", "record_id,idx,symbol,quantity,direction"),
    ("attempt", "record_id,idx,symbol,planned,filled,reason"),
    ("transaction", "record_id,idx,symbol,side,quantity,price,reference,reference_source,time,end_time,time_estimated,before,target,value,cost,loss_bp,coverage,covered,count,amount_complete,fee_covered"),
    ("transaction_fee", "record_id,transaction_idx,currency,value"),
];

pub(super) const COST_TABLES: &[&str] = &[
    "performance_cost_transaction_fee",
    "performance_cost_transaction",
    "performance_cost_attempt",
    "performance_cost_position",
    "performance_cost_execution_symbol",
    "performance_cost_trade",
    "performance_cost_execution",
    "performance_cost_range",
];

fn sql(error: rusqlite::Error) -> AnalysisError {
    AnalysisError::new("PUBLICATION_WRITE", error.to_string())
}

const ACCOUNT_SOURCES: &str = "SELECT s.record_id FROM main.analysis_source_execution s JOIN main.executerecord e ON e.id=s.record_id WHERE s.account_id=?2 AND e.account_id=?2";

pub(super) fn source_facts(
    connection: &Connection,
    snapshot_id: &str,
    account: i64,
) -> Result<(), AnalysisError> {
    crate::source::validate(connection, account).map_err(AnalysisError::from)?;
    let query = format!(
        "INSERT INTO main.performance_cost_execution (snapshot_id,{EXECUTION_COLUMNS},payload)
         SELECT ?1,{EXECUTION_COLUMNS},'{{}}' FROM main.analysis_source_execution
         WHERE record_id IN ({ACCOUNT_SOURCES})"
    );
    connection
        .execute(&query, params![snapshot_id, account])
        .map_err(sql)?;
    let query = format!(
        "INSERT INTO main.performance_cost_trade (snapshot_id,id,{TRADE_COLUMNS},payload)
         SELECT ?1,ROW_NUMBER() OVER (ORDER BY record_id,trade_index)-1,{TRADE_COLUMNS},'{{}}'
         FROM main.analysis_source_trade WHERE record_id IN ({ACCOUNT_SOURCES})"
    );
    connection
        .execute(&query, params![snapshot_id, account])
        .map_err(sql)?;
    for &(suffix, columns) in CHILDREN {
        let query = format!(
            "INSERT INTO main.performance_cost_{suffix} (snapshot_id,{columns})
             SELECT ?1,{columns} FROM main.analysis_source_{suffix}
             WHERE record_id IN ({ACCOUNT_SOURCES})"
        );
        connection
            .execute(&query, params![snapshot_id, account])
            .map_err(sql)?;
    }
    Ok(())
}

pub(super) fn ranges(connection: &Connection, snapshot_id: &str) -> Result<(), AnalysisError> {
    let mut statement = connection.prepare(
        "SELECT range_key,baseline,end FROM native_stage.performance_result_range ORDER BY range_key"
    ).map_err(sql)?;
    let rows = statement
        .query_map([], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, Option<String>>(1)?,
                row.get::<_, Option<String>>(2)?,
            ))
        })
        .map_err(sql)?;
    for row in rows {
        let (range, baseline, end) = row.map_err(sql)?;
        let baseline = baseline.as_deref().map(timestamp).transpose()?;
        let end = end.as_deref().map(timestamp).transpose()?;
        connection.execute(
            "INSERT INTO main.performance_cost_range (snapshot_id,range_key,baseline,end) VALUES (?1,?2,?3,?4)",
            params![snapshot_id, range, baseline, end],
        ).map_err(sql)?;
    }
    Ok(())
}

fn timestamp(value: &str) -> Result<f64, AnalysisError> {
    if let Ok(time) = DateTime::parse_from_rfc3339(value) {
        return Ok(time.timestamp_micros() as f64 / 1_000_000.0 * 1000.0);
    }
    let parsed = NaiveDateTime::parse_from_str(value, "%Y-%m-%dT%H:%M:%S%.f")
        .map_err(|_| AnalysisError::new("INVALID_RESULT_TIME", "invalid range timestamp"))?;
    Ok((parsed.and_utc().timestamp_micros() - 28_800_000_000) as f64 / 1_000_000.0 * 1000.0)
}
