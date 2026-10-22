//! Version-bound, small HTTP read views over frozen analysis scalar tables.
//!
//! Every read is pinned to one snapshot id and never touches the historical
//! JSON columns (settings/ranges/payload) or source trade rows.

use rusqlite::{Connection, Row};
use serde_json::{json, Map, Value};

pub const SCHEMA_VERSION: i64 = 1;
const READ_VIEW_SCHEMA_VERSION: i64 = 1;
const MAX_RETRY_FAILURES: i64 = 3;

const SUMMARY_COLUMNS: &[&str] = &[
    "record_id",
    "execution_id",
    "time",
    "created_at",
    "is_success",
    "noop",
    "has_transactions",
    "duration_sec",
    "symbol_count",
    "position_count",
    "positions_present",
    "reason",
    "value",
    "cost",
    "loss_bp",
    "coverage",
    "covered",
    "count",
    "fee_covered",
    "amount_complete",
    "covered_value",
    "estimated",
];
// SUMMARY_COLUMNS 里的布尔列(两个来源表均为 Boolean 存储 0/1)。
const SUMMARY_BOOLEANS: &[&str] = &[
    "noop",
    "has_transactions",
    "positions_present",
    "amount_complete",
];
const DAILY_COLUMNS: &[&str] = &[
    "day",
    "value",
    "cost",
    "loss_bp",
    "coverage",
    "covered",
    "count",
    "amount_complete",
    "fee_covered",
    "covered_value",
    "estimated",
    "present",
];
const DAILY_BOOLEANS: &[&str] = &["amount_complete"];

fn cell(row: &Row<'_>, index: usize) -> rusqlite::Result<Value> {
    use rusqlite::types::ValueRef;
    Ok(match row.get_ref(index)? {
        ValueRef::Null => Value::Null,
        ValueRef::Integer(value) => json!(value),
        ValueRef::Real(value) => json!(value),
        ValueRef::Text(value) => json!(String::from_utf8_lossy(value)),
        ValueRef::Blob(_) => Value::Null,
    })
}

fn boolean(row: &Row<'_>, index: usize) -> rusqlite::Result<Value> {
    Ok(row
        .get_ref(index)?
        .as_i64()
        .map_or(Value::Null, |value| json!(value != 0)))
}

fn table(
    connection: &Connection,
    sql: &str,
    params: &[&dyn rusqlite::ToSql],
    columns: &[&str],
    booleans: &[&str],
) -> Result<Value, String> {
    let mut statement = connection
        .prepare(sql)
        .map_err(|error| format!("ANALYSIS_READ_ERROR: {error}"))?;
    let column_count = statement.column_count();
    let mut rows = statement
        .query(params)
        .map_err(|error| format!("ANALYSIS_READ_ERROR: {error}"))?;
    let mut values = Vec::new();
    while let Some(row) = rows
        .next()
        .map_err(|error| format!("ANALYSIS_READ_ERROR: {error}"))?
    {
        let mut cells = Vec::with_capacity(column_count);
        for index in 0..column_count {
            let name = columns.get(index).copied().unwrap_or_default();
            let read_error = |error: rusqlite::Error| format!("ANALYSIS_READ_ERROR: {error}");
            if booleans.contains(&name) {
                cells.push(boolean(row, index).map_err(read_error)?);
            } else {
                cells.push(cell(row, index).map_err(read_error)?);
            }
        }
        values.push(Value::Array(cells));
    }
    Ok(json!({"columns": columns, "rows": values}))
}

fn scalar_row(
    connection: &Connection,
    sql: &str,
    params: &[&dyn rusqlite::ToSql],
) -> Result<Option<Vec<Value>>, String> {
    let read_error = |error: rusqlite::Error| format!("ANALYSIS_READ_ERROR: {error}");
    let mut statement = connection.prepare(sql).map_err(read_error)?;
    let column_count = statement.column_count();
    let mut rows = statement.query(params).map_err(read_error)?;
    match rows.next().map_err(read_error)? {
        None => Ok(None),
        Some(row) => {
            let mut cells = Vec::with_capacity(column_count);
            for index in 0..column_count {
                cells.push(cell(row, index).map_err(read_error)?);
            }
            Ok(Some(cells))
        }
    }
}

struct Guard {
    source_version: i64,
    computed_at: Option<String>,
    data_until: Option<String>,
    backtest_weight_type: Option<String>,
    backtest_fee_rate: Option<f64>,
}

fn require_account(connection: &Connection, account_id: i64) -> Result<(), String> {
    let exists: bool = connection
        .query_row(
            "SELECT EXISTS(SELECT 1 FROM account WHERE id=?1)",
            [account_id],
            |row| row.get(0),
        )
        .map_err(|error| format!("ANALYSIS_READ_ERROR: {error}"))?;
    if !exists {
        return Err("ACCOUNT_NOT_FOUND: 账户不存在".into());
    }
    Ok(())
}

/// Check ownership and view schema without selecting either historical JSON column.
fn guard(connection: &Connection, account_id: i64, snapshot_id: &str) -> Result<Guard, String> {
    require_account(connection, account_id)?;
    let row = scalar_row(
        connection,
        "SELECT account_id,source_version,computed_at,data_until,backtest_weight_type,backtest_fee_rate,
                result_schema_version,cost_schema_version,read_view_schema_version
         FROM performance_snapshot WHERE id=?1",
        &[&snapshot_id],
    )?
    .ok_or("SNAPSHOT_EXPIRED: 绩效版本已过期，请重新获取发布状态")?;
    let account = row[0].as_i64();
    if account != Some(account_id) {
        return Err("SNAPSHOT_NOT_FOUND: 绩效版本不存在".into());
    }
    if row[6].as_i64() != Some(2)
        || row[7].as_i64() != Some(1)
        || row[8].as_i64() != Some(READ_VIEW_SCHEMA_VERSION)
    {
        return Err("ANALYSIS_READ_VIEW_UNAVAILABLE: 此绩效版本需要刷新后才能读取".into());
    }
    Ok(Guard {
        source_version: row[1].as_i64().unwrap_or(0),
        computed_at: row[2].as_str().map(str::to_owned),
        data_until: row[3].as_str().map(str::to_owned),
        backtest_weight_type: row[4].as_str().map(str::to_owned),
        backtest_fee_rate: row[5].as_f64(),
    })
}

pub fn status(
    connection: &Connection,
    account_id: i64,
    logic_version: &str,
    engine_version: &str,
) -> Result<Value, String> {
    require_account(connection, account_id)?;
    let row = scalar_row(
        connection,
        "SELECT a.source_version,a.published_version,a.requested,a.running_version,a.failures,a.retry_at,a.error,
                s.id,s.source_version,s.logic_version,s.engine_version,s.computed_at,s.data_until,
                s.read_view_schema_version
         FROM account_analysis a LEFT JOIN performance_snapshot s
           ON a.current_snapshot=s.id AND s.account_id=a.account_id
         WHERE a.account_id=?1",
        &[&account_id],
    )?;
    let Some(row) = row else {
        return Ok(json!({
            "schema_version": SCHEMA_VERSION, "status": "empty", "source_version": 0,
            "snapshot_id": None::<String>, "snapshot_version": None::<i64>,
            "logic_version": logic_version, "engine_version": engine_version,
            "computed_at": None::<String>, "data_until": None::<String>,
            "error": None::<String>, "retry_at": None::<f64>, "read_view_schema_version": 0,
        }));
    };
    let has_snapshot = row[7].as_str().is_some_and(|id| !id.is_empty());
    let compatible = has_snapshot
        && row[9].as_str() == Some(logic_version)
        && row[10].as_str() == Some(engine_version);
    let state = crate::snapshots::publication_status(
        row[7].as_str(),
        compatible,
        row[6].as_str(),
        row[2].as_i64().unwrap_or(0) != 0 || row[3].is_i64(),
        row[1].as_i64() == row[0].as_i64(),
    );
    let pick = |index: usize| {
        if compatible {
            row[index].clone()
        } else {
            Value::Null
        }
    };
    Ok(json!({
        "schema_version": SCHEMA_VERSION,
        "status": state,
        "source_version": row[0],
        "snapshot_id": pick(7),
        "snapshot_version": pick(8),
        "logic_version": if compatible { row[9].clone() } else { json!(logic_version) },
        "engine_version": if compatible { row[10].clone() } else { json!(engine_version) },
        "computed_at": pick(11),
        "data_until": pick(12),
        "error": row[6],
        "retry_at": if row[6].as_str().is_some() && row[4].as_i64().unwrap_or(i64::MAX) <= MAX_RETRY_FAILURES {
            row[5].clone()
        } else {
            Value::Null
        },
        "read_view_schema_version": if compatible { row[13].clone() } else { json!(0) },
    }))
}

fn range_header(
    connection: &Connection,
    snapshot_id: &str,
    range_key: &str,
) -> Result<Value, String> {
    let range = scalar_row(
        connection,
        "SELECT engine_version,backtest_included,baseline,end,record_count,observation_count,
                used_record_count,target_used_record_count,invalid_asset_count,sizing_fallback_count,
                calendar_status,calendar_id,calendar_label,event_count
         FROM performance_result_range WHERE snapshot_id=?1 AND range_key=?2",
        &[&snapshot_id, &range_key],
    )?
    .ok_or("ANALYSIS_READ_VIEW_INCOMPLETE: 已发布的绩效版本缺少区间结果")?;
    let mut skips = Map::new();
    let mut statement = connection
        .prepare(
            "SELECT kind,count,missing_target,missing_ticks,first_time,last_time
             FROM performance_result_skip WHERE snapshot_id=?1 AND range_key=?2",
        )
        .map_err(|error| format!("ANALYSIS_READ_ERROR: {error}"))?;
    let mut rows = statement
        .query(rusqlite::params![snapshot_id, range_key])
        .map_err(|error| format!("ANALYSIS_READ_ERROR: {error}"))?;
    let read_error = |error: rusqlite::Error| format!("ANALYSIS_READ_ERROR: {error}");
    while let Some(row) = rows.next().map_err(read_error)? {
        let kind = row
            .get_ref(0)
            .map_err(read_error)?
            .as_str()
            .unwrap_or_default()
            .to_owned();
        skips.insert(
            kind,
            json!({"count": cell(row, 1).map_err(read_error)?, "missing_target": cell(row, 2).map_err(read_error)?,
                   "missing_ticks": cell(row, 3).map_err(read_error)?, "first_time": cell(row, 4).map_err(read_error)?,
                   "last_time": cell(row, 5).map_err(read_error)?}),
        );
    }
    // backtest_included 是 Boolean 存储。
    let backtest_included = range[1]
        .as_i64()
        .map_or(Value::Null, |value| json!(value != 0));
    Ok(json!({
        "range": range_key,
        "engine_version": range[0],
        "backtest_included": backtest_included,
        "baseline": range[2], "end": range[3],
        "record_count": range[4], "observation_count": range[5],
        "used_record_count": range[6], "target_used_record_count": range[7],
        "invalid_asset_count": range[8], "sizing_fallback_count": range[9],
        "event_count": range[13],
        "skips": skips.get("portfolio").cloned().unwrap_or(Value::Null),
        "target_skips": skips.get("target").cloned().unwrap_or(Value::Null),
        "calendar": {"status": range[10], "calendar_id": range[11], "label": range[12]},
    }))
}

/// Summary rows are frozen per snapshot; the range only filters by time bounds.
fn summary_sql() -> String {
    let columns = SUMMARY_COLUMNS
        .iter()
        .map(|name| {
            if matches!(
                *name,
                "record_id"
                    | "time"
                    | "value"
                    | "cost"
                    | "loss_bp"
                    | "coverage"
                    | "covered"
                    | "count"
                    | "fee_covered"
                    | "amount_complete"
                    | "covered_value"
                    | "estimated"
                    | "symbol_count"
                    | "has_transactions"
            ) {
                format!("s.{name}")
            } else {
                format!("e.{name}")
            }
        })
        .collect::<Vec<_>>()
        .join(",");
    format!(
        "SELECT {columns} FROM performance_execution_summary s
         JOIN performance_cost_execution e
           ON s.snapshot_id=e.snapshot_id AND s.record_id=e.record_id"
    )
}

/// Range-bounded filters; a missing bound pair yields no rows, matching sa.false().
fn summary_filters(
    connection: &Connection,
    snapshot_id: &str,
    range_key: &str,
    start: Option<f64>,
    end: Option<f64>,
    sql: &mut String,
    params: &mut Vec<Box<dyn rusqlite::ToSql>>,
) -> Result<(), String> {
    let bounds = scalar_row(
        connection,
        "SELECT baseline,end FROM performance_cost_range WHERE snapshot_id=?1 AND range_key=?2",
        &[&snapshot_id, &range_key],
    )?
    .ok_or("ANALYSIS_READ_VIEW_INCOMPLETE: 已发布的绩效版本缺少区间边界")?;
    params.push(Box::new(snapshot_id.to_owned()));
    sql.push_str(" WHERE s.snapshot_id=?");
    if let (Some(lower), Some(upper)) = (bounds[0].as_f64(), bounds[1].as_f64()) {
        sql.push_str(" AND s.time>=? AND s.time<=?");
        params.push(Box::new(lower));
        params.push(Box::new(upper));
    } else {
        // 无边界区间没有任何行;恒假条件等价于 sa.false()。
        sql.push_str(" AND 0");
    }
    if let Some(start) = start {
        sql.push_str(" AND s.time>=? AND s.time<=?");
        params.push(Box::new(start));
        params.push(Box::new(end.unwrap_or(f64::MAX)));
    }
    Ok(())
}

fn param_refs(params: &[Box<dyn rusqlite::ToSql>]) -> Vec<&dyn rusqlite::ToSql> {
    params.iter().map(|value| value.as_ref()).collect()
}

fn result_tables(
    connection: &Connection,
    snapshot_id: &str,
    range_key: &str,
) -> Result<Map<String, Value>, String> {
    let mut tables = Map::new();
    let scope = rusqlite::params![snapshot_id, range_key];
    tables.insert(
        "points".into(),
        table(
            connection,
            "SELECT date,observed_at,record_id,execution_id,account_return,account_equity,portfolio_return,
                    account_daily_return,portfolio_daily_return,target_portfolio_return,
                    target_portfolio_daily_return,difference,sizing_fallback
             FROM performance_result_point WHERE snapshot_id=?1 AND range_key=?2 ORDER BY idx",
            scope,
            &["date", "observed_at", "record_id", "execution_id", "account_return", "account_equity",
              "portfolio_return", "account_daily_return", "portfolio_daily_return", "target_portfolio_return",
              "target_portfolio_daily_return", "difference", "sizing_fallback"],
            &["sizing_fallback"],
        )?,
    );
    tables.insert(
        "bindings".into(),
        table(
            connection,
            "SELECT time,portfolio_id FROM performance_result_binding
             WHERE snapshot_id=?1 AND range_key=?2 ORDER BY idx",
            scope,
            &["time", "portfolio_id"],
            &[],
        )?,
    );
    tables.insert(
        "fallbacks".into(),
        table(
            connection,
            "SELECT start,end,count FROM performance_result_fallback
             WHERE snapshot_id=?1 AND range_key=?2 ORDER BY idx",
            scope,
            &["start", "end", "count"],
            &[],
        )?,
    );
    tables.insert(
        "calendar_ranges".into(),
        table(
            connection,
            "SELECT kind,start,end FROM performance_result_calendar_range
             WHERE snapshot_id=?1 AND range_key=?2 ORDER BY kind,idx",
            scope,
            &["kind", "start", "end"],
            &[],
        )?,
    );
    tables.insert(
        "daily_costs".into(),
        table(
            connection,
            &format!(
                "SELECT {} FROM performance_result_daily_cost
                 WHERE snapshot_id=?1 AND range_key=?2 ORDER BY day",
                DAILY_COLUMNS.join(",")
            ),
            scope,
            DAILY_COLUMNS,
            DAILY_BOOLEANS,
        )?,
    );
    tables.insert(
        "daily_fees".into(),
        table(
            connection,
            "SELECT day,currency,value FROM performance_result_daily_fee
             WHERE snapshot_id=?1 AND range_key=?2 ORDER BY day,currency",
            scope,
            &["day", "currency", "value"],
            &[],
        )?,
    );
    tables.insert(
        "events".into(),
        table(
            connection,
            "SELECT time,tag,text,execution_id FROM performance_result_event
             WHERE snapshot_id=?1 AND range_key=?2 ORDER BY idx LIMIT 100",
            scope,
            &["time", "tag", "text", "execution_id"],
            &[],
        )?,
    );
    Ok(tables)
}

pub fn chart(
    connection: &Connection,
    account_id: i64,
    snapshot_id: &str,
    range_key: &str,
) -> Result<Value, String> {
    let snapshot = guard(connection, account_id, snapshot_id)?;
    let header = range_header(connection, snapshot_id, range_key)?;
    let mut tables = result_tables(connection, snapshot_id, range_key)?;
    let mut sql = summary_sql();
    let mut params: Vec<Box<dyn rusqlite::ToSql>> = Vec::new();
    summary_filters(
        connection,
        snapshot_id,
        range_key,
        None,
        None,
        &mut sql,
        &mut params,
    )?;
    sql.push_str(" ORDER BY s.time,s.record_id");
    let index = table(
        connection,
        &sql,
        &param_refs(&params),
        SUMMARY_COLUMNS,
        SUMMARY_BOOLEANS,
    )?;
    let fee_sql = format!("WITH selected AS ({sql})
        SELECT record_id,currency,value,count FROM performance_execution_fee
        WHERE snapshot_id=?1 AND record_id IN (SELECT record_id FROM selected) ORDER BY record_id,currency");
    let fees = table(
        connection,
        &fee_sql,
        &param_refs(&params),
        &["record_id", "currency", "value", "count"],
        &[],
    )?;
    tables.insert("execution_index".into(), index);
    tables.insert("execution_fees".into(), fees);
    Ok(json!({
        "schema_version": SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "account_id": account_id,
        "range": range_key,
        "snapshot_version": snapshot.source_version,
        "computed_at": snapshot.computed_at,
        "data_until": snapshot.data_until,
        "settings": {
            "backtest_weight_type": snapshot.backtest_weight_type,
            "backtest_fee_rate": snapshot.backtest_fee_rate,
        },
        "header": header,
        "tables": tables,
    }))
}

#[allow(clippy::too_many_arguments)]
pub fn execution_summary(
    connection: &Connection,
    account_id: i64,
    snapshot_id: &str,
    range_key: &str,
    limit: i64,
    offset: i64,
    start: Option<f64>,
    end: Option<f64>,
) -> Result<Value, String> {
    guard(connection, account_id, snapshot_id)?;
    let mut sql = summary_sql();
    let mut params: Vec<Box<dyn rusqlite::ToSql>> = Vec::new();
    summary_filters(
        connection,
        snapshot_id,
        range_key,
        start,
        end,
        &mut sql,
        &mut params,
    )?;
    sql.push_str(" ORDER BY s.time,s.record_id LIMIT ? OFFSET ?");
    params.push(Box::new(limit + 1));
    params.push(Box::new(offset));
    let mut page = table(
        connection,
        &sql,
        &param_refs(&params),
        SUMMARY_COLUMNS,
        SUMMARY_BOOLEANS,
    )?;
    let rows = page["rows"].as_array_mut().unwrap();
    let has_more = rows.len() as i64 > limit;
    rows.truncate(limit as usize);
    let limit_index = params.len() - 2;
    params[limit_index] = Box::new(limit);
    let fee_sql = format!(
        "WITH selected AS ({sql})
        SELECT record_id,currency,value,count FROM performance_execution_fee
        WHERE snapshot_id=?1 AND record_id IN (SELECT record_id FROM selected)
        ORDER BY record_id,currency"
    );
    let fees = table(
        connection,
        &fee_sql,
        &param_refs(&params),
        &["record_id", "currency", "value", "count"],
        &[],
    )?;
    Ok(json!({
        "schema_version": SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "range": range_key,
        "table": page,
        "fees": fees,
        "next_offset": if has_more { json!(offset + limit) } else { Value::Null },
    }))
}

pub fn execution_detail(
    connection: &Connection,
    account_id: i64,
    snapshot_id: &str,
    record_id: i64,
    limit: i64,
    offset: i64,
) -> Result<Value, String> {
    guard(connection, account_id, snapshot_id)?;
    let header_columns = [
        "record_id",
        "time",
        "success",
        "noop",
        "key",
        "execution_id",
        "created_at",
        "is_success",
        "duration_sec",
        "trade_count",
        "symbol_count",
        "position_count",
        "positions_present",
        "reason",
        "raw_present",
        "has_symbol_results",
        "raw_status",
        "raw_task_status",
        "raw_error",
        "raw_outcome",
        "raw_outcome_reason",
        "raw_reason_code",
        "raw_msg",
        "raw_execution_kind",
    ];
    let header = scalar_row(
        connection,
        &format!(
            "SELECT {} FROM performance_cost_execution WHERE snapshot_id=?1 AND record_id=?2",
            header_columns.join(",")
        ),
        &[&snapshot_id, &record_id],
    )?
    .ok_or("EXECUTION_NOT_FOUND: 此绩效版本中没有该执行记录")?;
    let summary_columns = [
        "value",
        "cost",
        "loss_bp",
        "coverage",
        "covered",
        "count",
        "fee_covered",
        "amount_complete",
        "covered_value",
        "estimated",
        "symbol_count",
        "has_transactions",
    ];
    let summary = scalar_row(
        connection,
        &format!(
            "SELECT {} FROM performance_execution_summary WHERE snapshot_id=?1 AND record_id=?2",
            summary_columns.join(",")
        ),
        &[&snapshot_id, &record_id],
    )?
    .ok_or("ANALYSIS_READ_VIEW_INCOMPLETE: 已发布的绩效版本缺少执行摘要")?;
    let mut summary_map = Map::new();
    for (name, value) in summary_columns.iter().zip(summary) {
        summary_map.insert(
            (*name).to_owned(),
            if SUMMARY_BOOLEANS.contains(name) {
                value.as_i64().map_or(Value::Null, |flag| json!(flag != 0))
            } else {
                value
            },
        );
    }
    let mut statement = connection.prepare(
        "SELECT currency,value FROM performance_execution_fee WHERE snapshot_id=?1 AND record_id=?2 ORDER BY currency"
    ).map_err(|error| format!("ANALYSIS_READ_ERROR: {error}"))?;
    let fees = statement
        .query_map(rusqlite::params![snapshot_id, record_id], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, f64>(1)?))
        })
        .map_err(|error| format!("ANALYSIS_READ_ERROR: {error}"))?
        .map(|row| row.map(|(currency, value)| (currency, json!(value))))
        .collect::<rusqlite::Result<Map<String, Value>>>()
        .map_err(|error| format!("ANALYSIS_READ_ERROR: {error}"))?;
    summary_map.insert("fees".into(), Value::Object(fees));
    let header_booleans = &["success", "noop", "positions_present", "has_symbol_results"];
    let mut header_map = Map::new();
    for (name, value) in header_columns.iter().zip(header) {
        header_map.insert(
            (*name).to_owned(),
            if header_booleans.contains(name) {
                value.as_i64().map_or(Value::Null, |flag| json!(flag != 0))
            } else {
                value
            },
        );
    }
    let mut tables = Map::new();
    let mut next_offsets = Map::new();
    let mut transactions_next = Value::Null;
    for (name, source, columns, booleans, ordering) in [
        ("positions", "performance_cost_position", "idx,symbol,quantity,direction", &[][..], "idx"),
        ("attempts", "performance_cost_attempt", "idx,symbol,planned,filled,reason", &[][..], "idx"),
        ("symbols", "performance_cost_execution_symbol",
         "symbol,present,status,error,outcome,outcome_reason,reason_code", &[][..], "symbol"),
        ("transactions", "performance_cost_transaction",
         "idx,symbol,side,quantity,price,reference,reference_source,time,end_time,time_estimated,before,target,value,cost,loss_bp,coverage,covered,count,amount_complete,fee_covered",
         &["time_estimated", "amount_complete"][..], "idx"),
    ] {
        let sql = format!(
            "SELECT {columns} FROM {source}
             WHERE snapshot_id=? AND record_id=? ORDER BY {ordering} LIMIT ? OFFSET ?"
        );
        let columns: Vec<&str> = columns.split(',').collect();
        // 多取一行判断是否有下一页。
        let mut page = table(
            connection,
            &sql,
            &[&snapshot_id, &record_id, &(limit + 1), &offset],
            &columns,
            booleans,
        )?;
        let rows = page["rows"].as_array_mut().unwrap();
        let has_more = rows.len() as i64 > limit;
        rows.truncate(limit as usize);
        let next = if has_more { json!(offset + limit) } else { Value::Null };
        if name == "transactions" {
            transactions_next = next;
        } else {
            next_offsets.insert(name.to_owned(), next);
        }
        tables.insert(name.to_owned(), page);
    }
    tables.insert(
        "transaction_fees".into(),
        table(
            connection,
            "SELECT transaction_idx,currency,value FROM performance_cost_transaction_fee
             WHERE snapshot_id=?1 AND record_id=?2 AND transaction_idx IN (
                 SELECT idx FROM performance_cost_transaction
                 WHERE snapshot_id=?1 AND record_id=?2 ORDER BY idx LIMIT ?3 OFFSET ?4
             ) ORDER BY transaction_idx,currency",
            &[&snapshot_id, &record_id, &limit, &offset],
            &["transaction_idx", "currency", "value"],
            &[],
        )?,
    );
    let total: i64 = connection
        .query_row(
            "SELECT COUNT(*) FROM performance_cost_transaction WHERE snapshot_id=?1 AND record_id=?2",
            rusqlite::params![snapshot_id, record_id],
            |row| row.get(0),
        )
        .map_err(|error| format!("ANALYSIS_READ_ERROR: {error}"))?;
    let next_offset = transactions_next;
    Ok(json!({
        "schema_version": SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "record_id": record_id,
        "header": header_map,
        "summary": summary_map,
        "tables": tables,
        "total": total,
        "next_offset": next_offset,
        "next_offsets": next_offsets,
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use rusqlite::Connection;

    fn database() -> Connection {
        let connection = Connection::open_in_memory().unwrap();
        connection
            .execute_batch(
                "CREATE TABLE account (id INTEGER PRIMARY KEY);
                 INSERT INTO account VALUES (2),(3),(7);
                 CREATE TABLE account_analysis (
                     account_id INTEGER PRIMARY KEY, source_version INTEGER NOT NULL DEFAULT 1,
                     published_version INTEGER NOT NULL DEFAULT 0, requested INTEGER NOT NULL DEFAULT 0,
                     current_snapshot TEXT, running_version INTEGER, failures INTEGER NOT NULL DEFAULT 0,
                     retry_at REAL NOT NULL DEFAULT 0, error TEXT, logic_version TEXT, engine_version TEXT);
                 CREATE TABLE performance_snapshot (
                     id TEXT PRIMARY KEY, account_id INTEGER, source_version INTEGER,
                     logic_version TEXT, engine_version TEXT, computed_at TEXT, data_until TEXT,
                     backtest_weight_type TEXT, backtest_fee_rate REAL,
                     cost_schema_version INTEGER NOT NULL DEFAULT 1,
                     result_schema_version INTEGER NOT NULL DEFAULT 2,
                     read_view_schema_version INTEGER NOT NULL DEFAULT 1);
                 CREATE TABLE performance_result_range (
                     snapshot_id TEXT, range_key TEXT, engine_version TEXT, backtest_included INTEGER,
                     baseline TEXT, end TEXT, record_count INTEGER, observation_count INTEGER,
                     used_record_count INTEGER, target_used_record_count INTEGER, invalid_asset_count INTEGER,
                     sizing_fallback_count INTEGER, calendar_status TEXT, calendar_id TEXT,
                     calendar_label TEXT, event_count INTEGER, PRIMARY KEY(snapshot_id, range_key));
                 CREATE TABLE performance_result_skip (
                     snapshot_id TEXT, range_key TEXT, kind TEXT, count INTEGER, missing_target INTEGER,
                     missing_ticks INTEGER, first_time TEXT, last_time TEXT,
                     PRIMARY KEY(snapshot_id, range_key, kind));
                 CREATE TABLE performance_result_point (
                     snapshot_id TEXT, range_key TEXT, idx INTEGER, date TEXT, observed_at TEXT,
                     record_id INTEGER, execution_id TEXT, account_return REAL, account_equity REAL,
                     portfolio_return REAL, account_daily_return REAL, portfolio_daily_return REAL,
                     target_portfolio_return REAL, target_portfolio_daily_return REAL, difference REAL,
                     sizing_fallback INTEGER, PRIMARY KEY(snapshot_id, range_key, idx));
                 CREATE TABLE performance_result_binding (
                     snapshot_id TEXT, range_key TEXT, idx INTEGER, time TEXT, portfolio_id INTEGER,
                     PRIMARY KEY(snapshot_id, range_key, idx));
                 CREATE TABLE performance_result_fallback (
                     snapshot_id TEXT, range_key TEXT, idx INTEGER, start TEXT, end TEXT, count INTEGER,
                     PRIMARY KEY(snapshot_id, range_key, idx));
                 CREATE TABLE performance_result_calendar_range (
                     snapshot_id TEXT, range_key TEXT, kind TEXT, idx INTEGER, start TEXT, end TEXT,
                     PRIMARY KEY(snapshot_id, range_key, kind, idx));
                 CREATE TABLE performance_result_daily_cost (
                     snapshot_id TEXT, range_key TEXT, day TEXT, value REAL, cost REAL, loss_bp REAL,
                     coverage REAL, covered_value REAL, covered INTEGER, count INTEGER, fee_covered INTEGER,
                     estimated INTEGER, amount_complete INTEGER, present INTEGER,
                     PRIMARY KEY(snapshot_id, range_key, day));
                 CREATE TABLE performance_result_daily_fee (
                     snapshot_id TEXT, range_key TEXT, day TEXT, currency TEXT, value REAL,
                     PRIMARY KEY(snapshot_id, range_key, day, currency));
                 CREATE TABLE performance_result_event (
                     snapshot_id TEXT, range_key TEXT, idx INTEGER, time TEXT, tag TEXT, text TEXT,
                     execution_id TEXT, PRIMARY KEY(snapshot_id, range_key, idx));
                 CREATE TABLE performance_execution_summary (
                     snapshot_id TEXT, record_id INTEGER, time REAL, value REAL, cost REAL, loss_bp REAL,
                     coverage REAL, covered INTEGER, count INTEGER, fee_covered INTEGER,
                     amount_complete INTEGER, covered_value REAL, estimated INTEGER, symbol_count INTEGER,
                     has_transactions INTEGER, PRIMARY KEY(snapshot_id, record_id));
                 CREATE TABLE performance_execution_fee (
                     snapshot_id TEXT, record_id INTEGER, currency TEXT, value REAL, count INTEGER,
                     PRIMARY KEY(snapshot_id, record_id, currency));
                 CREATE TABLE performance_cost_execution (
                     snapshot_id TEXT, record_id INTEGER, time REAL, success INTEGER, noop INTEGER,
                     key TEXT, execution_id TEXT, created_at TEXT, is_success INTEGER, duration_sec REAL,
                     trade_count INTEGER, symbol_count INTEGER, position_count INTEGER,
                     positions_present INTEGER, reason TEXT, raw_present INTEGER, has_symbol_results INTEGER,
                     raw_status TEXT, raw_task_status TEXT, raw_error TEXT, raw_outcome TEXT,
                     raw_outcome_reason TEXT, raw_reason_code TEXT, raw_msg TEXT, raw_execution_kind TEXT,
                     payload TEXT, PRIMARY KEY(snapshot_id, record_id));
                 CREATE TABLE performance_cost_range (
                     snapshot_id TEXT, range_key TEXT, baseline REAL, end REAL,
                     PRIMARY KEY(snapshot_id, range_key));
                 CREATE TABLE performance_cost_position (
                     snapshot_id TEXT, record_id INTEGER, idx INTEGER, symbol TEXT, quantity REAL,
                     direction TEXT, PRIMARY KEY(snapshot_id, record_id, idx));
                 CREATE TABLE performance_cost_attempt (
                     snapshot_id TEXT, record_id INTEGER, idx INTEGER, symbol TEXT, planned REAL,
                     filled REAL, reason TEXT, PRIMARY KEY(snapshot_id, record_id, idx));
                 CREATE TABLE performance_cost_execution_symbol (
                     snapshot_id TEXT, record_id INTEGER, symbol TEXT, present INTEGER, status TEXT,
                     error TEXT, outcome TEXT, outcome_reason TEXT, reason_code TEXT,
                     PRIMARY KEY(snapshot_id, record_id, symbol));
                 CREATE TABLE performance_cost_transaction (
                     snapshot_id TEXT, record_id INTEGER, idx INTEGER, symbol TEXT, side TEXT,
                     quantity REAL, price REAL, reference REAL, reference_source TEXT, time REAL,
                     end_time REAL, time_estimated INTEGER, before REAL, target REAL, value REAL,
                     cost REAL, loss_bp REAL, coverage REAL, covered INTEGER, count INTEGER,
                     amount_complete INTEGER, fee_covered INTEGER, PRIMARY KEY(snapshot_id, record_id, idx));
                 CREATE TABLE performance_cost_transaction_fee (
                     snapshot_id TEXT, record_id INTEGER, transaction_idx INTEGER, currency TEXT, value REAL,
                     PRIMARY KEY(snapshot_id, record_id, transaction_idx, currency));",
            )
            .unwrap();
        connection
            .execute_batch(
                "INSERT INTO account_analysis VALUES (2,1,1,0,'s1',NULL,0,0,NULL,'18','equity-ledger-1');
                 INSERT INTO performance_snapshot VALUES ('s1',2,1,'18','equity-ledger-1','2026-01-03T00:00:00','2026-01-02T09:00:00','cs',0.0,1,2,1);
                 INSERT INTO performance_snapshot VALUES ('foreign',3,1,'18','equity-ledger-1',NULL,NULL,'cs',0.0,1,2,1);
                 INSERT INTO performance_snapshot VALUES ('legacy',2,1,'18','equity-ledger-1',NULL,NULL,'cs',0.0,1,2,0);
                 INSERT INTO performance_result_range VALUES ('s1','all','equity-ledger-1',1,'2026-01-01T09:00:00','2026-01-02T09:00:00',1,1,1,1,0,0,'available','cal','日历',1);
                 INSERT INTO performance_result_point VALUES ('s1','all',0,'2026-01-01','2026-01-01T09:00:00',NULL,NULL,NULL,100.0,NULL,NULL,NULL,NULL,NULL,NULL,0);
                 INSERT INTO performance_result_point VALUES ('s1','all',1,'2026-01-02','2026-01-02T09:00:00',1,'e-1',0.1,110.0,NULL,0.1,NULL,NULL,NULL,NULL,1);
                 INSERT INTO performance_cost_range VALUES ('s1','all',1000.0,2000.0);
                 INSERT INTO performance_execution_summary VALUES ('s1',1,1500.0,1234.5,-0.5,NULL,1.0,1,1,2,1,1234.5,0,1,1);
                 INSERT INTO performance_execution_summary VALUES ('s1',2,1800.0,NULL,NULL,NULL,NULL,0,0,0,1,0,0,NULL,0);
                 INSERT INTO performance_execution_fee VALUES ('s1',1,'USD',0.0,1),('s1',1,'BTC',-0.000001,1);
                 INSERT INTO performance_cost_execution VALUES ('s1',1,1500.0,1,0,'k-1','e-1','2026-01-01T09:30:00',1,0.25,1,1,1,1,NULL,0,1,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,'{}');
                 INSERT INTO performance_cost_execution VALUES ('s1',2,1800.0,1,1,'k-2','e-2','2026-01-02T09:30:00',1,0.1,0,0,0,0,'noop',0,1,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,'{}');
                 INSERT INTO performance_cost_transaction VALUES ('s1',1,0,'A','buy',0.5,2469.0,NULL,NULL,1500.25,NULL,0,NULL,NULL,1234.5,-0.5,NULL,1.0,1,1,1,2);
                 INSERT INTO performance_cost_transaction_fee VALUES ('s1',1,0,'USD',-0.25);",
            )
            .unwrap();
        connection
    }

    #[test]
    fn chart_retains_one_snapshot_during_concurrent_deletion() {
        let path = std::env::temp_dir().join(format!(
            "axile-views-retention-{}.sqlite",
            std::process::id()
        ));
        let source = database();
        source
            .execute("VACUUM INTO ?1", [path.to_str().unwrap()])
            .unwrap();
        let reader = Connection::open(&path).unwrap();
        reader
            .execute_batch("PRAGMA journal_mode=WAL; BEGIN DEFERRED;")
            .unwrap();
        let expected = chart(&reader, 2, "s1", "all").unwrap();
        let writer = Connection::open(&path).unwrap();
        writer.execute_batch("DELETE FROM performance_snapshot WHERE id='s1'; DELETE FROM performance_result_point WHERE snapshot_id='s1';").unwrap();
        assert_eq!(chart(&reader, 2, "s1", "all").unwrap(), expected);
        reader.execute_batch("ROLLBACK;").unwrap();
        assert!(chart(&reader, 2, "s1", "all")
            .unwrap_err()
            .starts_with("SNAPSHOT_EXPIRED"));
        drop(writer);
        drop(reader);
        std::fs::remove_file(path).unwrap();
    }

    #[test]
    fn status_reports_ready_and_view_versions() {
        let connection = database();
        let value = status(&connection, 2, "18", "equity-ledger-1").unwrap();
        assert_eq!(value["status"], "ready");
        assert_eq!(value["snapshot_id"], "s1");
        assert_eq!(value["read_view_schema_version"], 1);
        let empty = status(&connection, 7, "18", "equity-ledger-1").unwrap();
        assert_eq!(empty["status"], "empty");
        assert_eq!(empty["source_version"], 0);
    }

    #[test]
    fn guard_rejects_expired_foreign_and_legacy_views() {
        let connection = database();
        for (snapshot_id, code) in [
            ("missing", "SNAPSHOT_EXPIRED"),
            ("foreign", "SNAPSHOT_NOT_FOUND"),
            ("legacy", "ANALYSIS_READ_VIEW_UNAVAILABLE"),
        ] {
            let error = chart(&connection, 2, snapshot_id, "all").unwrap_err();
            assert!(error.starts_with(code), "{snapshot_id}: {error}");
        }
    }

    #[test]
    fn chart_returns_header_tables_and_frozen_index() {
        let connection = database();
        let value = chart(&connection, 2, "s1", "all").unwrap();
        assert_eq!(value["schema_version"], 1);
        assert_eq!(value["settings"]["backtest_weight_type"], "cs");
        assert_eq!(value["header"]["calendar"]["status"], "available");
        let points = &value["tables"]["points"];
        assert_eq!(points["columns"][0], "date");
        assert_eq!(points["rows"].as_array().unwrap().len(), 2);
        assert_eq!(points["rows"][1][12], true); // sizing_fallback 转布尔
        let index = &value["tables"]["execution_index"];
        assert_eq!(index["rows"].as_array().unwrap().len(), 2);
        let first = &index["rows"][0];
        assert_eq!(first[0], 1);
        assert_eq!(first[1], "e-1");
        assert_eq!(first[4], 1); // is_success 保持整数
        assert_eq!(first[10], true); // positions_present 转布尔
        assert_eq!(first[6], true); // has_transactions 转布尔
        let fees = &value["tables"]["execution_fees"]["rows"];
        assert_eq!(fees.as_array().unwrap().len(), 2);
    }

    #[test]
    fn summary_pages_and_viewport_filter_before_pagination() {
        let connection = database();
        let page = execution_summary(&connection, 2, "s1", "all", 1, 0, None, None).unwrap();
        assert_eq!(page["table"]["rows"].as_array().unwrap().len(), 1);
        assert_eq!(page["next_offset"], 1);
        let scoped = execution_summary(
            &connection,
            2,
            "s1",
            "all",
            200,
            0,
            Some(1000.0),
            Some(1600.0),
        )
        .unwrap();
        let rows = scoped["table"]["rows"].as_array().unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0][0], 1);
        assert_eq!(scoped["next_offset"], serde_json::Value::Null);
        let beyond = execution_summary(&connection, 2, "s1", "all", 200, 2, None, None).unwrap();
        assert_eq!(beyond["table"]["rows"].as_array().unwrap().len(), 0);
        assert_eq!(beyond["next_offset"], serde_json::Value::Null);
    }

    #[test]
    fn detail_reads_typed_evidence_with_fees_and_pagination() {
        let connection = database();
        let value = execution_detail(&connection, 2, "s1", 1, 200, 0).unwrap();
        assert_eq!(value["header"]["execution_id"], "e-1");
        assert_eq!(value["header"]["success"], true);
        assert_eq!(value["summary"]["fees"]["BTC"], -0.000001);
        assert_eq!(value["summary"]["has_transactions"], true);
        let transactions = &value["tables"]["transactions"];
        assert_eq!(transactions["rows"][0][7], 1500.25); // time
        assert_eq!(transactions["rows"][0][9], false); // time_estimated
        assert_eq!(value["tables"]["transaction_fees"]["rows"][0][2], -0.25);
        assert_eq!(value["total"], 1);
        let missing = execution_detail(&connection, 2, "s1", 9, 200, 0).unwrap_err();
        assert!(missing.starts_with("EXECUTION_NOT_FOUND"));
    }
}
