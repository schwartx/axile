//! Bounded publication rows for native persistence and compatibility streaming.
//!
//! Rust keeps historical facts inside its read snapshot. Only scalar result
//! rows are emitted to a native database staging callback. The compatibility
//! command can still stream its original result rows to a Python publisher.

use std::collections::BTreeMap;

use chrono::{DateTime, NaiveDateTime};
use rusqlite::{params, Connection, OptionalExtension};
use serde_json::{json, Value};

const MAX_ROWS: usize = 256;
const MAX_ROW_BYTES: usize = 512 * 1024;
const POINT_COLUMNS: &[&str] = &[
    "date",
    "observed_at",
    "record_id",
    "execution_id",
    "account_return",
    "account_equity",
    "portfolio_return",
    "account_daily_return",
    "portfolio_daily_return",
    "target_portfolio_return",
    "target_portfolio_daily_return",
    "difference",
    "sizing_fallback",
];
const RANGE_COLUMNS: &[&str] = &[
    "backtest_included",
    "engine_version",
    "baseline",
    "end",
    "record_count",
    "observation_count",
    "used_record_count",
    "target_used_record_count",
    "invalid_asset_count",
    "sizing_fallback_count",
    "calendar_status",
    "calendar_id",
    "calendar_label",
    "event_count",
];

fn sql_error(error: rusqlite::Error) -> String {
    format!("ANALYSIS_READ_ERROR:{error}")
}

fn string_bytes(value: &str) -> usize {
    2 + value
        .bytes()
        .map(|byte| match byte {
            b'"' | b'\\' | b'\x08' | b'\x0c' | b'\n' | b'\r' | b'\t' => 2,
            0..=0x1f => 6,
            _ => 1,
        })
        .sum::<usize>()
}

/// Count JSON framing bytes without encoding or copying native result rows.
fn value_bytes(value: &Value) -> usize {
    match value {
        Value::Null => 4,
        Value::Bool(true) => 4,
        Value::Bool(false) => 5,
        Value::Number(number) => number.to_string().len(),
        Value::String(text) => string_bytes(text),
        Value::Array(values) => row_bytes(values),
        Value::Object(values) => {
            2 + values.len().saturating_sub(1)
                + values
                    .iter()
                    .map(|(key, value)| string_bytes(key) + 1 + value_bytes(value))
                    .sum::<usize>()
        }
    }
}

fn row_bytes(values: &[Value]) -> usize {
    2 + values.len().saturating_sub(1) + values.iter().map(value_bytes).sum::<usize>()
}

/// At most one bounded batch is retained, including when a consumer is slow.
pub(crate) fn rows(
    table: &str,
    columns: &[&str],
    values: impl IntoIterator<Item = Result<Vec<Value>, String>>,
    emit: &mut dyn FnMut(&Value) -> Result<(), String>,
) -> Result<(), String> {
    let mut pending = Vec::with_capacity(MAX_ROWS);
    let mut bytes = 0;
    for row in values {
        let row = row?;
        if row.len() != columns.len() {
            return Err(format!("INVALID_RESULT_ROW:{table} column count mismatch"));
        }
        let size = row_bytes(&row);
        if size > MAX_ROW_BYTES {
            return Err(format!(
                "RESULT_FRAME_TOO_LARGE:{table} contains an oversized row"
            ));
        }
        if !pending.is_empty() && (pending.len() == MAX_ROWS || bytes + size > MAX_ROW_BYTES) {
            emit(&json!({"type":"rows", "table":table, "columns":columns, "rows":pending}))?;
            pending.clear();
            bytes = 0;
        }
        bytes += size;
        pending.push(row);
    }
    if !pending.is_empty() {
        emit(&json!({"type":"rows", "table":table, "columns":columns, "rows":pending}))?;
    }
    Ok(())
}

fn array_rows(
    range: &str,
    performance: &mut Value,
    field: &str,
    table: &str,
    fields: &[&str],
    emit: &mut dyn FnMut(&Value) -> Result<(), String>,
) -> Result<(), String> {
    let values = performance
        .get_mut(field)
        .map(Value::take)
        .unwrap_or(Value::Null);
    let values = match values {
        Value::Array(values) => values,
        Value::Null => Vec::new(),
        _ => return Err(format!("INVALID_RESULT_ROW:{field} must be an array")),
    };
    let columns: Vec<&str> = ["range_key", "idx"]
        .into_iter()
        .chain(fields.iter().copied())
        .collect();
    rows(
        table,
        &columns,
        values.into_iter().enumerate().map(|(index, mut value)| {
            let mut row = vec![json!(range), json!(index)];
            row.extend(
                fields
                    .iter()
                    .map(|field| value.get_mut(field).map(Value::take).unwrap_or(Value::Null)),
            );
            Ok(row)
        }),
        emit,
    )
}

fn numeric_time(value: &str) -> Result<f64, String> {
    if let Ok(time) = DateTime::parse_from_rfc3339(value) {
        return Ok(time.timestamp_micros() as f64 / 1_000_000.0 * 1000.0);
    }
    for format in ["%Y-%m-%dT%H:%M:%S%.f", "%Y-%m-%d %H:%M:%S%.f"] {
        if let Ok(time) = NaiveDateTime::parse_from_str(value, format) {
            return Ok(
                (time.and_utc().timestamp_micros() - 28_800_000_000) as f64 / 1_000_000.0 * 1000.0,
            );
        }
    }
    Err(format!("INVALID_RESULT_TIME:{value:?}"))
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
    emit: &mut dyn FnMut(&Value) -> Result<(), String>,
) -> Result<usize, String> {
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
            "SELECT created_at,time,event_tag,event_text,execution_id FROM analysis_source_execution \
             WHERE account_id=?1 AND time>=?2 AND time<=?3 AND event_tag IS NOT NULL ORDER BY time,record_id"
        ).map_err(sql_error)?;
        let mut cursor = statement
            .query(params![account, start, end])
            .map_err(sql_error)?;
        while let Some(row) = cursor.next().map_err(sql_error)? {
            consume(Event {
                time: row.get(0).map_err(sql_error)?,
                numeric_time: row.get(1).map_err(sql_error)?,
                tag: row.get(2).map_err(sql_error)?,
                text: row.get(3).map_err(sql_error)?,
                execution_id: row.get(4).map_err(sql_error)?,
            });
        }
    }
    rows(
        "performance_result_event",
        &["range_key", "idx", "time", "tag", "text", "execution_id"],
        newest.into_iter().enumerate().map(|(index, event)| {
            Ok(vec![
                json!(range),
                json!(index),
                json!(event.time),
                json!(event.tag),
                json!(event.text),
                json!(event.execution_id),
            ])
        }),
        emit,
    )?;
    Ok(count)
}

fn daily_costs(
    connection: &Connection,
    account: i64,
    range: &str,
    start: f64,
    end: f64,
    emit: &mut dyn FnMut(&Value) -> Result<(), String>,
) -> Result<(), String> {
    let predicate =
        "FROM analysis_source_trade t JOIN analysis_source_execution e ON e.record_id=t.record_id \
        WHERE e.account_id=?1 AND e.time>=?2 AND e.time<=?3";
    let sql = format!("SELECT t.day,COUNT(*),COUNT(t.value),SUM(t.value), \
        COUNT(CASE WHEN t.value IS NOT NULL AND t.cost IS NOT NULL THEN 1 END), \
        SUM(CASE WHEN t.value IS NOT NULL AND t.cost IS NOT NULL THEN t.cost END), \
        SUM(CASE WHEN t.value IS NOT NULL AND t.cost IS NOT NULL THEN t.value ELSE 0 END), \
        SUM(CASE WHEN t.value IS NOT NULL AND t.cost IS NOT NULL THEN t.value*t.loss_bp ELSE 0 END), \
        COUNT(CASE WHEN t.fee IS NOT NULL AND t.fee_currency IS NOT NULL AND t.fee_currency!='' THEN 1 END) \
        {predicate} GROUP BY t.day ORDER BY t.day");
    let mut statement = connection.prepare(&sql).map_err(sql_error)?;
    let mut cursor = statement
        .query(params![account, start, end])
        .map_err(sql_error)?;
    let values = std::iter::from_fn(|| match cursor.next() {
        Ok(Some(row)) => Some((|| -> Result<Vec<Value>, String> {
            let day: String = row.get(0).map_err(sql_error)?;
            let count: i64 = row.get(1).map_err(sql_error)?;
            let valued: i64 = row.get(2).map_err(sql_error)?;
            let value: Option<f64> = row.get(3).map_err(sql_error)?;
            let covered: i64 = row.get(4).map_err(sql_error)?;
            let cost: Option<f64> = row.get(5).map_err(sql_error)?;
            let covered_value: f64 = row.get(6).map_err(sql_error)?;
            let weighted: Option<f64> = row.get(7).map_err(sql_error)?;
            let fee_covered: i64 = row.get(8).map_err(sql_error)?;
            let loss = weighted
                .filter(|_| covered_value > 0.0)
                .map(|amount| amount / covered_value);
            let coverage = value
                .filter(|total| valued == count && *total > 0.0)
                .map(|total| covered_value / total);
            // Existing daily summaries omit coveredValue and estimated. Preserve
            // their absence instead of manufacturing API fields during storage.
            Ok(vec![
                json!(range),
                json!(day),
                json!(value),
                json!(cost),
                json!(loss),
                json!(coverage),
                Value::Null,
                json!(covered),
                json!(count),
                json!(fee_covered),
                Value::Null,
                json!(valued == count),
                json!(255),
            ])
        })()),
        Ok(None) => None,
        Err(error) => Some(Err(sql_error(error))),
    });
    rows(
        "performance_result_daily_cost",
        &[
            "range_key",
            "day",
            "value",
            "cost",
            "loss_bp",
            "coverage",
            "covered_value",
            "covered",
            "count",
            "fee_covered",
            "estimated",
            "amount_complete",
            "present",
        ],
        values,
        emit,
    )?;
    drop(cursor);
    drop(statement);
    let sql = format!(
        "SELECT t.day,t.fee_currency,SUM(t.fee) {predicate} \
        AND t.fee IS NOT NULL AND t.fee_currency IS NOT NULL AND t.fee_currency!='' \
        GROUP BY t.day,t.fee_currency ORDER BY t.day,t.fee_currency"
    );
    let mut statement = connection.prepare(&sql).map_err(sql_error)?;
    let mut cursor = statement
        .query(params![account, start, end])
        .map_err(sql_error)?;
    let values = std::iter::from_fn(|| match cursor.next() {
        Ok(Some(row)) => Some((|| -> Result<Vec<Value>, String> {
            Ok(vec![
                json!(range),
                json!(row.get::<_, String>(0).map_err(sql_error)?),
                json!(row.get::<_, String>(1).map_err(sql_error)?),
                json!(row.get::<_, f64>(2).map_err(sql_error)?),
            ])
        })()),
        Ok(None) => None,
        Err(error) => Some(Err(sql_error(error))),
    });
    rows(
        "performance_result_daily_fee",
        &["range_key", "day", "currency", "value"],
        values,
        emit,
    )
}

fn range_rows(
    connection: &Connection,
    account: i64,
    key: &str,
    mut performance: Value,
    emit: &mut dyn FnMut(&Value) -> Result<(), String>,
) -> Result<(), String> {
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
    array_rows(
        key,
        &mut performance,
        "points",
        "performance_result_point",
        POINT_COLUMNS,
        emit,
    )?;
    array_rows(
        key,
        &mut performance,
        "sizing_fallback_ranges",
        "performance_result_fallback",
        &["start", "end", "count"],
        emit,
    )?;
    array_rows(
        key,
        &mut performance,
        "bindings",
        "performance_result_binding",
        &["time", "portfolio_id"],
        emit,
    )?;
    for (field, kind) in [("skips", "portfolio"), ("target_skips", "target")] {
        let value = performance
            .get_mut(field)
            .map(Value::take)
            .unwrap_or(Value::Null);
        if !value.is_null() {
            rows(
                "performance_result_skip",
                &[
                    "range_key",
                    "kind",
                    "count",
                    "missing_target",
                    "missing_ticks",
                    "first_time",
                    "last_time",
                ],
                [Ok(vec![
                    json!(key),
                    json!(kind),
                    value["count"].clone(),
                    value["missing_target"].clone(),
                    value["missing_ticks"].clone(),
                    value["first_time"].clone(),
                    value["last_time"].clone(),
                ])],
                emit,
            )?;
        }
    }
    let mut calendar = performance["calendar"].take();
    for (field, kind) in [
        ("closed_ranges", "closed"),
        ("unavailable_ranges", "unavailable"),
    ] {
        let values = calendar
            .get_mut(field)
            .map(Value::take)
            .unwrap_or(json!([]));
        let values = values
            .as_array()
            .ok_or("INVALID_RESULT_ROW:calendar ranges must be arrays")?;
        rows(
            "performance_result_calendar_range",
            &["range_key", "kind", "idx", "start", "end"],
            values.iter().enumerate().map(|(index, value)| {
                Ok(vec![
                    json!(key),
                    json!(kind),
                    json!(index),
                    value["start"].clone(),
                    value["end"].clone(),
                ])
            }),
            emit,
        )?;
    }
    daily_costs(connection, account, key, start, end, emit)?;
    let count = events(connection, account, key, start, end, emit)?;
    let mut row = vec![json!(key)];
    row.extend(
        RANGE_COLUMNS[..10]
            .iter()
            .map(|field| performance[*field].take()),
    );
    row.extend([
        calendar["status"].take(),
        calendar["calendar_id"].take(),
        calendar["label"].take(),
        json!(count),
    ]);
    let columns: Vec<_> = ["range_key"]
        .into_iter()
        .chain(RANGE_COLUMNS.iter().copied())
        .collect();
    rows("performance_result_range", &columns, [Ok(row)], emit)
}

/// Emit typed result rows without ever writing to the source database.
pub fn write(
    connection: &Connection,
    request: &Value,
    emit: &mut dyn FnMut(&Value) -> Result<(), String>,
) -> Result<(), String> {
    let account = request
        .get("account_id")
        .and_then(Value::as_i64)
        .ok_or("account_id must be an integer")?;
    // The runtime owns one read transaction covering both version capture and
    // every fact cursor. A later Python writer compares this version at commit.
    let version: Option<i64> = connection
        .query_row(
            "SELECT source_version FROM account_analysis WHERE account_id=?1",
            [account],
            |row| row.get(0),
        )
        .optional()
        .map_err(sql_error)?;
    let mut calculation = request.clone();
    let object = calculation
        .as_object_mut()
        .ok_or("request must be an object")?;
    object.insert("include_metadata".to_owned(), json!(true));
    object.insert("require_source_facts".to_owned(), json!(true));
    let mut settings = Value::Null;
    let mut data_until: Option<String> = None;
    let mut row_counts: BTreeMap<String, usize> = BTreeMap::new();
    {
        let mut counted_emit = |frame: &Value| -> Result<(), String> {
            emit(frame)?;
            let table = frame["table"]
                .as_str()
                .ok_or("INVALID_RESULT_ROW:missing table")?;
            let count = frame["rows"]
                .as_array()
                .ok_or("INVALID_RESULT_ROW:missing rows")?
                .len();
            *row_counts.entry(table.to_owned()).or_default() += count;
            Ok(())
        };
        // Each range is released before the next scan. This intentionally spends
        // extra SQLite scans to avoid retaining three daily result trees at once.
        for key in ["30", "90", "all"] {
            calculation["range"] = json!(key);
            let performance = crate::performance::handle(connection, &calculation)?;
            if settings.is_null() {
                settings = performance["settings"].clone();
            }
            if key == "all" {
                data_until = performance["end"].as_str().map(str::to_owned);
            }
            range_rows(connection, account, key, performance, &mut counted_emit)?;
        }
        // New native publication prepares one reusable execution index. The
        // legacy stream retains exactly its original frame/table contract.
        if request
            .get("include_execution_summaries")
            .and_then(Value::as_bool)
            == Some(true)
        {
            crate::execution_summary::write(connection, account, &mut counted_emit)?;
        }
    }
    emit(
        &json!({"type":"complete","account_id":account,"source_version":version,
        "settings":settings,"data_until":data_until,"row_counts":row_counts}),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn row_byte_count_matches_escaping_without_encoding_native_rows() {
        let row = vec![
            Value::Null,
            json!(true),
            json!(false),
            json!(-123.45),
            json!("中文\"\\\n\r\t\u{0008}\u{000c}\u{0001}"),
            json!([]),
            json!({"escaped\nkey": [1, null, "value"]}),
        ];
        assert_eq!(row_bytes(&row), serde_json::to_vec(&row).unwrap().len());
        assert_eq!(row_bytes(&[]), 2);
    }

    #[test]
    fn batches_bound_rows_and_preserve_order() {
        let mut frames = Vec::new();
        rows(
            "example",
            &["idx"],
            (0..777).map(|index| Ok(vec![json!(index)])),
            &mut |frame| {
                frames.push(frame.clone());
                Ok(())
            },
        )
        .unwrap();
        assert_eq!(frames.len(), 4);
        assert_eq!(frames[0]["rows"].as_array().unwrap().len(), 256);
        assert_eq!(
            frames[3]["rows"],
            json!([
                [768],
                [769],
                [770],
                [771],
                [772],
                [773],
                [774],
                [775],
                [776]
            ])
        );
        assert_eq!(
            frames
                .iter()
                .map(|frame| frame["rows"].as_array().unwrap().len())
                .sum::<usize>(),
            777
        );
    }

    #[test]
    fn batches_bound_bytes_and_reject_single_oversized_values() {
        let mut frames = Vec::new();
        rows(
            "example",
            &["text"],
            (0..6).map(|_| Ok(vec![json!("a".repeat(200_000))])),
            &mut |frame| {
                frames.push(frame.clone());
                Ok(())
            },
        )
        .unwrap();
        assert_eq!(frames.len(), 3);
        assert!(frames
            .iter()
            .all(|frame| serde_json::to_vec(frame).unwrap().len() < 1024 * 1024));
        assert!(rows(
            "example",
            &["text"],
            [Ok(vec![json!("x".repeat(MAX_ROW_BYTES))])],
            &mut |_| Ok(())
        )
        .unwrap_err()
        .starts_with("RESULT_FRAME_TOO_LARGE:"));
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
            "CREATE TABLE analysis_source_execution(record_id INTEGER PRIMARY KEY,account_id INTEGER,time REAL,created_at TEXT,event_tag TEXT,event_text TEXT,execution_id TEXT);
             CREATE TABLE analysis_source_trade(record_id INTEGER,trade_index INTEGER,day TEXT,value REAL,cost REAL,loss_bp REAL,fee REAL,fee_currency TEXT);
             CREATE TABLE portfolioaccount(id INTEGER PRIMARY KEY,account_id INTEGER,created_at TEXT,portfolio_id INTEGER);
             CREATE TABLE schedule_skip(id INTEGER PRIMARY KEY,account_id INTEGER,triggered_at TEXT,reason_code TEXT);"
        ).unwrap();
        connection
    }

    #[test]
    fn daily_costs_filter_by_execution_time_and_keep_unknown_amounts() {
        let connection = scalar_fixture();
        connection
            .execute_batch(
                "INSERT INTO analysis_source_execution VALUES
             (1,2,100,'2026-01-01T23:59:00',NULL,NULL,'one'),
             (2,2,200,'2026-01-02T09:00:00',NULL,NULL,'two');
             INSERT INTO analysis_source_trade VALUES
             (1,0,'2026-01-01',100,2,200,0,'CNY'),
             (1,1,'2026-01-02',NULL,7,NULL,-0.2,'USD'),
             (1,2,'2026-01-02',NULL,NULL,NULL,0,'USD'),
             (2,0,'2026-01-02',500,5,100,8,'CNY');",
            )
            .unwrap();
        let mut frames = Vec::new();
        daily_costs(&connection, 2, "all", 100.0, 100.0, &mut |frame| {
            frames.push(frame.clone());
            Ok(())
        })
        .unwrap();
        let costs = &frames[0]["rows"];
        assert_eq!(
            costs[0],
            json!([
                "all",
                "2026-01-01",
                100.0,
                2.0,
                200.0,
                1.0,
                null,
                1,
                1,
                1,
                null,
                true,
                255
            ])
        );
        assert_eq!(
            costs[1],
            json!([
                "all",
                "2026-01-02",
                null,
                null,
                null,
                null,
                null,
                0,
                2,
                2,
                null,
                false,
                255
            ])
        );
        assert_eq!(
            frames[1]["rows"],
            json!([
                ["all", "2026-01-01", "CNY", 0.0],
                ["all", "2026-01-02", "USD", -0.2]
            ])
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
                "INSERT INTO analysis_source_execution VALUES(?1,2,?2,'2026-01-01T10:00:00','failed',?3,?4)",
                params![index, time, format!("failure-{index}"),format!("execution-{index}")],
            ).unwrap();
        }
        let mut frames = Vec::new();
        let count = events(&connection, 2, "all", time, time, &mut |frame| {
            frames.push(frame.clone());
            Ok(())
        })
        .unwrap();
        assert_eq!(count, 152);
        let event_rows = frames[0]["rows"].as_array().unwrap();
        assert_eq!(event_rows.len(), 100);
        assert_eq!(
            event_rows[0],
            json!(["all", 0, "2026-01-01T10:00:00", "换绑", "组合 #7", null])
        );
        assert_eq!(
            event_rows[1],
            json!(["all", 1, "2026-01-01T10:00:00", "跳过", "执行中", null])
        );
        assert_eq!(event_rows[99][5], "execution-97");
        let mut emitted = false;
        assert_eq!(events_empty(&connection, time, &mut emitted), 0);
        assert!(!emitted);
    }

    fn events_empty(connection: &Connection, time: f64, emitted: &mut bool) -> usize {
        events(connection, 2, "all", time + 1.0, time + 2.0, &mut |_| {
            *emitted = true;
            Ok(())
        })
        .unwrap()
    }
}
