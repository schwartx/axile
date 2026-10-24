//! Calculate from one read snapshot and insert directly into disk-backed staging.

use std::collections::BTreeMap;

use rusqlite::{params, Connection};
use serde_json::{json, Value};

use crate::error::AnalysisError;
use crate::native_publication::task::Task;

pub(crate) fn sql_error(error: rusqlite::Error) -> AnalysisError {
    AnalysisError::caused_by("ANALYSIS_READ_ERROR", error)
}

pub(crate) fn stage_error(error: rusqlite::Error) -> AnalysisError {
    AnalysisError::caused_by("PUBLICATION_STAGE", error)
}

// Keep the former JSON number semantics when binding calculated floats directly.
pub(crate) fn finite(value: Option<f64>) -> Option<f64> {
    value.filter(|value| value.is_finite())
}

fn numeric_time(value: &str) -> Result<f64, AnalysisError> {
    crate::calendar::timestamp_ms(value).map_err(|_| {
        AnalysisError::new(
            "INVALID_RESULT_TIME",
            format!("INVALID_RESULT_TIME:{value:?}"),
        )
    })
}

fn take_array(value: &mut Value, field: &str) -> Result<Vec<Value>, AnalysisError> {
    match value.get_mut(field).map(Value::take).unwrap_or(Value::Null) {
        Value::Array(values) => Ok(values),
        Value::Null => Ok(Vec::new()),
        _ => Err(AnalysisError::new(
            "INVALID_RESULT_ROW",
            format!("{field} must be an array"),
        )),
    }
}

#[derive(Debug)]
struct Event {
    time: String,
    numeric_time: f64,
    tag: String,
    text: String,
    execution_id: Option<String>,
}

/// Retain the newest hundred events while counting every matching event.
fn events(
    connection: &Connection,
    account: i64,
    range: &str,
    start: f64,
    end: f64,
    target: &Connection,
) -> Result<usize, AnalysisError> {
    let mut count = 0;
    let mut newest: Vec<Event> = Vec::with_capacity(101);
    let mut consume = |event: Event| {
        if event.numeric_time < start || event.numeric_time > end {
            return;
        }
        count += 1;
        // Equal-time events retain the existing binding, skip, execution order.
        let index = newest.partition_point(|other| other.numeric_time >= event.numeric_time);
        if index < 100 {
            newest.insert(index, event);
            if newest.len() > 100 {
                newest.pop();
            }
        }
    };
    {
        let mut statement = connection.prepare(
            "SELECT created_at,portfolio_id FROM portfolioaccount WHERE account_id=?1 ORDER BY created_at,id"
        ).map_err(sql_error)?;
        let mut cursor = statement.query([account]).map_err(sql_error)?;
        while let Some(row) = cursor.next().map_err(sql_error)? {
            let time: String = row.get(0).map_err(sql_error)?;
            let portfolio: Option<i64> = row.get(1).map_err(sql_error)?;
            consume(Event {
                numeric_time: numeric_time(&time)?,
                time,
                tag: "换绑".to_owned(),
                text: portfolio
                    .map(|id| format!("组合 #{id}"))
                    .unwrap_or_else(|| "解绑组合".to_owned()),
                execution_id: None,
            });
        }
    }
    {
        let mut statement = connection.prepare(
            "SELECT triggered_at,reason_code FROM schedule_skip WHERE account_id=?1 ORDER BY triggered_at,id"
        ).map_err(sql_error)?;
        let mut cursor = statement.query([account]).map_err(sql_error)?;
        while let Some(row) = cursor.next().map_err(sql_error)? {
            let time: String = row.get(0).map_err(sql_error)?;
            let reason: String = row.get(1).map_err(sql_error)?;
            consume(Event {
                numeric_time: numeric_time(&time)?,
                time,
                tag: "跳过".to_owned(),
                text: if reason == "BUSY" {
                    "执行中"
                } else {
                    "休市"
                }
                .to_owned(),
                execution_id: None,
            });
        }
    }
    {
        let mut statement = connection.prepare(
            "SELECT id,created_at,display_status,task_status,reason_code,display_error,execution_id FROM executerecord
             WHERE account_id=?1 AND is_success IS NOT 1 ORDER BY id"
        ).map_err(sql_error)?;
        let mut labels = crate::fact_events::EventReader::new(connection).map_err(sql_error)?;
        let mut cursor = statement.query([account]).map_err(sql_error)?;
        while let Some(row) = cursor.next().map_err(sql_error)? {
            let time: String = row.get(1).map_err(sql_error)?;
            let numeric_time = numeric_time(&time)?;
            if numeric_time < start || numeric_time > end {
                continue;
            }
            let status: Option<String> = row.get(2).map_err(sql_error)?;
            let task: Option<String> = row.get(3).map_err(sql_error)?;
            let code: Option<String> = row.get(4).map_err(sql_error)?;
            let label = labels
                .label(
                    row.get(0).map_err(sql_error)?,
                    status.as_deref(),
                    task.as_deref(),
                    code.as_deref(),
                )
                .map_err(sql_error)?;
            let reason: Option<String> = row.get(5).map_err(sql_error)?;
            let text = if matches!(status.as_deref(), Some("FAILED" | "BLOCKED" | "PARTIAL")) {
                reason
                    .filter(|reason| !reason.is_empty())
                    .map(|reason| format!("{label} · {reason}"))
                    .unwrap_or_else(|| label.to_owned())
            } else {
                label.to_owned()
            };
            consume(Event {
                time,
                numeric_time,
                tag: label.to_owned(),
                text: text.chars().take(500).collect(),
                execution_id: row.get(6).map_err(sql_error)?,
            });
        }
    }
    let mut insert = target
        .prepare_cached(
            r#"INSERT INTO native_stage.performance_result_event (
                range_key, idx, time, tag, text, execution_id
            ) VALUES (
                ?1, ?2, ?3, ?4, ?5, ?6
            )"#,
        )
        .map_err(stage_error)?;
    for (index, event) in newest.into_iter().enumerate() {
        insert
            .execute(params![
                range,
                index,
                event.time,
                event.tag,
                event.text,
                event.execution_id
            ])
            .map_err(stage_error)?;
    }
    Ok(count)
}

fn daily_costs(
    connection: &Connection,
    account: i64,
    ranges: &[(&str, f64, f64)],
    target: &Connection,
) -> Result<(), AnalysisError> {
    let mut summaries: Vec<BTreeMap<String, crate::cost_summary::Summary>> =
        ranges.iter().map(|_| BTreeMap::new()).collect();
    let mut ticks = crate::fact_ticks::TickReader::new(connection).map_err(sql_error)?;
    let mut trades = crate::fact_trades::TradeReader::new(connection).map_err(sql_error)?;
    let mut statement = connection
        .prepare("SELECT id,created_at FROM executerecord WHERE account_id=?1 ORDER BY id")
        .map_err(sql_error)?;
    let mut cursor = statement.query([account]).map_err(sql_error)?;
    while let Some(row) = cursor.next().map_err(sql_error)? {
        let id: i64 = row.get(0).map_err(sql_error)?;
        let created_at: String = row.get(1).map_err(sql_error)?;
        let time = numeric_time(&created_at)?;
        if !ranges
            .iter()
            .any(|(_, start, end)| time >= *start && time <= *end)
        {
            continue;
        }
        let prices = ticks.read(id).map_err(sql_error)?;
        for trade in trades.read(id, &created_at, &prices)? {
            for ((_, start, end), days) in ranges.iter().zip(&mut summaries) {
                if time < *start || time > *end {
                    continue;
                }
                days.entry(trade.day.clone())
                    .or_insert_with(crate::cost_summary::Summary::empty)
                    .push(&trade);
            }
        }
    }
    let mut cost = target
        .prepare_cached(
            r#"INSERT INTO native_stage.performance_result_daily_cost (
                range_key, day, value, cost, loss_bp, coverage,
                covered_value, covered, count, fee_covered, estimated, amount_complete,
                present
            ) VALUES (
                ?1, ?2, ?3, ?4, ?5, ?6,
                NULL, ?7, ?8, ?9, NULL, ?10,
                255
            )"#,
        )
        .map_err(stage_error)?;
    let mut fee = target.prepare_cached(
        "INSERT INTO native_stage.performance_result_daily_fee(range_key,day,currency,value) VALUES(?1,?2,?3,?4)"
    ).map_err(stage_error)?;
    for ((range, _, _), days) in ranges.iter().zip(&summaries) {
        for (day, value) in days {
            cost.execute(params![
                range,
                day,
                finite(value.value),
                finite(value.cost),
                finite(value.loss_bp()),
                finite(value.coverage()),
                value.covered,
                value.count,
                value.fee_count,
                value.amount_complete(),
            ])
            .map_err(stage_error)?;
            for (currency, (value, _)) in &value.fees {
                fee.execute(params![range, day, currency, finite(Some(*value))])
                    .map_err(stage_error)?;
            }
        }
    }
    Ok(())
}

fn range_rows(
    source: &Connection,
    target: &Connection,
    account: i64,
    key: &str,
    mut performance: Value,
) -> Result<(), AnalysisError> {
    let start = performance["baseline"]
        .as_str()
        .map(numeric_time)
        .transpose()?
        .unwrap_or(0.0);
    let end = performance["end"]
        .as_str()
        .map(numeric_time)
        .transpose()?
        .unwrap_or(-1.0);
    let mut insert = target.prepare_cached(
        r#"INSERT INTO native_stage.performance_result_point (
                range_key, idx, date, observed_at, record_id, execution_id,
                account_return, account_equity, portfolio_return, portfolio_daily_return, account_daily_return, target_portfolio_return,
                target_portfolio_daily_return, difference, sizing_fallback
            ) VALUES (
                ?1, ?2, ?3, ?4, ?5, ?6,
                ?7, ?8, ?9, ?10, ?11, ?12,
                ?13, ?14, ?15
            )"#
    ).map_err(stage_error)?;
    for (index, value) in take_array(&mut performance, "points")?
        .into_iter()
        .enumerate()
    {
        insert
            .execute(params![
                key,
                index,
                value["date"].as_str(),
                value["observed_at"].as_str(),
                value["record_id"].as_i64(),
                value["execution_id"].as_str(),
                value["account_return"].as_f64(),
                value["account_equity"].as_f64(),
                value["portfolio_return"].as_f64(),
                value["portfolio_daily_return"].as_f64(),
                value["account_daily_return"].as_f64(),
                value["target_portfolio_return"].as_f64(),
                value["target_portfolio_daily_return"].as_f64(),
                value["difference"].as_f64(),
                value["sizing_fallback"].as_bool(),
            ])
            .map_err(stage_error)?;
    }
    let mut insert = target.prepare_cached(
        "INSERT INTO native_stage.performance_result_fallback(range_key,idx,start,end,count) VALUES(?1,?2,?3,?4,?5)"
    ).map_err(stage_error)?;
    for (index, value) in take_array(&mut performance, "sizing_fallback_ranges")?
        .into_iter()
        .enumerate()
    {
        insert
            .execute(params![
                key,
                index,
                value["start"].as_str(),
                value["end"].as_str(),
                value["count"].as_i64()
            ])
            .map_err(stage_error)?;
    }
    let mut insert = target.prepare_cached(
        "INSERT INTO native_stage.performance_result_binding(range_key,idx,time,portfolio_id) VALUES(?1,?2,?3,?4)"
    ).map_err(stage_error)?;
    for (index, value) in take_array(&mut performance, "bindings")?
        .into_iter()
        .enumerate()
    {
        insert
            .execute(params![
                key,
                index,
                value["time"].as_str(),
                value["portfolio_id"].as_i64()
            ])
            .map_err(stage_error)?;
    }
    let mut insert = target
        .prepare_cached(
            r#"INSERT INTO native_stage.performance_result_skip (
                range_key, kind, count, missing_target, missing_ticks, first_time,
                last_time
            ) VALUES (
                ?1, ?2, ?3, ?4, ?5, ?6,
                ?7
            )"#,
        )
        .map_err(stage_error)?;
    for (field, kind) in [("skips", "portfolio"), ("target_skips", "target")] {
        let value = performance
            .get_mut(field)
            .map(Value::take)
            .unwrap_or(Value::Null);
        if !value.is_null() {
            insert
                .execute(params![
                    key,
                    kind,
                    value["count"].as_i64(),
                    value["missing_target"].as_i64(),
                    value["missing_ticks"].as_i64(),
                    value["first_time"].as_str(),
                    value["last_time"].as_str(),
                ])
                .map_err(stage_error)?;
        }
    }
    let mut calendar = performance["calendar"].take();
    let mut insert = target.prepare_cached(
        "INSERT INTO native_stage.performance_result_calendar_range(range_key,kind,idx,start,end) VALUES(?1,?2,?3,?4,?5)"
    ).map_err(stage_error)?;
    for (field, kind) in [
        ("closed_ranges", "closed"),
        ("unavailable_ranges", "unavailable"),
    ] {
        for (index, value) in take_array(&mut calendar, field)?.into_iter().enumerate() {
            insert
                .execute(params![
                    key,
                    kind,
                    index,
                    value["start"].as_str(),
                    value["end"].as_str()
                ])
                .map_err(stage_error)?;
        }
    }
    let count = events(source, account, key, start, end, target)?;
    target.execute(
        r#"INSERT INTO native_stage.performance_result_range (
                range_key, backtest_included, engine_version, baseline, end, record_count,
                observation_count, used_record_count, target_used_record_count, invalid_asset_count, sizing_fallback_count, calendar_status,
                calendar_id, calendar_label, event_count
            ) VALUES (
                ?1, ?2, ?3, ?4, ?5, ?6,
                ?7, ?8, ?9, ?10, ?11, ?12,
                ?13, ?14, ?15
            )"#,
        params![
key,
performance["backtest_included"].as_bool(),
performance["engine_version"].as_str(),
performance["baseline"].as_str(),
performance["end"].as_str(),
performance["record_count"].as_i64(),
performance["observation_count"].as_i64(),
performance["used_record_count"].as_i64(),
performance["target_used_record_count"].as_i64(),
performance["invalid_asset_count"].as_i64(),
performance["sizing_fallback_count"].as_i64(),
calendar["status"].as_str(),
calendar["calendar_id"].as_str(),
calendar["label"].as_str(),
count,
]
    ).map_err(stage_error)?;
    Ok(())
}

pub(crate) fn write(
    source: &Connection,
    target: &Connection,
    task: &Task,
) -> Result<Value, AnalysisError> {
    let calculation = crate::performance::PerformanceRequest {
        account_id: task.account_id,
        range: None,
        include_backtest: task.include_backtest,
        include_metadata: true,
        require_observation_facts: task.require_observation_facts,
        expected_source_version: crate::performance::SourceVersionCheck::Version(Some(
            task.source_version,
        )),
        backtest_fee_rate: task.fee_rate,
    };
    let mut ranges = crate::performance::performance(source, &calculation)?;
    let bounds = ["30", "90", "all"]
        .map(|key| {
            Ok((
                key,
                ranges[key]["baseline"]
                    .as_str()
                    .map(numeric_time)
                    .transpose()?
                    .unwrap_or(0.0),
                ranges[key]["end"]
                    .as_str()
                    .map(numeric_time)
                    .transpose()?
                    .unwrap_or(-1.0),
            ))
        })
        .into_iter()
        .collect::<Result<Vec<_>, AnalysisError>>()?;
    daily_costs(source, task.account_id, &bounds, target)?;
    let settings = ranges["all"]["settings"].clone();
    let data_until = ranges["all"]["end"].clone();
    for key in ["30", "90", "all"] {
        range_rows(source, target, task.account_id, key, ranges[key].take())?;
    }
    crate::fact_cost_rows::trades(source, task.account_id, target)?;
    Ok(
        json!({"account_id":task.account_id,"source_version":task.source_version,"settings":settings,"data_until":data_until}),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn direct_float_binding_keeps_unknown_and_nonfinite_values_null() {
        let connection = Connection::open_in_memory().unwrap();
        connection
            .execute_batch("CREATE TABLE amounts(value REAL)")
            .unwrap();
        for value in [
            None,
            Some(f64::NAN),
            Some(f64::INFINITY),
            Some(f64::NEG_INFINITY),
            Some(0.0),
            Some(-0.2),
        ] {
            connection
                .execute("INSERT INTO amounts VALUES(?1)", [finite(value)])
                .unwrap();
        }
        let values = connection
            .prepare("SELECT value FROM amounts ORDER BY rowid")
            .unwrap()
            .query_map([], |row| row.get::<_, Option<f64>>(0))
            .unwrap()
            .collect::<rusqlite::Result<Vec<_>>>()
            .unwrap();
        assert_eq!(values, vec![None, None, None, None, Some(0.0), Some(-0.2)]);
    }

    #[test]
    fn timestamp_normalizes_shanghai_and_explicit_offsets() {
        assert_eq!(
            numeric_time("2026-01-01T10:00:00.123456").unwrap(),
            numeric_time("2026-01-01T10:00:00.123456+08:00").unwrap()
        );
        assert_eq!(
            numeric_time("2026-01-01T02:00:00Z").unwrap(),
            numeric_time("2026-01-01 10:00:00").unwrap()
        );
        // Match datetime.timestamp()*1000 exactly at inclusive SQL boundaries.
        assert_eq!(
            numeric_time("2026-01-01T09:00:00.000003+08:00").unwrap(),
            1767229200000.0032
        );
        assert_eq!(
            numeric_time("2026-01-01T09:00:00.000003").unwrap(),
            1767229200000.0032
        );
        assert!(numeric_time("unknown").is_err());
    }

    fn scalar_fixture() -> Connection {
        let connection = Connection::open_in_memory().unwrap();
        connection.execute_batch(
            "CREATE TABLE executerecord(id INTEGER PRIMARY KEY,account_id INTEGER,created_at TEXT,display_status TEXT,task_status TEXT,reason_code TEXT,display_error TEXT,execution_id TEXT,is_success INTEGER);
             CREATE TABLE execution_symbol_result(record_id INTEGER,path TEXT,ordinal INTEGER,node_kind TEXT,status TEXT,reason_code TEXT);
             CREATE TABLE execution_order(record_id INTEGER,path TEXT);
             CREATE TABLE business_trade(record_id INTEGER,collection_path TEXT); CREATE TABLE execution_symbol_collection(record_id INTEGER,path TEXT,node_kind TEXT); CREATE TABLE execution_order_collection(record_id INTEGER,kind TEXT); CREATE TABLE business_trade_collection(record_id INTEGER,state TEXT,malformed_value TEXT);
             CREATE TABLE portfolioaccount(id INTEGER PRIMARY KEY,account_id INTEGER,created_at TEXT,portfolio_id INTEGER);
             CREATE TABLE schedule_skip(id INTEGER PRIMARY KEY,account_id INTEGER,triggered_at TEXT,reason_code TEXT);"
        ).unwrap();
        connection
    }

    #[test]
    fn daily_costs_filter_by_execution_time_and_keep_unknown_amounts() {
        let connection = Connection::open_in_memory().unwrap();
        connection.execute_batch(
            "CREATE TABLE executerecord(id INTEGER,account_id INTEGER,created_at TEXT);
             CREATE TABLE execution_symbol_result(record_id INTEGER,path TEXT,map_key TEXT,ordinal INTEGER);
             CREATE TABLE execution_symbol_tick(record_id INTEGER,path TEXT,map_key TEXT,ordinal INTEGER,bid_price REAL,ask_price REAL,last_price REAL,book_valid INTEGER);
             CREATE TABLE execution_symbol_sizing(record_id INTEGER,path TEXT,unit_multiplier REAL);
             CREATE TABLE business_trade_collection(record_id INTEGER,path TEXT,parent_path TEXT,state TEXT);
             CREATE TABLE business_trade(record_id INTEGER,collection_path TEXT,ordinal INTEGER,trade_time TEXT,trade_price REAL,trade_volume REAL,order_id TEXT,extra TEXT,fee_currency TEXT);
             CREATE TABLE execution_order(record_id INTEGER,path TEXT,order_id TEXT,ordinal INTEGER,direction TEXT);
             INSERT INTO executerecord VALUES(1,2,'2026-01-01T23:59:00'),(2,2,'2026-01-02T09:00:00');
             INSERT INTO execution_symbol_result VALUES(1,'/symbol_results/A','A',0),(2,'/symbol_results/A','A',0);
             INSERT INTO execution_symbol_tick VALUES(1,'/symbol_results/A/first_tick','A',0,98,98,98,1);
             INSERT INTO execution_symbol_sizing VALUES(1,'/symbol_results/A/sizing',1),(2,'/symbol_results/A/sizing',1);
             INSERT INTO business_trade_collection VALUES(1,'/symbol_results/A/trades','/symbol_results/A','array'),(2,'/symbol_results/A/trades','/symbol_results/A','array');
             INSERT INTO business_trade VALUES
              (1,'/symbol_results/A/trades',0,'2026-01-01T23:59:00',100,1,'1','{\"commission\":0,\"direction\":\"buy\"}','CNY'),
              (1,'/symbol_results/A/trades',1,'2026-01-02T00:01:00',NULL,1,'2','{\"commission\":-0.2}','USD'),
              (1,'/symbol_results/A/trades',2,'2026-01-02T00:02:00',NULL,1,'3','{\"commission\":0}','USD'),
              (2,'/symbol_results/A/trades',0,'2026-01-02T09:00:00',500,1,'4','{\"commission\":8}','CNY');"
        ).unwrap();
        let boundary = numeric_time("2026-01-01T23:59:00").unwrap();
        connection.execute_batch("ATTACH DATABASE '' AS native_stage; CREATE TABLE native_stage.performance_result_daily_cost(\"range_key\" TEXT,\"day\" TEXT,\"value\" FLOAT,\"cost\" FLOAT,\"loss_bp\" FLOAT,\"coverage\" FLOAT,\"covered_value\" FLOAT,\"covered\" INTEGER,\"count\" INTEGER,\"fee_covered\" INTEGER,\"estimated\" INTEGER,\"amount_complete\" BOOLEAN,\"present\" INTEGER);CREATE TABLE native_stage.performance_result_daily_fee(\"range_key\" TEXT,\"day\" TEXT,\"currency\" TEXT,\"value\" FLOAT);").unwrap();
        daily_costs(&connection, 2, &[("all", boundary, boundary)], &connection).unwrap();
        let costs = connection.prepare("SELECT day,value,cost,loss_bp,coverage,covered,count,fee_covered,amount_complete FROM native_stage.performance_result_daily_cost ORDER BY day").unwrap().query_map([],|row| Ok((row.get::<_,String>(0)?,row.get::<_,Option<f64>>(1)?,row.get::<_,Option<f64>>(2)?,row.get::<_,Option<f64>>(3)?,row.get::<_,Option<f64>>(4)?,row.get::<_,i64>(5)?,row.get::<_,i64>(6)?,row.get::<_,i64>(7)?,row.get::<_,bool>(8)?))).unwrap().collect::<rusqlite::Result<Vec<_>>>().unwrap();
        assert_eq!(
            costs,
            vec![
                (
                    "2026-01-01".into(),
                    Some(100.0),
                    Some(2.0),
                    Some(2.0 / 98.0 * 10000.0),
                    Some(1.0),
                    1,
                    1,
                    1,
                    true
                ),
                ("2026-01-02".into(), None, None, None, None, 0, 2, 2, false),
            ]
        );
        let fees = connection.prepare("SELECT day,currency,value FROM native_stage.performance_result_daily_fee ORDER BY day,currency").unwrap().query_map([],|row| Ok((row.get::<_,String>(0)?,row.get::<_,String>(1)?,row.get::<_,f64>(2)?))).unwrap().collect::<rusqlite::Result<Vec<_>>>().unwrap();
        assert_eq!(
            fees,
            vec![
                ("2026-01-01".into(), "CNY".into(), 0.0),
                ("2026-01-02".into(), "USD".into(), -0.2)
            ]
        );
    }

    #[test]
    fn newest_events_are_bounded_but_count_covers_the_full_range() {
        let connection = scalar_fixture();
        let time = numeric_time("2026-01-01T10:00:00").unwrap();
        connection
            .execute_batch(
                "INSERT INTO portfolioaccount VALUES(1,2,'2026-01-01T10:00:00',7);
             INSERT INTO schedule_skip VALUES(1,2,'2026-01-01T10:00:00','BUSY');",
            )
            .unwrap();
        for index in 0..150 {
            connection.execute(
                "INSERT INTO executerecord VALUES(?1,2,'2026-01-01T10:00:00','FAILED',NULL,NULL,?2,?3,0)",
                params![index, format!("failure-{index}"),format!("execution-{index}")],
            ).unwrap();
        }
        connection.execute_batch("ATTACH DATABASE '' AS native_stage; CREATE TABLE native_stage.performance_result_event(\"range_key\" TEXT,\"idx\" INTEGER,\"time\" TEXT,\"tag\" TEXT,\"text\" TEXT,\"execution_id\" TEXT);").unwrap();
        let count = events(&connection, 2, "all", time, time, &connection).unwrap();
        assert_eq!(count, 152);
        let rows = connection.prepare("SELECT tag,text,execution_id FROM native_stage.performance_result_event ORDER BY idx").unwrap().query_map([],|row| Ok((row.get::<_,String>(0)?,row.get::<_,String>(1)?,row.get::<_,Option<String>>(2)?))).unwrap().collect::<rusqlite::Result<Vec<_>>>().unwrap();
        assert_eq!(rows.len(), 100);
        assert_eq!(rows[0], ("换绑".into(), "组合 #7".into(), None));
        assert_eq!(rows[1], ("跳过".into(), "执行中".into(), None));
        assert_eq!(rows[99].2.as_deref(), Some("execution-97"));
        connection
            .execute("DELETE FROM native_stage.performance_result_event", [])
            .unwrap();
        assert_eq!(
            events(&connection, 2, "all", time + 1.0, time + 2.0, &connection).unwrap(),
            0
        );
        assert_eq!(
            connection
                .query_row(
                    "SELECT COUNT(*) FROM native_stage.performance_result_event",
                    [],
                    |row| row.get::<_, i64>(0)
                )
                .unwrap(),
            0
        );
    }
}
