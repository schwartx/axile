//! Calculate from one read snapshot and insert directly into disk-backed staging.

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

pub(crate) fn numeric_time(value: &str) -> Result<f64, AnalysisError> {
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

#[derive(Clone, Debug)]
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
    ranges: &[(&str, f64, f64)],
    target: &Connection,
) -> Result<Vec<usize>, AnalysisError> {
    let mut results: Vec<(usize, Vec<Event>)> = ranges
        .iter()
        .map(|_| (0, Vec::with_capacity(101)))
        .collect();
    let mut consume = |event: Event| {
        for ((_, start, end), (count, newest)) in ranges.iter().zip(&mut results) {
            if event.numeric_time < *start || event.numeric_time > *end {
                continue;
            }
            *count += 1;
            // Equal-time events retain the existing binding, skip, execution order.
            let index = newest.partition_point(|other| other.numeric_time >= event.numeric_time);
            if index < 100 {
                newest.insert(index, event.clone());
                if newest.len() > 100 {
                    newest.pop();
                }
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
            if !ranges
                .iter()
                .any(|(_, start, end)| numeric_time >= *start && numeric_time <= *end)
            {
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
    let mut counts = Vec::with_capacity(ranges.len());
    for ((range, _, _), (count, newest)) in ranges.iter().zip(results) {
        counts.push(count);
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
    }
    Ok(counts)
}

fn range_rows(
    target: &Connection,
    key: &str,
    mut performance: Value,
    event_count: usize,
) -> Result<(), AnalysisError> {
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
event_count,
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
    crate::fact_cost_rows::trades(source, task.account_id, target, &bounds)?;
    let counts = events(source, task.account_id, &bounds, target)?;
    let settings = ranges["all"]["settings"].clone();
    let data_until = ranges["all"]["end"].clone();
    for ((key, _, _), count) in bounds.iter().zip(counts) {
        range_rows(target, key, ranges[*key].take(), count)?;
    }
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
        let count = events(
            &connection,
            2,
            &[
                ("all", time, time),
                ("90", time, time),
                ("30", time + 1.0, time + 2.0),
            ],
            &connection,
        )
        .unwrap();
        assert_eq!(count, vec![152, 152, 0]);
        let rows = connection.prepare("SELECT tag,text,execution_id FROM native_stage.performance_result_event WHERE range_key='all' ORDER BY idx").unwrap().query_map([],|row| Ok((row.get::<_,String>(0)?,row.get::<_,String>(1)?,row.get::<_,Option<String>>(2)?))).unwrap().collect::<rusqlite::Result<Vec<_>>>().unwrap();
        assert_eq!(rows.len(), 100);
        assert_eq!(rows[0], ("换绑".into(), "组合 #7".into(), None));
        assert_eq!(rows[1], ("跳过".into(), "执行中".into(), None));
        assert_eq!(rows[99].2.as_deref(), Some("execution-97"));
        assert_eq!(connection.query_row("SELECT COUNT(*) FROM native_stage.performance_result_event WHERE range_key='90'", [], |row| row.get::<_,i64>(0)).unwrap(), 100);
        connection
            .execute("DELETE FROM native_stage.performance_result_event", [])
            .unwrap();
        assert_eq!(
            events(
                &connection,
                2,
                &[("all", time + 1.0, time + 2.0)],
                &connection
            )
            .unwrap(),
            vec![0]
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
