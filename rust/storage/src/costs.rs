//! 成本快照分页；只读取当前结构化结果。

use std::collections::HashMap;

use base64::{engine::general_purpose::URL_SAFE, Engine as _};
use chrono::NaiveDate;
use rusqlite::{params_from_iter, types::Value as SqlValue, Connection, Row};
use serde_json::{json, Map, Value};

use crate::cost_summary::{sql_number, Summary, AGGREGATES};

const EXECUTION_AGGREGATES: &str = "SELECT record_id,COUNT(*) AS trade_rows,COUNT(value) AS valued, \
    SUM(value) AS total_value,SUM(cost) AS total_cost, \
    COUNT(CASE WHEN cost IS NOT NULL AND value IS NOT NULL THEN 1 END) AS covered, \
    SUM(CASE WHEN cost IS NOT NULL AND value IS NOT NULL THEN value ELSE 0 END) AS covered_value, \
    SUM(CASE WHEN cost IS NOT NULL AND value IS NOT NULL THEN loss_bp*value ELSE 0 END) AS weighted_loss, \
    SUM(CAST(estimated AS INTEGER)) AS estimated_count, \
    COUNT(DISTINCT CASE WHEN quantity IS NOT NULL THEN symbol END) AS symbols \
    FROM filtered_trades GROUP BY record_id";
const RAW_KEYS: [&str; 8] = [
    "status",
    "task_status",
    "error",
    "outcome",
    "outcome_reason",
    "reason_code",
    "msg",
    "execution_kind",
];
const SYMBOL_KEYS: [&str; 5] = [
    "status",
    "error",
    "outcome",
    "outcome_reason",
    "reason_code",
];

fn failure(code: &str, message: &str) -> String {
    format!("{code}:{message}")
}
fn sql_error(error: rusqlite::Error) -> String {
    failure("ANALYSIS_READ_ERROR", &error.to_string())
}
fn string<'a>(request: &'a Value, key: &str, default: &'a str) -> &'a str {
    request.get(key).and_then(Value::as_str).unwrap_or(default)
}
fn optional_string<'a>(request: &'a Value, key: &str) -> Option<&'a str> {
    request.get(key).and_then(Value::as_str)
}
fn bind(args: &mut Vec<SqlValue>, value: impl Into<SqlValue>) -> String {
    args.push(value.into());
    format!("?{}", args.len())
}

fn quantity_total(row: &Row<'_>, index: usize) -> rusqlite::Result<Value> {
    let value = sql_number(row, index)?;
    Ok(if value.is_null() { json!(0) } else { value })
}

struct Scope {
    cte: String,
    args: Vec<SqlValue>,
}
impl Scope {
    fn new(conn: &Connection, request: &Value) -> Result<Self, String> {
        let snapshot = string(request, "snapshot_id", "");
        let range = string(request, "range", "all");
        let bounds = crate::performance_ranges::range_bounds(conn, snapshot, range)?
            .ok_or("ANALYSIS_READ_VIEW_INCOMPLETE: missing range bounds")?;
        let mut args = vec![
            SqlValue::Text(snapshot.to_owned()),
            SqlValue::Real(bounds.0.unwrap_or(0.0)),
            SqlValue::Real(bounds.1.unwrap_or(-1.0)),
        ];
        let mut execution_filters = vec![
            "snapshot_id=?1".to_owned(),
            "time>=?2".to_owned(),
            "time<=?3".to_owned(),
        ];
        if let Some(record) = request.get("record_id").filter(|value| !value.is_null()) {
            let id = record
                .as_i64()
                .ok_or_else(|| failure("INVALID_COST_QUERY", "record_id must be an integer"))?;
            execution_filters.push(format!("record_id={}", bind(&mut args, id)));
        }
        let mut trade_filters = vec![
            "snapshot_id=?1".to_owned(),
            "record_id IN (SELECT record_id FROM base_executions)".to_owned(),
        ];
        let mut time_filter = None;
        if let Some(day) = optional_string(request, "day").filter(|day| !day.is_empty()) {
            let day_param = bind(&mut args, day.to_owned());
            trade_filters.push(format!("day={day_param}"));
            let midnight = format!("(CAST(strftime('%s', {day_param}) AS INTEGER)*1000-28800000)");
            time_filter = Some(format!("(time>={midnight} AND time<{midnight}+86400000)"));
        } else if let Some(start) = request.get("start").and_then(Value::as_f64) {
            let end = request.get("end").and_then(Value::as_f64).ok_or_else(|| {
                failure(
                    "INVALID_COST_QUERY",
                    "start and end must define a nonempty interval",
                )
            })?;
            if !start.is_finite() || !end.is_finite() || start >= end {
                return Err(failure(
                    "INVALID_COST_QUERY",
                    "start and end must define a nonempty interval",
                ));
            }
            let left = bind(&mut args, start);
            let right = bind(&mut args, end);
            let interval = format!("(time>{left} AND time<={right})");
            trade_filters.push(interval.clone());
            time_filter = Some(interval);
        } else if request.get("end").is_some_and(|value| !value.is_null()) {
            return Err(failure(
                "INVALID_COST_QUERY",
                "start and end must define a nonempty interval",
            ));
        }
        let mut symbol_scope = false;
        if let Some(symbol) = optional_string(request, "symbol") {
            symbol_scope = true;
            trade_filters.push(format!("symbol={}", bind(&mut args, symbol.to_owned())));
        }
        if let Some(side) = optional_string(request, "side") {
            symbol_scope = true;
            trade_filters.push(format!("side={}", bind(&mut args, side.to_owned())));
        }
        if let Some(search) =
            optional_string(request, "symbol_search").filter(|value| !value.is_empty())
        {
            symbol_scope = true;
            let literal = search
                .replace('/', "//")
                .replace('%', "/%")
                .replace('_', "/_");
            trade_filters.push(format!(
                "lower(symbol) LIKE '%' || lower({}) || '%' ESCAPE '/'",
                bind(&mut args, literal)
            ));
        }
        let execution_selection = match (symbol_scope, time_filter) {
            (true, _) => " WHERE record_id IN (SELECT record_id FROM filtered_trades)".to_owned(),
            (false, Some(time)) => {
                format!(" WHERE record_id IN (SELECT record_id FROM filtered_trades) OR {time}")
            }
            (false, None) => String::new(),
        };
        let execution_columns = "snapshot_id,record_id,time,success,noop,key,execution_id,created_at,is_success,duration_sec,trade_count,symbol_count,position_count,positions_present,reason,raw_present,has_symbol_results,raw_status,raw_task_status,raw_error,raw_outcome,raw_outcome_reason,raw_reason_code,raw_msg,raw_execution_kind";
        let trade_columns = "snapshot_id,id,record_id,symbol,time,day,value,cost,loss_bp,fee,fee_currency,estimated,side,quantity,price,reference,reference_source";
        Ok(Self { cte: format!(
            "WITH base_executions AS (SELECT {execution_columns} FROM performance_cost_execution WHERE {}), \
             filtered_trades AS (SELECT {trade_columns} FROM performance_cost_trade WHERE {}), \
             filtered_executions AS (SELECT * FROM base_executions{}) ",
            execution_filters.join(" AND "), trade_filters.join(" AND "), execution_selection), args })
    }
    fn summary(&self, conn: &Connection) -> Result<Value, String> {
        let sql = format!("{} SELECT {AGGREGATES} FROM filtered_trades", self.cte);
        let mut summary = conn
            .query_row(&sql, params_from_iter(self.args.iter()), |row| {
                Summary::read(row, 0)
            })
            .map_err(sql_error)?;
        let sql = format!("{} SELECT fee_currency,SUM(fee),COUNT(*) FROM filtered_trades WHERE fee IS NOT NULL AND fee_currency IS NOT NULL GROUP BY fee_currency", self.cte);
        let mut stmt = conn.prepare(&sql).map_err(sql_error)?;
        let mut rows = stmt
            .query(params_from_iter(self.args.iter()))
            .map_err(sql_error)?;
        while let Some(row) = rows.next().map_err(sql_error)? {
            summary.add_fee(
                &row.get::<_, String>(0).map_err(sql_error)?,
                row.get(1).map_err(sql_error)?,
                row.get(2).map_err(sql_error)?,
            );
        }
        Ok(summary.output())
    }
}

pub fn query_costs(conn: &Connection, request: &Value) -> Result<Value, String> {
    let account = request
        .get("account_id")
        .and_then(Value::as_i64)
        .ok_or_else(|| failure("INVALID_COST_QUERY", "account_id is required"))?;
    let snapshot = string(request, "snapshot_id", "");
    let expected_logic = string(request, "logic_version", "18");
    let expected_engine = string(request, "engine_version", "equity-ledger-1");
    let (digest, offset) = cursor(request)?;
    let mut stmt = conn.prepare("SELECT data_until,logic_version,engine_version,cost_schema_version FROM performance_snapshot WHERE id=?1 AND account_id=?2").map_err(sql_error)?;
    let mut rows = stmt.query((snapshot, account)).map_err(sql_error)?;
    let row = rows
        .next()
        .map_err(sql_error)?
        .ok_or_else(|| failure("SNAPSHOT_EXPIRED", "快照版本已失效，请重新读取绩效"))?;
    let data_until: Option<String> = row.get(0).map_err(sql_error)?;
    let logic: String = row.get(1).map_err(sql_error)?;
    let engine: String = row.get(2).map_err(sql_error)?;
    let schema: i64 = row.get(3).map_err(sql_error)?;
    if logic != expected_logic || engine != expected_engine || schema != 1 {
        return Err(failure(
            "SNAPSHOT_EXPIRED",
            "快照版本已失效，请重新读取绩效",
        ));
    }
    drop(rows);
    drop(stmt);
    let dimension = string(request, "dimension", "execution");
    let sort = string(request, "sort", "time");
    if !["execution", "symbol", "trade", "summary"].contains(&dimension)
        || !["time", "cost"].contains(&sort)
    {
        return Err(failure(
            "INVALID_COST_QUERY",
            "invalid dimension or ordering",
        ));
    }
    let limit = request.get("limit").and_then(Value::as_i64).unwrap_or(20);
    if !(1..=100).contains(&limit) {
        return Err(failure(
            "INVALID_COST_QUERY",
            "limit must be between 1 and 100",
        ));
    }
    let range = string(request, "range", "all");
    if !["all", "30", "90"].contains(&range) {
        return Err(failure("INVALID_COST_QUERY", "invalid performance range"));
    }
    let start = request.get("start").filter(|value| !value.is_null());
    let end = request.get("end").filter(|value| !value.is_null());
    if start.is_some() != end.is_some() {
        return Err(failure(
            "INVALID_COST_QUERY",
            "start and end must define a nonempty interval",
        ));
    }
    if let Some(day) = optional_string(request, "day").filter(|day| !day.is_empty()) {
        let parsed = NaiveDate::parse_from_str(day, "%Y-%m-%d")
            .map_err(|_| failure("INVALID_COST_QUERY", "day must be YYYY-MM-DD"))?;
        if parsed.format("%Y-%m-%d").to_string() != day || start.is_some() {
            return Err(failure(
                "INVALID_COST_QUERY",
                "day must be canonical and cannot be combined with interval",
            ));
        }
    }
    if let Some(side) = optional_string(request, "side") {
        if !["buy", "sell", "none"].contains(&side) {
            return Err(failure("INVALID_COST_QUERY", "invalid trade side"));
        }
    }
    let scope = Scope::new(conn, request)?;
    let totals = scope.summary(conn)?;
    let execution_counts = conn.query_row(
        &format!("{} SELECT COUNT(*),SUM(CAST(success AS INTEGER)),SUM(CAST(noop AS INTEGER)) FROM filtered_executions", scope.cte),
        params_from_iter(scope.args.iter()), |row| Ok((row.get::<_, i64>(0)?, row.get::<_, Option<i64>>(1)?.unwrap_or(0), row.get::<_, Option<i64>>(2)?.unwrap_or(0))),
    ).map_err(sql_error)?;
    let (count, data) = match dimension {
        "summary" => (0, Vec::new()),
        "execution" => (
            execution_counts.0,
            execution_page(conn, &scope, sort, limit, offset, false)?.0,
        ),
        "symbol" => {
            let count = conn
                .query_row(
                    &format!(
                        "{} SELECT COUNT(DISTINCT symbol) FROM filtered_trades",
                        scope.cte
                    ),
                    params_from_iter(scope.args.iter()),
                    |row| row.get(0),
                )
                .map_err(sql_error)?;
            (count, symbol_page(conn, &scope, sort, limit, offset)?)
        }
        _ => (
            totals["count"].as_i64().unwrap_or(0),
            trade_page(conn, &scope, sort, limit, offset)?,
        ),
    };
    let next_offset = offset
        .checked_add(limit)
        .ok_or_else(|| failure("INVALID_COST_CURSOR", "游标与查询条件不匹配"))?;
    let next_cursor = if next_offset < count {
        Some(URL_SAFE.encode(format!(
            "{{\"scope\": \"{digest}\", \"offset\": {next_offset}}}"
        )))
    } else {
        None
    };
    Ok(
        json!({"snapshot_id": snapshot, "data_until": data_until, "summary": totals,
        "successful": execution_counts.1, "noop": execution_counts.2, "count": count,
        "data": data, "next_cursor": next_cursor}),
    )
}

fn cursor(request: &Value) -> Result<(String, i64), String> {
    let digest = optional_string(request, "cursor_scope")
        .filter(|scope| !scope.is_empty())
        .ok_or_else(|| {
            failure(
                "INVALID_COST_QUERY",
                "cursor_scope must be a nonempty string",
            )
        })?
        .to_owned();
    let mut offset = 0;
    if let Some(encoded) = optional_string(request, "cursor").filter(|value| !value.is_empty()) {
        let invalid = || failure("INVALID_COST_CURSOR", "游标与查询条件不匹配");
        if encoded.len() > 1024 {
            return Err(invalid());
        }
        let bytes = URL_SAFE.decode(encoded).map_err(|_| invalid())?;
        let decoded: Value = serde_json::from_slice(&bytes).map_err(|_| invalid())?;
        offset = decoded
            .get("offset")
            .and_then(Value::as_i64)
            .filter(|value| *value >= 0)
            .ok_or_else(invalid)?;
        if decoded.get("scope").and_then(Value::as_str) != Some(digest.as_str()) {
            return Err(invalid());
        }
    }
    Ok((digest, offset))
}

fn trade_page(
    conn: &Connection,
    scope: &Scope,
    sort: &str,
    limit: i64,
    offset: i64,
) -> Result<Vec<Value>, String> {
    let ordering = if sort == "cost" {
        "t.cost DESC NULLS LAST,t.time DESC,t.id ASC"
    } else {
        "t.time DESC,t.id ASC"
    };
    let sql = format!("{} SELECT t.symbol,t.day,t.time,t.estimated,t.side,t.quantity,t.price,t.reference,t.reference_source,t.value,t.cost,t.loss_bp,t.fee,t.fee_currency,t.id,t.record_id,e.execution_id FROM filtered_trades t JOIN base_executions e USING(snapshot_id,record_id) ORDER BY {ordering} LIMIT {limit} OFFSET {offset}", scope.cte);
    let mut stmt = conn.prepare(&sql).map_err(sql_error)?;
    let rows = stmt.query_map(params_from_iter(scope.args.iter()), |row| Ok(json!({
        "symbol": row.get::<_, String>(0)?, "day": row.get::<_, String>(1)?, "time": row.get::<_, f64>(2)?,
        "timeEstimated": row.get::<_, bool>(3)?, "side": row.get::<_, String>(4)?, "quantity": row.get::<_, Option<f64>>(5)?,
        "price": row.get::<_, Option<f64>>(6)?, "reference": row.get::<_, Option<f64>>(7)?, "referenceSource": row.get::<_, Option<String>>(8)?,
        "value": row.get::<_, Option<f64>>(9)?, "cost": row.get::<_, Option<f64>>(10)?, "lossBp": row.get::<_, Option<f64>>(11)?,
        "fee": row.get::<_, Option<f64>>(12)?, "feeCurrency": row.get::<_, Option<String>>(13)?, "trade_id": row.get::<_, i64>(14)?,
        "record_id": row.get::<_, i64>(15)?, "execution_id": row.get::<_, Option<String>>(16)?,
    }))).map_err(sql_error)?;
    rows.collect::<Result<Vec<_>, _>>().map_err(sql_error)
}

fn symbol_page(
    conn: &Connection,
    scope: &Scope,
    sort: &str,
    limit: i64,
    offset: i64,
) -> Result<Vec<Value>, String> {
    let ordering = if sort == "cost" {
        "SUM(cost) DESC NULLS LAST,symbol ASC"
    } else {
        "symbol ASC"
    };
    let sql = format!("{} SELECT symbol,MAX(time),{AGGREGATES}, \
        SUM(CASE WHEN side='buy' THEN quantity ELSE 0 END),SUM(CASE WHEN side='sell' THEN quantity ELSE 0 END), \
        COUNT(CASE WHEN side='none' OR quantity IS NULL THEN 1 END) \
        FROM filtered_trades GROUP BY symbol ORDER BY {ordering} LIMIT {limit} OFFSET {offset}", scope.cte);
    let mut stmt = conn.prepare(&sql).map_err(sql_error)?;
    let mut rows = stmt
        .query(params_from_iter(scope.args.iter()))
        .map_err(sql_error)?;
    let mut output = Vec::new();
    while let Some(row) = rows.next().map_err(sql_error)? {
        let symbol: String = row.get(0).map_err(sql_error)?;
        let summary = Summary::read(row, 2).map_err(sql_error)?;
        output.push(json!({"symbol": symbol, "lastTime": row.get::<_, Option<f64>>(1).map_err(sql_error)?,
            "summary": summary.output(), "buy": quantity_total(row, 10).map_err(sql_error)?,
            "sell": quantity_total(row, 11).map_err(sql_error)?, "quantityIncomplete": row.get::<_, i64>(12).map_err(sql_error)? != 0}));
    }
    drop(rows);
    drop(stmt);
    attach_page_fees(conn, scope, &mut output, "symbol", "symbol")?;
    Ok(output)
}

fn attach_page_fees(
    conn: &Connection,
    scope: &Scope,
    output: &mut [Value],
    field: &str,
    column: &str,
) -> Result<(), String> {
    if output.is_empty() {
        return Ok(());
    }
    let mut args = scope.args.clone();
    let mut lookup = HashMap::new();
    let mut placeholders = Vec::new();
    for (index, value) in output.iter().enumerate() {
        let key = if field == "record_id" {
            value["record"]["id"].clone()
        } else {
            value[field].clone()
        };
        let sql_value = match &key {
            Value::String(text) => SqlValue::Text(text.clone()),
            Value::Number(number) => SqlValue::Integer(
                number
                    .as_i64()
                    .ok_or_else(|| failure("ANALYSIS_READ_ERROR", "invalid cost page identity"))?,
            ),
            _ => return Err(failure("ANALYSIS_READ_ERROR", "invalid cost page identity")),
        };
        placeholders.push(bind(&mut args, sql_value));
        lookup.insert(key.to_string(), index);
    }
    let sql = format!("{} SELECT {column},fee_currency,SUM(fee),COUNT(*) FROM filtered_trades WHERE {column} IN ({}) AND fee IS NOT NULL AND fee_currency IS NOT NULL GROUP BY {column},fee_currency", scope.cte, placeholders.join(","));
    let mut stmt = conn.prepare(&sql).map_err(sql_error)?;
    let mut rows = stmt
        .query(params_from_iter(args.iter()))
        .map_err(sql_error)?;
    while let Some(row) = rows.next().map_err(sql_error)? {
        let key = if column == "record_id" {
            json!(row.get::<_, i64>(0).map_err(sql_error)?)
        } else {
            json!(row.get::<_, String>(0).map_err(sql_error)?)
        };
        if let Some(index) = lookup.get(&key.to_string()) {
            let currency: String = row.get(1).map_err(sql_error)?;
            output[*index]["summary"]["fees"][currency] =
                json!(row.get::<_, f64>(2).map_err(sql_error)?);
            let previous = output[*index]["summary"]["feeCovered"]
                .as_i64()
                .unwrap_or(0);
            output[*index]["summary"]["feeCovered"] =
                json!(previous + row.get::<_, i64>(3).map_err(sql_error)?);
        }
    }
    Ok(())
}

fn execution_page(
    conn: &Connection,
    scope: &Scope,
    sort: &str,
    limit: i64,
    offset: i64,
    historical_summary: bool,
) -> Result<(Vec<Value>, Option<(f64, i64)>), String> {
    let ordering = if sort == "cost" {
        "a.total_cost DESC NULLS LAST,e.time DESC,e.record_id DESC"
    } else {
        "e.time DESC,e.record_id DESC"
    };
    let aggregate_sql = if historical_summary {
        EXECUTION_AGGREGATES.replace(
            "SUM(cost) AS total_cost",
            "SUM(CASE WHEN cost IS NOT NULL AND value IS NOT NULL THEN cost END) AS total_cost",
        )
    } else {
        EXECUTION_AGGREGATES.to_owned()
    };
    let sql = format!("{}, per_record AS ({aggregate_sql}) SELECT e.record_id,e.key,e.execution_id,e.created_at,e.is_success,e.noop,e.duration_sec,e.trade_count,e.position_count,e.positions_present,e.reason,e.raw_present,e.has_symbol_results,e.raw_status,e.raw_task_status,e.raw_error,e.raw_outcome,e.raw_outcome_reason,e.raw_reason_code,e.raw_msg,e.raw_execution_kind,COALESCE(a.trade_rows,0),COALESCE(a.valued,0),a.total_value,a.total_cost,COALESCE(a.covered,0),a.covered_value,a.weighted_loss,a.estimated_count,COALESCE(a.symbols,0),e.time FROM filtered_executions e LEFT JOIN per_record a USING(record_id) ORDER BY {ordering} LIMIT {limit} OFFSET {offset}", scope.cte.trim_end());
    let mut stmt = conn.prepare(&sql).map_err(sql_error)?;
    let mut rows = stmt
        .query(params_from_iter(scope.args.iter()))
        .map_err(sql_error)?;
    let mut output = Vec::new();
    let mut last = None;
    while let Some(row) = rows.next().map_err(sql_error)? {
        last = Some((
            row.get(30).map_err(sql_error)?,
            row.get(0).map_err(sql_error)?,
        ));
        let mut raw = Map::new();
        let mask: i64 = row.get(11).map_err(sql_error)?;
        for (index, key) in RAW_KEYS.iter().enumerate() {
            if mask & (1 << index) != 0 {
                raw.insert(
                    (*key).to_owned(),
                    json!(row
                        .get::<_, Option<String>>(13 + index)
                        .map_err(sql_error)?),
                );
            }
        }
        if row.get::<_, bool>(12).map_err(sql_error)? {
            raw.insert("symbol_results".to_owned(), json!({}));
        }
        let positions_present: bool = row.get(9).map_err(sql_error)?;
        let summary = Summary::read(row, 21).map_err(sql_error)?;
        output.push(json!({
            "key": row.get::<_, Option<String>>(1).map_err(sql_error)?,
            "record": {"id":row.get::<_, i64>(0).map_err(sql_error)?, "execution_id":row.get::<_, Option<String>>(2).map_err(sql_error)?,
                "created_at":row.get::<_, Option<String>>(3).map_err(sql_error)?, "is_success":row.get::<_, Option<i64>>(4).map_err(sql_error)?, "raw_result": raw},
            "noop":row.get::<_, bool>(5).map_err(sql_error)?, "durationSec":row.get::<_, Option<f64>>(6).map_err(sql_error)?,
            "tradeCount":row.get::<_, Option<i64>>(7).map_err(sql_error)?, "positionCount":row.get::<_, Option<i64>>(8).map_err(sql_error)?,
            "positions":if positions_present {json!([])} else {Value::Null}, "reason":row.get::<_, Option<String>>(10).map_err(sql_error)?,
            "transactions":[], "attempts":[], "symbolCount":row.get::<_, i64>(29).map_err(sql_error)?, "summary":if historical_summary {summary.historical_output()} else {summary.output()},
        }));
    }
    drop(rows);
    drop(stmt);
    attach_page_fees(conn, scope, &mut output, "record_id", "record_id")?;
    attach_execution_children(conn, &scope.args[0], &mut output)?;
    Ok((output, last))
}

fn attach_execution_children(
    conn: &Connection,
    snapshot: &SqlValue,
    output: &mut [Value],
) -> Result<(), String> {
    if output.is_empty() {
        return Ok(());
    }
    let mut args = vec![snapshot.clone()];
    let mut lookup = HashMap::new();
    let ids = output
        .iter()
        .enumerate()
        .map(|(index, value)| {
            let record_id = value["record"]["id"].as_i64().unwrap();
            lookup.insert(record_id, index);
            bind(&mut args, record_id)
        })
        .collect::<Vec<_>>()
        .join(",");
    let predicate = format!("snapshot_id=?1 AND record_id IN ({ids})");
    let mut stmt = conn.prepare(&format!("SELECT record_id,symbol,present,status,error,outcome,outcome_reason,reason_code FROM performance_cost_execution_symbol WHERE {predicate} ORDER BY record_id,symbol")).map_err(sql_error)?;
    let mut rows = stmt
        .query(params_from_iter(args.iter()))
        .map_err(sql_error)?;
    while let Some(row) = rows.next().map_err(sql_error)? {
        let record: i64 = row.get(0).map_err(sql_error)?;
        let symbol: String = row.get(1).map_err(sql_error)?;
        let mask: i64 = row.get(2).map_err(sql_error)?;
        let mut status = Map::new();
        for (index, key) in SYMBOL_KEYS.iter().enumerate() {
            if mask & (1 << index) != 0 {
                status.insert(
                    (*key).to_owned(),
                    json!(row.get::<_, Option<String>>(index + 3).map_err(sql_error)?),
                );
            }
        }
        if let Some(index) = lookup.get(&record) {
            output[*index]["record"]["raw_result"]["symbol_results"][symbol] =
                Value::Object(status);
        }
    }
    drop(rows);
    drop(stmt);
    for (table, columns, target) in [
        (
            "performance_cost_position",
            "symbol,quantity,direction",
            "positions",
        ),
        (
            "performance_cost_attempt",
            "symbol,planned,filled,reason",
            "attempts",
        ),
    ] {
        let mut stmt = conn
            .prepare(&format!(
                "SELECT record_id,{columns} FROM {table} WHERE {predicate} ORDER BY record_id,idx"
            ))
            .map_err(sql_error)?;
        let mut rows = stmt
            .query(params_from_iter(args.iter()))
            .map_err(sql_error)?;
        while let Some(row) = rows.next().map_err(sql_error)? {
            let record: i64 = row.get(0).map_err(sql_error)?;
            let item = if target == "positions" {
                json!({"symbol":row.get::<_,String>(1).map_err(sql_error)?,"quantity":row.get::<_,Option<f64>>(2).map_err(sql_error)?,"direction":row.get::<_,String>(3).map_err(sql_error)?})
            } else {
                json!({"symbol":row.get::<_,String>(1).map_err(sql_error)?,"planned":row.get::<_,Option<f64>>(2).map_err(sql_error)?,"filled":row.get::<_,Option<f64>>(3).map_err(sql_error)?,"reason":row.get::<_,String>(4).map_err(sql_error)?})
            };
            if let Some(index) = lookup.get(&record) {
                if let Some(array) = output[*index][target].as_array_mut() {
                    array.push(item);
                }
            }
        }
    }
    let mut stmt=conn.prepare(&format!("SELECT record_id,idx,symbol,side,quantity,price,reference,reference_source,time,end_time,time_estimated,\"before\",target,value,cost,loss_bp,coverage,covered,count,amount_complete,fee_covered FROM performance_cost_transaction WHERE {predicate} ORDER BY record_id,idx")).map_err(sql_error)?;
    let mut rows = stmt
        .query(params_from_iter(args.iter()))
        .map_err(sql_error)?;
    let mut transaction_lookup = HashMap::new();
    while let Some(row) = rows.next().map_err(sql_error)? {
        let record: i64 = row.get(0).map_err(sql_error)?;
        let transaction: i64 = row.get(1).map_err(sql_error)?;
        let item = json!({"symbol":row.get::<_,String>(2).map_err(sql_error)?,"side":row.get::<_,String>(3).map_err(sql_error)?,
            "quantity":row.get::<_,Option<f64>>(4).map_err(sql_error)?,"price":row.get::<_,Option<f64>>(5).map_err(sql_error)?,"reference":row.get::<_,Option<f64>>(6).map_err(sql_error)?,"referenceSource":row.get::<_,Option<String>>(7).map_err(sql_error)?,
            "time":row.get::<_,Option<f64>>(8).map_err(sql_error)?,"endTime":row.get::<_,Option<f64>>(9).map_err(sql_error)?,"timeEstimated":row.get::<_,bool>(10).map_err(sql_error)?,"before":row.get::<_,Option<f64>>(11).map_err(sql_error)?,"target":row.get::<_,Option<f64>>(12).map_err(sql_error)?,
            "summary":{"value":row.get::<_,Option<f64>>(13).map_err(sql_error)?,"cost":row.get::<_,Option<f64>>(14).map_err(sql_error)?,"lossBp":row.get::<_,Option<f64>>(15).map_err(sql_error)?,"coverage":row.get::<_,Option<f64>>(16).map_err(sql_error)?,"covered":row.get::<_,i64>(17).map_err(sql_error)?,"count":row.get::<_,i64>(18).map_err(sql_error)?,"amountComplete":row.get::<_,bool>(19).map_err(sql_error)?,"fees":{},"feeCovered":row.get::<_,i64>(20).map_err(sql_error)?}});
        if let Some(index) = lookup.get(&record) {
            let array = output[*index]["transactions"].as_array_mut().unwrap();
            transaction_lookup.insert((record, transaction), (*index, array.len()));
            array.push(item);
        }
    }
    drop(rows);
    drop(stmt);
    let mut stmt=conn.prepare(&format!("SELECT record_id,transaction_idx,currency,value FROM performance_cost_transaction_fee WHERE {predicate} ORDER BY record_id,transaction_idx,currency")).map_err(sql_error)?;
    let mut rows = stmt
        .query(params_from_iter(args.iter()))
        .map_err(sql_error)?;
    while let Some(row) = rows.next().map_err(sql_error)? {
        let record: i64 = row.get(0).map_err(sql_error)?;
        let transaction: i64 = row.get(1).map_err(sql_error)?;
        if let Some((index, item)) = transaction_lookup.get(&(record, transaction)) {
            let currency: String = row.get(2).map_err(sql_error)?;
            output[*index]["transactions"][*item]["summary"]["fees"][currency] =
                json!(row.get::<_, f64>(3).map_err(sql_error)?);
        }
    }
    Ok(())
}

/// Reconstruct the legacy performance execution list only for the final response.
///
/// Source rows and child previews are read in keyset batches of at most 100.
/// The returned vector is the requested API output, never a computation input.
pub(crate) fn snapshot_executions(
    conn: &Connection,
    snapshot_id: &str,
    baseline: f64,
    end: f64,
) -> Result<Vec<Value>, String> {
    if !baseline.is_finite() || !end.is_finite() {
        return Err(failure(
            "SNAPSHOT_INVALID",
            "execution range must be finite",
        ));
    }
    let mut output = Vec::new();
    let mut before: Option<(f64, i64)> = None;
    loop {
        let mut args = vec![
            SqlValue::Text(snapshot_id.to_owned()),
            SqlValue::Real(baseline),
            SqlValue::Real(end),
        ];
        let keyset = if let Some((time, record)) = before {
            let time_param = bind(&mut args, time);
            let record_param = bind(&mut args, record);
            format!(" AND (time<{time_param} OR (time={time_param} AND record_id<{record_param}))")
        } else {
            String::new()
        };
        let cte = format!(
            "WITH base_executions AS (SELECT snapshot_id,record_id,time,success,noop,key,execution_id,created_at,is_success,duration_sec,trade_count,symbol_count,position_count,positions_present,reason,raw_present,has_symbol_results,raw_status,raw_task_status,raw_error,raw_outcome,raw_outcome_reason,raw_reason_code,raw_msg,raw_execution_kind FROM performance_cost_execution WHERE snapshot_id=?1 AND time>=?2 AND time<=?3{keyset} ORDER BY time DESC,record_id DESC LIMIT 100), \
             filtered_trades AS (SELECT snapshot_id,id,record_id,symbol,time,day,value,cost,loss_bp,fee,fee_currency,estimated,side,quantity,price,reference,reference_source FROM performance_cost_trade WHERE snapshot_id=?1 AND record_id IN (SELECT record_id FROM base_executions)), \
             filtered_executions AS (SELECT * FROM base_executions) "
        );
        let scope = Scope { cte, args };
        let (mut batch, last) = execution_page(conn, &scope, "time", 100, 0, true)?;
        let size = batch.len();
        before = last;
        output.append(&mut batch);
        if size < 100 {
            break;
        }
    }
    output.reverse();
    Ok(output)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn empty_cursor_matches_first_page() {
        let first_page = json!({"snapshot_id": "v1", "cursor_scope": "known"});
        let mut empty = first_page.clone();
        empty["cursor"] = json!("");
        assert_eq!(cursor(&empty).unwrap(), ("known".to_owned(), 0));
        assert_eq!(cursor(&empty).unwrap(), cursor(&first_page).unwrap());
        empty["cursor"] = Value::Null;
        assert_eq!(cursor(&empty).unwrap(), cursor(&first_page).unwrap());
        empty["cursor"] = json!("invalid");
        assert!(cursor(&empty)
            .unwrap_err()
            .starts_with("INVALID_COST_CURSOR:"));
    }

    #[test]
    fn cursor_requires_supplied_scope() {
        for request in [
            json!({"snapshot_id": "v1"}),
            json!({"cursor_scope": null}),
            json!({"cursor_scope": 42}),
            json!({"cursor_scope": ""}),
        ] {
            assert_eq!(
                cursor(&request).unwrap_err(),
                "INVALID_COST_QUERY:cursor_scope must be a nonempty string"
            );
        }
    }

    #[test]
    fn cursor_scope_and_integer_offset_are_required() {
        let request = json!({"snapshot_id":"v1","cursor_scope":"known"});
        assert_eq!(cursor(&request).unwrap(), ("known".to_owned(), 0));
        for payload in [
            json!({"scope":"other","offset":1}),
            json!({"scope":"known","offset":true}),
            json!({"scope":"known","offset":-1}),
            json!({"scope":"known","offset":1.0}),
        ] {
            let mut invalid = request.clone();
            invalid["cursor"] = json!(URL_SAFE.encode(serde_json::to_vec(&payload).unwrap()));
            assert!(cursor(&invalid)
                .unwrap_err()
                .starts_with("INVALID_COST_CURSOR:"));
        }
        let mut valid = request;
        valid["cursor"] = json!(URL_SAFE.encode(br#"{"scope":"known","offset":20}"#));
        assert_eq!(cursor(&valid).unwrap().1, 20);
    }
    #[test]
    fn snapshot_execution_batches_preserve_order_and_ignore_json_payloads() {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE performance_cost_execution(snapshot_id TEXT,record_id INTEGER,time REAL,success INTEGER,noop INTEGER,key TEXT,execution_id TEXT,created_at TEXT,is_success INTEGER,duration_sec REAL,trade_count INTEGER,symbol_count INTEGER,position_count INTEGER,positions_present INTEGER,reason TEXT,raw_present INTEGER,has_symbol_results INTEGER,raw_status TEXT,raw_task_status TEXT,raw_error TEXT,raw_outcome TEXT,raw_outcome_reason TEXT,raw_reason_code TEXT,raw_msg TEXT,raw_execution_kind TEXT,payload TEXT,PRIMARY KEY(snapshot_id,record_id));
             CREATE INDEX execution_time ON performance_cost_execution(snapshot_id,time);
             CREATE TABLE performance_cost_trade(snapshot_id TEXT,id INTEGER,record_id INTEGER,symbol TEXT,time REAL,day TEXT,value REAL,cost REAL,loss_bp REAL,fee REAL,fee_currency TEXT,estimated INTEGER,side TEXT,quantity REAL,price REAL,reference REAL,reference_source TEXT,payload TEXT);
             CREATE TABLE performance_cost_execution_symbol(snapshot_id TEXT,record_id INTEGER,symbol TEXT,present INTEGER,status TEXT,error TEXT,outcome TEXT,outcome_reason TEXT,reason_code TEXT);
             CREATE TABLE performance_cost_position(snapshot_id TEXT,record_id INTEGER,idx INTEGER,symbol TEXT,quantity REAL,direction TEXT);
             CREATE TABLE performance_cost_attempt(snapshot_id TEXT,record_id INTEGER,idx INTEGER,symbol TEXT,planned REAL,filled REAL,reason TEXT);
             CREATE TABLE performance_cost_transaction(snapshot_id TEXT,record_id INTEGER,idx INTEGER,symbol TEXT,side TEXT,quantity REAL,price REAL,reference REAL,reference_source TEXT,time REAL,end_time REAL,time_estimated INTEGER,\"before\" REAL,target REAL,value REAL,cost REAL,loss_bp REAL,coverage REAL,covered INTEGER,count INTEGER,amount_complete INTEGER,fee_covered INTEGER);
             CREATE TABLE performance_cost_transaction_fee(snapshot_id TEXT,record_id INTEGER,transaction_idx INTEGER,currency TEXT,value REAL);"
        ).unwrap();
        for record in 1..=201 {
            conn.execute("INSERT INTO performance_cost_execution(snapshot_id,record_id,time,success,noop,key,execution_id,created_at,is_success,trade_count,symbol_count,positions_present,reason,raw_present,has_symbol_results,payload) VALUES('v1',?1,?2,1,1,?3,?3,'2026-01-01T00:00:00',1,0,0,0,'',0,0,'not-json')",rusqlite::params![record, f64::from(record / 2), record.to_string()]).unwrap();
        }
        conn.execute("INSERT INTO performance_cost_execution_symbol VALUES('v1',100,'A',5,'BLOCKED',NULL,NULL,NULL,NULL)", []).unwrap();
        let executions = snapshot_executions(&conn, "v1", 0.0, 1000.0).unwrap();
        assert_eq!(executions.len(), 201);
        for (index, execution) in executions.iter().enumerate() {
            assert_eq!(execution["record"]["id"], json!(index + 1));
            assert!(execution["summary"].get("estimated").is_none());
            assert!(execution["summary"].get("coveredValue").is_none());
            assert_eq!(execution["positions"], Value::Null);
            assert_eq!(execution["summary"]["cost"], Value::Null);
        }
        assert_eq!(executions[99]["record"]["raw_result"]["symbol_results"]["A"], json!({"status":"BLOCKED","outcome":null}));
        let selected = snapshot_executions(&conn, "v1", 50.0, 60.0).unwrap();
        assert_eq!(selected.len(), 22);
        assert_eq!(selected.first().unwrap()["record"]["id"], json!(100));
        assert_eq!(selected.last().unwrap()["record"]["id"], json!(121));
        let exact = snapshot_executions(&conn, "v1", 1.0, 50.0).unwrap();
        assert_eq!(exact.len(), 100);
        assert_eq!(exact.first().unwrap()["record"]["id"], json!(2));
        assert_eq!(exact.last().unwrap()["record"]["id"], json!(101));
        assert!(snapshot_executions(&conn, "v1", 1001.0, 1002.0).unwrap().is_empty());
    }
    #[test]
    fn summary_preserves_empty_and_uncovered_amounts() {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE facts(value REAL,cost REAL,loss_bp REAL,estimated INTEGER);",
        )
        .unwrap();
        let empty = conn
            .query_row(&format!("SELECT {AGGREGATES} FROM facts"), [], |row| {
                Summary::read(row, 0)
            })
            .unwrap()
            .output();
        assert_eq!(empty["coveredValue"], Value::Null);
        assert_eq!(empty["value"], Value::Null);
        assert_eq!(empty["amountComplete"], json!(true));
        assert_eq!(empty["estimated"], json!(0));
        conn.execute("INSERT INTO facts VALUES(NULL,7,NULL,1)", [])
            .unwrap();
        let partial = conn
            .query_row(&format!("SELECT {AGGREGATES} FROM facts"), [], |row| {
                Summary::read(row, 0)
            })
            .unwrap()
            .output();
        assert_eq!(partial["cost"], json!(7.0));
        assert_eq!(partial["coveredValue"], json!(0));
        assert_eq!(partial["amountComplete"], json!(false));
        assert_eq!(partial["lossBp"], Value::Null);
    }
}
