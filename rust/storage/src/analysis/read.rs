//! Read daily results and derive requested details directly from canonical business facts.
use super::{self as store, failure, sql, State};
use crate::{cost_summary::Summary, error::AnalysisError, fact_trades::Trade};
use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine as _};
use rusqlite::Connection;
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet};

fn iso(time: i64) -> Result<String, AnalysisError> {
    crate::time_us::format(time).map_err(|e| failure("ANALYSIS_TIME", e))
}
fn optional_iso(time: Option<i64>) -> Result<Option<String>, AnalysisError> {
    time.map(iso).transpose()
}
fn ms(time: i64) -> f64 {
    time as f64 / 1000.0
}

pub(super) fn account(c: &Connection, id: i64) -> Result<(), AnalysisError> {
    let exists: bool = c
        .query_row(
            "SELECT EXISTS(SELECT 1 FROM account WHERE id=?1)",
            [id],
            |r| r.get(0),
        )
        .map_err(sql)?;
    if exists {
        Ok(())
    } else {
        Err(failure("ACCOUNT_NOT_FOUND", "账户不存在"))
    }
}
pub(super) fn guard(
    c: &Connection,
    id: i64,
    version: &str,
    key: &str,
) -> Result<State, AnalysisError> {
    account(c, id)?;
    let state =
        store::state(c, id, key)?.ok_or_else(|| failure("ANALYSIS_PENDING", "分析尚未完成"))?;
    let source: i64 = c
        .query_row(
            "SELECT source_version FROM account_analysis WHERE account_id=?1",
            [id],
            |r| r.get(0),
        )
        .map_err(sql)?;
    if state.version.to_string() != version
        || state.version != source
        || state.logic_version != store::LOGIC_VERSION
        || state.engine_version != crate::replay::ENGINE_VERSION
    {
        return Err(failure(
            "ANALYSIS_VERSION_CHANGED",
            "分析或业务事实已更新，请重新读取",
        ));
    }
    Ok(state)
}
pub(crate) fn status(c: &Connection, id: i64) -> Result<Value, AnalysisError> {
    account(c, id)?;
    let s = store::state(c, id, "all")?;
    let q=store::rows(c,"SELECT source_version,requested,running_version,error,retry_at_us FROM account_analysis WHERE account_id=?1",&[&id])?.pop().unwrap_or_else(||json!({"source_version":1}));
    let source = q["source_version"].as_i64().unwrap_or(1);
    let compatible = s.as_ref().is_some_and(|s| {
        s.logic_version == store::LOGIC_VERSION && s.engine_version == crate::replay::ENGINE_VERSION
    });
    let state = if !q["error"].is_null() {
        "failed"
    } else if compatible
        && s.as_ref().is_some_and(|s| s.version == source)
        && q["requested"] != 1
        && q["running_version"].is_null()
    {
        "ready"
    } else if compatible {
        "stale"
    } else if q["requested"] == 1 || !q["running_version"].is_null() {
        "pending"
    } else {
        "empty"
    };
    Ok(
        json!({"schema_version":1,"status":state,"source_version":source,"version":s.as_ref().filter(|_|compatible).map(|s|s.version.to_string()),"logic_version":store::LOGIC_VERSION,"engine_version":crate::replay::ENGINE_VERSION,"computed_at":s.as_ref().map(|s|iso(s.computed_at)).transpose()?,"data_until":optional_iso(s.as_ref().and_then(|s|s.last_time))?,"error":q["error"],"retry_at":q["retry_at_us"].as_i64().map(|v| v as f64 / 1_000_000.0)}),
    )
}
fn point_rows(c: &Connection, s: &State) -> Result<Vec<Value>, AnalysisError> {
    let mut points = store::rows(
        c,
        "SELECT * FROM analysis_day WHERE account_id=?1 AND range_key=?2 ORDER BY day",
        &[&s.account_id, &s.range_key],
    )?;
    for p in &mut points {
        let body = p.as_object_mut().unwrap();
        let day = body.remove("day").unwrap();
        body.insert("date".into(), day);
        let time = body
            .remove("observed_at_us")
            .and_then(|v| v.as_i64())
            .ok_or_else(|| failure("ANALYSIS_STATE", "missing observation time"))?;
        body.insert("observed_at".into(), json!(iso(time)?));
        body.remove("account_id");
        body.remove("range_key");
        let b = body["sizing_fallback"] == 1;
        body.insert("sizing_fallback".into(), json!(b));
    }
    if let (Some(time), Some(equity)) = (&s.baseline_time, s.baseline_asset) {
        points.insert(0,json!({"date":crate::time_us::local_datetime(*time).map_err(|e|failure("ANALYSIS_TIME",e))?.format("%Y-%m-%dT%H:%M:%S%.6f").to_string(),"observed_at":iso(*time)?,"record_id":s.baseline_id,"execution_id":s.baseline_execution,"account_return":0.0,"account_equity":equity,"account_daily_return":null,"portfolio_return":s.executable_start.as_ref().filter(|t|*t<=time).map(|_|0.0),"portfolio_daily_return":null,"target_portfolio_return":s.target_start.as_ref().filter(|t|*t<=time).map(|_|0.0),"target_portfolio_daily_return":null,"difference":s.executable_start.as_ref().filter(|t|*t<=time).map(|_|0.0),"sizing_fallback":false}));
    }
    Ok(points)
}
pub(crate) fn summaries(c: &Connection, ids: &[i64]) -> Result<Value, AnalysisError> {
    let mut out = serde_json::Map::new();
    for id in ids {
        let meta = status(c, *id)?;
        let s = store::state(c, *id, "all")?;
        let points = s
            .as_ref()
            .map(|s| point_rows(c, s))
            .transpose()?
            .unwrap_or_default();
        let last = points.last().cloned().unwrap_or(Value::Null);
        out.insert(id.to_string(),json!({"version":meta["version"],"status":meta["status"],"computed_at":meta["computed_at"],"observed_at":last["observed_at"],"account_equity":last["account_equity"],"account_daily_return":last["account_daily_return"],"points":points}));
    }
    Ok(Value::Object(out))
}
fn summary(trades: &[Trade]) -> Summary {
    let mut s = Summary::empty();
    for t in trades {
        s.push(t);
    }
    s
}
fn snake_summary(s: &Summary) -> Value {
    json!({"value":s.value,"cost":s.cost,"loss_bp":s.loss_bp(),"coverage":s.coverage(),"covered":s.covered,"count":s.count,"amount_complete":s.amount_complete(),"fee_covered":s.fee_count,"fees":s.fees.iter().map(|(c,(v,_))|(c.clone(),json!(v))).collect::<serde_json::Map<_,_>>()})
}
struct Record {
    head: Value,
    trades: Vec<Trade>,
    positions: Option<usize>,
    noop: bool,
    time: i64,
}
fn records(c: &Connection, s: &State, only: Option<i64>) -> Result<Vec<Record>, AnalysisError> {
    let start = s.baseline_time.unwrap_or(i64::MIN);
    let end = s.last_time.unwrap_or(i64::MIN);
    let heads=store::rows(c,"SELECT * FROM executerecord WHERE account_id=?1 AND result_staged=0 AND (?2 IS NULL OR id=?2) AND created_at_us>=?3 AND created_at_us<=?4 ORDER BY created_at_us,id",&[&s.account_id,&only,&start,&end])?;
    let mut ticks = crate::fact_ticks::TickReader::new(c).map_err(sql)?;
    let mut reader = crate::fact_trades::TradeReader::new(c).map_err(sql)?;
    let mut position_reader = crate::fact_positions::PositionReader::new(c).map_err(sql)?;
    let mut out = Vec::new();
    for mut head in heads {
        let id = head["id"].as_i64().unwrap();
        let when = head["created_at_us"].as_i64().unwrap();
        let prices = ticks.read(id).map_err(sql)?;
        let trades = reader
            .read(id, when, &prices)
            .map_err(|e| failure("ANALYSIS_FACTS", e))?;
        let positions = position_reader
            .read(id)
            .map_err(|e| failure("ANALYSIS_FACTS", e))?
            .map(|v| v.len());
        let has_fills:bool=c.query_row("SELECT EXISTS(SELECT 1 FROM execution_order WHERE record_id=?1 AND filled_volume>0)",[id],|r|r.get(0)).map_err(sql)?;
        let (has_symbols,non_noop):(bool,bool)=c.query_row("SELECT count(*)>0,COALESCE(sum(status IS NOT 'NOOP'),0)>0 FROM execution_symbol_result WHERE record_id=?1",[id],|r|Ok((r.get(0)?,r.get(1)?))).map_err(sql)?;
        let noop = head["is_success"] == 1
            && trades.is_empty()
            && !has_fills
            && (head["display_status"] == "NOOP" || has_symbols && !non_noop);
        let time = when;
        head["created_at"] = json!(iso(when)?);
        head["execution_time"] = json!(head["execution_time_us"]
            .as_i64()
            .map(|v| v as f64 / 1_000_000.0));
        out.push(Record {
            head,
            trades,
            positions,
            noop,
            time,
        });
    }
    Ok(out)
}
fn execution_summary(r: &Record) -> Value {
    json!({"record_id":r.head["id"],"execution_id":r.head["execution_id"],"time_ms":ms(r.time),"created_at":r.head["created_at"],"is_success":r.head["is_success"],"noop":r.noop,"has_transactions":r.trades.iter().any(|t|t.quantity.is_some()),"duration_sec":r.head["execution_time"],"symbol_count":r.trades.iter().filter(|t|t.quantity.is_some()).map(|t|&t.symbol).collect::<BTreeSet<_>>().len(),"position_count":r.positions,"positions_present":r.positions.is_some(),"reason":r.head["display_error"],"summary":summary(&r.trades).output()})
}
fn events(c: &Connection, s: &State) -> Result<(Vec<Value>, usize), AnalysisError> {
    let start = s.baseline_time.unwrap_or(s.start);
    let mut rows=store::rows(c,"SELECT created_at_us AS time,'换绑' AS tag,CASE WHEN portfolio_id IS NULL THEN '解绑组合' ELSE '组合 #'||portfolio_id END AS text,NULL AS executionId FROM portfolioaccount WHERE account_id=?1 AND created_at_us>=?2 AND created_at_us<=?3 UNION ALL SELECT triggered_at_us,'跳过',CASE WHEN reason_code='BUSY' THEN '执行中' ELSE '休市' END,NULL FROM schedule_skip WHERE account_id=?1 AND triggered_at_us>=?2 AND triggered_at_us<=?3",&[&s.account_id,&start,&s.last_time])?;
    let mut labels = crate::fact_events::EventReader::new(c).map_err(sql)?;
    for r in store::rows(c,"SELECT id,created_at_us,execution_id,display_status,task_status,reason_code FROM executerecord WHERE account_id=?1 AND result_staged=0 AND created_at_us>=?2 AND created_at_us<=?3",&[&s.account_id,&start,&s.last_time])? {
        let text=labels.label(r["id"].as_i64().unwrap(),r["display_status"].as_str(),r["task_status"].as_str(),r["reason_code"].as_str()).map_err(sql)?;
        rows.push(json!({"time":r["created_at_us"],"tag":"执行","text":text,"executionId":r["execution_id"]}));
    }
    let total = rows.len();
    let mut rows = rows;
    rows.sort_by_key(|v| std::cmp::Reverse(v["time"].as_i64().unwrap()));
    rows.truncate(100);
    for row in &mut rows {
        row["time"] = json!(iso(row["time"].as_i64().unwrap())?);
    }
    Ok((rows, total))
}
pub(crate) fn chart(
    c: &Connection,
    id: i64,
    version: &str,
    key: &str,
) -> Result<Value, AnalysisError> {
    let s = guard(c, id, version, key)?;
    let points = point_rows(c, &s)?;
    let mut result = json!({"range":key,"settings":{"backtest_weight_type":"cs","backtest_fee_rate":s.fee_rate},"engine_version":s.engine_version,"backtest_included":true,"baseline":optional_iso(s.baseline_time)?,"end":optional_iso(s.last_time)?,"record_count":c.query_row("SELECT count(*) FROM executerecord WHERE account_id=?1 AND result_staged=0",[id],|r|r.get::<_,i64>(0)).map_err(sql)?,"observation_count":s.observations,"used_record_count":s.used,"target_used_record_count":s.target_used,"invalid_asset_count":s.invalid_assets,"skips":{"count":s.skipped,"missing_target":s.missing_target,"missing_ticks":s.missing_price,"first_time":optional_iso(s.skip_first)?,"last_time":optional_iso(s.skip_last)?},"target_skips":{"count":s.target_skipped,"missing_target":s.target_missing_target,"missing_ticks":s.target_missing_price,"first_time":optional_iso(s.target_skip_first)?,"last_time":optional_iso(s.target_skip_last)?},"sizing_fallback_count":s.fallback_count,"points":points,"bindings":[],"calendar":null});
    crate::calendar::enrich(c, id, &mut result).map_err(|e| failure("ANALYSIS_CALENDAR", e))?;
    let records = records(c, &s, None)?;
    let mut days = BTreeMap::<String, Summary>::new();
    for r in &records {
        for t in &r.trades {
            days.entry(t.day.clone())
                .or_insert_with(Summary::empty)
                .push(t);
        }
    }
    let daily = days
        .into_iter()
        .map(|(day, s)| (day, s.output()))
        .collect::<serde_json::Map<_, _>>();
    let (events, count) = events(c, &s)?;
    let dates = points
        .iter()
        .enumerate()
        .filter_map(|(i, p)| p["date"].as_str().map(|d| (d.to_owned(), i)))
        .collect::<BTreeMap<_, _>>();
    let mut contributions=store::rows(c,"SELECT day,symbol,product,currency,gross_pnl,fee,fee_missing,issues,trades FROM analysis_contribution_day WHERE account_id=?1 AND range_key=?2 ORDER BY day,symbol",&[&id,&key])?;
    for row in &mut contributions {
        let day = row.as_object_mut().unwrap().remove("day").unwrap();
        row["idx"] = json!(dates
            .get(day.as_str().unwrap())
            .copied()
            .ok_or_else(|| failure("ANALYSIS_STATE", "contribution has no daily point"))?);
    }
    Ok(
        json!({"schema_version":1,"account_id":id,"version":version,"range":key,"computed_at":iso(s.computed_at)?,"data_until":optional_iso(s.last_time)?,"result":result,"daily_costs":daily,"events":events,"event_count":count,"execution_index":records.iter().map(execution_summary).collect::<Vec<_>>(),"contributions":contributions}),
    )
}
fn trade_json(t: &Trade, id: i64, index: usize, execution: &Value) -> Value {
    json!({"record_id":id,"execution_id":execution,"trade_id":index,"symbol":t.symbol,"time":ms(t.time),"day":t.day,"timeEstimated":t.estimated,"side":t.side,"quantity":t.quantity,"price":t.price,"reference":t.reference,"referenceSource":t.reference_source,"value":t.value,"cost":t.cost,"lossBp":t.loss_bp,"fee":t.fee,"feeCurrency":t.fee_currency})
}
fn transaction_rows(c: &Connection, r: &Record) -> Result<Vec<Value>, AnalysisError> {
    crate::fact_transactions::TransactionReader::new(c)
        .map_err(sql)?
        .read(r.head["id"].as_i64().unwrap(), &r.trades)
}
fn execution_row(c: &Connection, r: &Record, trades: &[Trade]) -> Result<Value, AnalysisError> {
    let id = r.head["id"].as_i64().unwrap();
    let mut query=c.prepare("SELECT map_key,node_kind,status,error,outcome,outcome_reason,reason_code,memory FROM execution_symbol_result WHERE record_id=?1 ORDER BY ordinal").map_err(sql)?;
    let mut raw = query.query([id]).map_err(sql)?;
    let mut symbols = serde_json::Map::new();
    while let Some(row) = raw.next().map_err(sql)? {
        symbols.insert(
            row.get::<_, String>("map_key").map_err(sql)?,
            Value::Object(crate::executions::symbol_from_row(row)?),
        );
    }
    let positions=crate::fact_positions::PositionReader::new(c).map_err(sql)?.read(id).map_err(|e|failure("ANALYSIS_FACTS",e))?.map(|v|v.into_iter().map(|(symbol,quantity,direction)|json!({"symbol":symbol,"quantity":quantity,"direction":direction})).collect::<Vec<_>>());
    let attempts = crate::fact_attempts::AttemptReader::new(c)
        .map_err(sql)?
        .read(id)?;
    Ok(
        json!({"key":id.to_string(),"record":{"id":id,"execution_id":r.head["execution_id"],"created_at":r.head["created_at"],"is_success":r.head["is_success"],"raw_result":{"status":r.head["display_status"],"task_status":r.head["task_status"],"error":r.head["display_error"],"outcome":r.head["display_outcome"],"outcome_reason":r.head["display_outcome_reason"],"reason_code":r.head["reason_code"],"msg":r.head["msg"],"execution_kind":r.head["execution_kind"],"symbol_results":symbols}},"noop":r.noop,"durationSec":r.head["execution_time"],"symbolCount":symbols.len(),"tradeCount":trades.len(),"positionCount":r.positions,"positions":positions,"transactions":transaction_rows(c,r)?,"attempts":attempts,"reason":r.head["display_error"],"summary":summary(trades).output()}),
    )
}
fn cost_execution(r: &Record, trades: &[Trade]) -> Value {
    let symbols = trades
        .iter()
        .map(|t| t.symbol.as_str())
        .collect::<BTreeSet<_>>();
    json!({
        "key":r.head["id"].to_string(),
        "record":{"id":r.head["id"],"execution_id":r.head["execution_id"],"created_at":r.head["created_at"],"is_success":r.head["is_success"],
            "raw_result":{"status":r.head["display_status"],"task_status":r.head["task_status"],"error":r.head["display_error"],"outcome":r.head["display_outcome"],"outcome_reason":r.head["display_outcome_reason"],"reason_code":r.head["reason_code"],"execution_kind":r.head["execution_kind"]}},
        "noop":r.noop,"durationSec":r.head["execution_time"],"symbolCount":symbols.len(),"symbols":symbols,"tradeCount":trades.len(),"positionCount":r.positions,"reason":r.head["display_error"],"summary":summary(trades).output()
    })
}
fn table(values: &[Value], columns: &[&str]) -> Value {
    json!({"columns":columns,"rows":values.iter().map(|v|columns.iter().map(|n|v[*n].clone()).collect::<Vec<_>>()).collect::<Vec<_>>()})
}
pub(crate) fn detail(
    c: &Connection,
    id: i64,
    version: &str,
    key: &str,
    record: i64,
    limit: usize,
    offset: usize,
) -> Result<Value, AnalysisError> {
    let s = guard(c, id, version, key)?;
    let mut found = records(c, &s, Some(record))?;
    let r = found
        .pop()
        .ok_or_else(|| failure("EXECUTION_NOT_FOUND", "执行不属于所选区间"))?;
    let row = execution_row(c, &r, &r.trades)?;
    let mut header = json!({"record_id":record,"time":ms(r.time),"success":r.head["is_success"]==1,"noop":r.noop,"key":record.to_string(),"execution_id":r.head["execution_id"],"created_at":r.head["created_at"],"is_success":r.head["is_success"],"duration_sec":r.head["execution_time"],"trade_count":r.trades.len(),"symbol_count":row["symbolCount"],"position_count":r.positions,"positions_present":r.positions.is_some(),"reason":r.head["display_error"],"raw_present":255,"has_symbol_results":true});
    for field in [
        "status",
        "task_status",
        "error",
        "outcome",
        "outcome_reason",
        "reason_code",
        "msg",
        "execution_kind",
    ] {
        header[format!("raw_{field}")] = row["record"]["raw_result"][field].clone();
    }
    let positions = row["positions"].as_array().cloned().unwrap_or_default();
    let attempts = row["attempts"].as_array().cloned().unwrap_or_default();
    let symbols = row["record"]["raw_result"]["symbol_results"]
        .as_object()
        .unwrap()
        .iter()
        .map(|(symbol, v)| {
            let mut v = v.clone();
            v["symbol"] = json!(symbol);
            v["present"] = json!(31);
            v["memory_message"] = Value::Null;
            v["memory_reason_code"] = Value::Null;
            v
        })
        .collect::<Vec<_>>();
    let mut transactions = Vec::new();
    let mut fees = Vec::new();
    for (idx, t) in row["transactions"].as_array().unwrap().iter().enumerate() {
        let mut value = t.clone();
        value["idx"] = json!(idx);
        value["reference_source"] = t["referenceSource"].clone();
        value["end_time"] = t["endTime"].clone();
        value["time_estimated"] = t["timeEstimated"].clone();
        for (snake, camel) in [
            ("value", "value"),
            ("cost", "cost"),
            ("loss_bp", "lossBp"),
            ("coverage", "coverage"),
            ("covered", "covered"),
            ("count", "count"),
            ("amount_complete", "amountComplete"),
            ("fee_covered", "feeCovered"),
        ] {
            value[snake] = t["summary"][camel].clone();
        }
        for (currency, amount) in t["summary"]["fees"].as_object().unwrap() {
            if idx >= offset && idx < offset + limit {
                fees.push(json!({"transaction_idx":idx,"currency":currency,"value":amount}));
            }
        }
        transactions.push(value);
    }
    let mut next = serde_json::Map::new();
    let mut tables = serde_json::Map::new();
    for (name, values, columns) in [
        (
            "positions",
            positions,
            vec!["idx", "symbol", "quantity", "direction"],
        ),
        (
            "attempts",
            attempts,
            vec!["idx", "symbol", "planned", "filled", "reason"],
        ),
        (
            "symbols",
            symbols,
            vec![
                "symbol",
                "present",
                "status",
                "error",
                "outcome",
                "outcome_reason",
                "reason_code",
                "memory_message",
                "memory_reason_code",
            ],
        ),
        (
            "transactions",
            transactions.clone(),
            vec![
                "idx",
                "symbol",
                "side",
                "quantity",
                "price",
                "reference",
                "reference_source",
                "time",
                "end_time",
                "time_estimated",
                "before",
                "target",
                "value",
                "cost",
                "loss_bp",
                "coverage",
                "covered",
                "count",
                "amount_complete",
                "fee_covered",
            ],
        ),
    ] {
        next.insert(
            name.into(),
            if values.len() > offset + limit {
                json!(offset + limit)
            } else {
                Value::Null
            },
        );
        let values = values
            .into_iter()
            .enumerate()
            .skip(offset)
            .take(limit)
            .map(|(idx, mut v)| {
                v["idx"] = json!(idx);
                v
            })
            .collect::<Vec<_>>();
        tables.insert(name.into(), table(&values, &columns));
    }
    tables.insert(
        "transaction_fees".into(),
        table(&fees, &["transaction_idx", "currency", "value"]),
    );
    let more = next.values().filter_map(Value::as_u64).min();
    Ok(
        json!({"schema_version":1,"version":version,"record_id":record,"header":header,"summary":snake_summary(&summary(&r.trades)),"tables":tables,"next_offsets":next,"next_offset":more,"total":transactions.len()}),
    )
}

fn cursor(request: &Value, version: &str) -> Result<(String, usize), AnalysisError> {
    use sha2::{Digest, Sha256};
    let mut fields = request
        .as_object()
        .cloned()
        .ok_or_else(|| failure("INVALID_REQUEST", "expected query object"))?;
    fields.remove("cursor");
    fields.remove("limit");
    let ordered = fields.into_iter().collect::<BTreeMap<_, _>>();
    let scope = format!(
        "{:x}",
        Sha256::digest(serde_json::to_vec(&ordered).unwrap())
    );
    let Some(token) = request["cursor"].as_str().filter(|s| !s.is_empty()) else {
        return Ok((scope, 0));
    };
    let bytes = URL_SAFE_NO_PAD
        .decode(token)
        .map_err(|_| failure("INVALID_REQUEST", "invalid cursor"))?;
    let value: Value =
        serde_json::from_slice(&bytes).map_err(|_| failure("INVALID_REQUEST", "invalid cursor"))?;
    if value["version"] != version || value["scope"] != scope {
        return Err(failure("ANALYSIS_VERSION_CHANGED", "成本筛选或版本已变化"));
    }
    let offset = value["offset"]
        .as_u64()
        .and_then(|v| usize::try_from(v).ok())
        .ok_or_else(|| failure("INVALID_REQUEST", "invalid cursor offset"))?;
    Ok((scope, offset))
}
fn matches(t: &Trade, q: &Value, window: Option<(i64, i64)>) -> bool {
    if q["day"].as_str().is_some_and(|d| t.day != d) {
        return false;
    }
    if let Some((start, end)) = window {
        if t.time <= start || t.time > end {
            return false;
        }
    }
    if q["symbol"].as_str().is_some_and(|s| t.symbol != s)
        || q["side"].as_str().is_some_and(|s| t.side != s)
    {
        return false;
    }
    if q["symbol_search"]
        .as_str()
        .is_some_and(|s| !t.symbol.to_lowercase().contains(&s.to_lowercase()))
    {
        return false;
    }
    true
}

fn selected_records(
    c: &Connection,
    s: &State,
    q: &Value,
) -> Result<Vec<(Record, Vec<Trade>)>, AnalysisError> {
    let mut selected = Vec::new();
    let window = q["start"]
        .as_f64()
        .map(|start| -> Result<(i64, i64), AnalysisError> {
            let end = q["end"]
                .as_f64()
                .ok_or_else(|| failure("INVALID_REQUEST", "end is required"))?;
            let convert = |value: f64| -> Result<i64, AnalysisError> {
                let us = value * 1000.0;
                if !us.is_finite() || us < i64::MIN as f64 || us >= i64::MAX as f64 {
                    return Err(failure("INVALID_REQUEST", "invalid millisecond boundary"));
                }
                Ok(us.round() as i64)
            };
            Ok((convert(start)?, convert(end)?))
        })
        .transpose()?;
    for r in records(c, s, q["record_id"].as_i64())? {
        let filtered = r
            .trades
            .iter()
            .filter(|t| matches(t, q, window))
            .cloned()
            .collect::<Vec<_>>();
        let symbol_scope =
            q["symbol"].is_string() || q["symbol_search"].is_string() || q["side"].is_string();
        let window = if let Some(day) = q["day"].as_str() {
            crate::time_us::local_datetime(r.time)
                .map(|t| t.date().to_string() == day)
                .unwrap_or(false)
        } else {
            window.is_none_or(|(start, end)| r.time > start && r.time <= end)
        };
        if !filtered.is_empty() || !symbol_scope && window {
            selected.push((r, filtered));
        }
    }
    Ok(selected)
}
fn symbol_costs(selected: &[(Record, Vec<Trade>)]) -> Vec<Value> {
    let mut groups = BTreeMap::<String, (Summary, i64, f64, f64, bool)>::new();
    for (_, trades) in selected {
        for t in trades {
            let group = groups
                .entry(t.symbol.clone())
                .or_insert_with(|| (Summary::empty(), t.time, 0.0, 0.0, false));
            group.0.push(t);
            group.1 = group.1.max(t.time);
            match (t.quantity, t.side.as_str()) {
                (Some(q), "buy") => group.2 += q,
                (Some(q), "sell") => group.3 += q,
                (None, _) => group.4 = true,
                _ => {}
            }
        }
    }
    groups.into_iter().map(|(symbol,(summary,time,buy,sell,incomplete))|json!({"symbol":symbol,"lastTime":ms(time),"buy":buy,"sell":sell,"quantityIncomplete":incomplete,"summary":summary.output()})).collect()
}
fn trade_costs(selected: &[(Record, Vec<Trade>)]) -> Vec<Value> {
    selected
        .iter()
        .flat_map(|(r, trades)| {
            trades.iter().enumerate().map(move |(idx, t)| {
                trade_json(
                    t,
                    r.head["id"].as_i64().unwrap(),
                    idx,
                    &r.head["execution_id"],
                )
            })
        })
        .collect()
}
fn cost_order(value: &Value, dimension: &str, sort: &str) -> f64 {
    let n = if sort == "cost" {
        if dimension == "symbol" {
            &value["summary"]["cost"]
        } else {
            &value["cost"]
        }
    } else if dimension == "symbol" {
        &value["lastTime"]
    } else {
        &value["time"]
    };
    n.as_f64().unwrap_or(f64::NEG_INFINITY)
}
pub(crate) fn costs(
    c: &Connection,
    id: i64,
    version: &str,
    key: &str,
    q: &Value,
) -> Result<Value, AnalysisError> {
    let s = guard(c, id, version, key)?;
    let limit = q["limit"].as_u64().unwrap_or(20) as usize;
    if !(1..=100).contains(&limit) {
        return Err(failure(
            "INVALID_REQUEST",
            "limit must be between 1 and 100",
        ));
    }
    let dimension = q["dimension"].as_str().unwrap_or("execution");
    if !["execution", "symbol", "trade", "summary"].contains(&dimension) {
        return Err(failure("INVALID_REQUEST", "invalid cost dimension"));
    }
    let sort = q["sort"].as_str().unwrap_or("time");
    let (scope, offset) = cursor(q, version)?;
    let mut selected = selected_records(c, &s, q)?;
    let mut total = Summary::empty();
    let mut successful = 0;
    let mut noop = 0;
    for (r, trades) in &selected {
        successful += i64::from(r.head["is_success"] == 1);
        noop += i64::from(r.noop);
        for t in trades {
            total.push(t);
        }
    }
    let (count, data) = if dimension == "execution" {
        selected.sort_by(|(a, at), (b, bt)| {
            (if sort == "cost" {
                summary(bt)
                    .cost
                    .unwrap_or(f64::NEG_INFINITY)
                    .total_cmp(&summary(at).cost.unwrap_or(f64::NEG_INFINITY))
            } else {
                b.time.cmp(&a.time)
            })
            .then_with(|| b.head["id"].as_i64().cmp(&a.head["id"].as_i64()))
        });
        let count = selected.len();
        let data: Vec<Value> = selected
            .into_iter()
            .skip(offset)
            .take(limit)
            .map(|(r, trades)| cost_execution(&r, &trades))
            .collect();
        (count, data)
    } else {
        let mut output = match dimension {
            "symbol" => symbol_costs(&selected),
            "trade" => trade_costs(&selected),
            _ => Vec::new(),
        };
        output.sort_by(|a, b| {
            cost_order(b, dimension, sort)
                .total_cmp(&cost_order(a, dimension, sort))
                .then_with(|| b.to_string().cmp(&a.to_string()))
        });
        (
            output.len(),
            output.into_iter().skip(offset).take(limit).collect(),
        )
    };
    let next = (offset.saturating_add(limit) < count).then(|| {
        URL_SAFE_NO_PAD
            .encode(json!({"version":version,"scope":scope,"offset":offset+limit}).to_string())
    });
    Ok(
        json!({"version":version,"data_until":optional_iso(s.last_time)?,"summary":total.output(),"successful":successful,"noop":noop,"count":count,"data":data,"next_cursor":next}),
    )
}
