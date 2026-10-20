//! Assemble calendar and binding metadata from Python-written scalar facts.
//!
//! Calendar providers remain with Python's channel registry. Rust reads their
//! frozen per-day decisions and never calls back across the process boundary.

use chrono::{DateTime, Duration, FixedOffset, NaiveDate, NaiveDateTime, Timelike};
use rusqlite::{params, Connection, OptionalExtension};
use serde_json::{json, Value};

/// Apply the same Shanghai-local convention as performance.local_time.
pub(crate) fn local_time(value: &str) -> Result<NaiveDateTime, String> {
    if let Ok(parsed) = DateTime::parse_from_rfc3339(value) {
        return Ok(parsed
            .with_timezone(&FixedOffset::east_opt(8 * 60 * 60).expect("Shanghai offset"))
            .naive_local());
    }
    let normalized = value.replacen(' ', "T", 1);
    for pattern in ["%Y-%m-%dT%H:%M:%S%.f", "%Y-%m-%dT%H:%M"] {
        if let Ok(parsed) = NaiveDateTime::parse_from_str(&normalized, pattern) {
            return Ok(parsed);
        }
    }
    NaiveDate::parse_from_str(&normalized, "%Y-%m-%d")
        .ok()
        .and_then(|day| day.and_hms_opt(0, 0, 0))
        .ok_or_else(|| format!("Invalid metadata time {value:?}"))
}

fn display_time(value: NaiveDateTime) -> String {
    if value.nanosecond() == 0 {
        value.format("%Y-%m-%dT%H:%M:%S").to_string()
    } else {
        value.format("%Y-%m-%dT%H:%M:%S%.6f").to_string()
    }
}

fn empty_calendar(status: &str) -> Value {
    json!({"status":status,"calendar_id":null,"label":null,
        "closed_ranges":[],"unavailable_ranges":[]})
}

/// Merge consecutive days while holding only the last open range.
fn append_day(ranges: &mut Vec<Value>, day: NaiveDate) {
    let day_text = day.format("%Y-%m-%d").to_string();
    let previous = (day - Duration::days(1)).format("%Y-%m-%d").to_string();
    if let Some(last) = ranges.last_mut() {
        if last.get("end").and_then(Value::as_str) == Some(previous.as_str()) {
            last["end"] = Value::String(day_text);
            return;
        }
    }
    ranges.push(json!({"start":day_text,"end":day_text}));
}

fn read_calendar(
    connection: &Connection,
    channel: &str,
    first: NaiveDate,
    last: NaiveDate,
) -> Result<Value, String> {
    let first_text = first.format("%Y-%m-%d").to_string();
    let last_text = last.format("%Y-%m-%d").to_string();
    let mut statement = connection
        .prepare(
            "SELECT day,status,calendar_id,label FROM analysis_calendar_day \
             WHERE channel=?1 AND day>=?2 AND day<=?3 ORDER BY day",
        )
        .map_err(|error| error.to_string())?;
    let mut rows = statement
        .query(params![channel, first_text, last_text])
        .map_err(|error| error.to_string())?;
    let mut row = rows.next().map_err(|error| error.to_string())?;
    let mut current = first;
    let mut calendar_id = None::<String>;
    let mut label = None::<String>;
    let mut closed = Vec::new();
    let mut unavailable = Vec::new();
    let mut unavailable_count = 0;
    while current <= last {
        let day = current.format("%Y-%m-%d").to_string();
        let matches = row
            .map(|row| row.get::<_, String>(0))
            .transpose()
            .map_err(|error| error.to_string())?
            .is_some_and(|observed| observed == day);
        if matches {
            let fact = row.expect("matching calendar row");
            let status: String = fact.get(1).map_err(|error| error.to_string())?;
            for (column, previous) in [(2, &mut calendar_id), (3, &mut label)] {
                if let Some(value) = fact
                    .get::<_, Option<String>>(column)
                    .map_err(|error| error.to_string())?
                    .filter(|value| !value.is_empty())
                {
                    *previous = Some(value);
                }
            }
            match status.as_str() {
                "not_required" => return Ok(empty_calendar("not_required")),
                "unregistered" => return Ok(empty_calendar("unavailable")),
                "available_closed" => append_day(&mut closed, current),
                "unavailable" => {
                    unavailable_count += 1;
                    append_day(&mut unavailable, current);
                }
                "available_open" => {}
                _ => return Err(format!("INVALID_CALENDAR_FACT: unknown status {status:?}")),
            }
            row = rows.next().map_err(|error| error.to_string())?;
        } else {
            // A missing provider decision is unknown, never an assumed open day.
            unavailable_count += 1;
            append_day(&mut unavailable, current);
        }
        current = current
            .succ_opt()
            .ok_or("calendar date exceeds supported range")?;
    }
    let count = (last - first).num_days() + 1;
    let status = if unavailable_count == 0 {
        "available"
    } else if unavailable_count == count {
        "unavailable"
    } else {
        "partial"
    };
    Ok(
        json!({"status":status,"calendar_id":calendar_id,"label":label,
        "closed_ranges":closed,"unavailable_ranges":unavailable}),
    )
}

fn read_bindings(
    connection: &Connection,
    account_id: i64,
    start: NaiveDateTime,
    end: NaiveDateTime,
) -> Result<Vec<Value>, String> {
    let mut statement = connection
        .prepare(
            "SELECT created_at,portfolio_id FROM portfolioaccount WHERE account_id=?1 ORDER BY id",
        )
        .map_err(|error| error.to_string())?;
    let mut rows = statement
        .query([account_id])
        .map_err(|error| error.to_string())?;
    let mut selected = Vec::new();
    while let Some(row) = rows.next().map_err(|error| error.to_string())? {
        let when = local_time(&row.get::<_, String>(0).map_err(|error| error.to_string())?)?;
        if start <= when && when <= end {
            let portfolio_id: Option<i64> = row.get(1).map_err(|error| error.to_string())?;
            selected.push((when, portfolio_id));
        }
    }
    // Stable ordering preserves the original row order for equal local times.
    selected.sort_by_key(|(when, _)| *when);
    Ok(selected
        .into_iter()
        .map(|(when, portfolio_id)| json!({"time":display_time(when),"portfolio_id":portfolio_id}))
        .collect())
}

/// Complete a computed range without decoding it in Python.
pub fn enrich(
    connection: &Connection,
    account_id: i64,
    performance: &mut Value,
) -> Result<(), String> {
    let boundaries = performance
        .get("baseline")
        .and_then(Value::as_str)
        .zip(performance.get("end").and_then(Value::as_str));
    let Some((start, end)) = boundaries else {
        performance["bindings"] = json!([]);
        performance["calendar"] = empty_calendar("unavailable");
        return Ok(());
    };
    let (start, end) = (local_time(start)?, local_time(end)?);
    let bindings = read_bindings(connection, account_id, start, end)?;
    let channel: Option<String> = connection
        .query_row(
            "SELECT trade_channel FROM account WHERE id=?1",
            [account_id],
            |row| row.get(0),
        )
        .optional()
        .map_err(|error| error.to_string())?;
    let calendar = if let Some(channel) = channel {
        read_calendar(connection, &channel, start.date(), end.date())?
    } else {
        empty_calendar("unavailable")
    };
    performance["bindings"] = Value::Array(bindings);
    performance["calendar"] = calendar;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture() -> Connection {
        let connection = Connection::open_in_memory().unwrap();
        connection
            .execute_batch(
                "CREATE TABLE account (id INTEGER PRIMARY KEY,trade_channel TEXT NOT NULL);
             INSERT INTO account VALUES (1,'ctp');
             CREATE TABLE portfolioaccount (id INTEGER PRIMARY KEY,account_id INTEGER,
                 portfolio_id INTEGER,created_at TEXT NOT NULL);
             CREATE TABLE analysis_calendar_day (channel TEXT NOT NULL,day TEXT NOT NULL,
                 status TEXT NOT NULL,calendar_id TEXT,label TEXT,PRIMARY KEY(channel,day));",
            )
            .unwrap();
        connection
    }

    #[test]
    fn calendar_merges_closed_and_missing_days_and_keeps_last_labels() {
        let connection = fixture();
        connection
            .execute_batch(
                "INSERT INTO analysis_calendar_day VALUES
             ('ctp','2026-01-01','available_open','china','旧名'),
             ('ctp','2026-01-02','available_closed',NULL,NULL),
             ('ctp','2026-01-03','available_closed','',''),
             ('ctp','2026-01-05','unavailable','updated','新名');",
            )
            .unwrap();
        let mut performance = json!({"baseline":"2026-01-01T10:00:00","end":"2026-01-05T15:00:00"});
        enrich(&connection, 1, &mut performance).unwrap();
        assert_eq!(
            performance["calendar"],
            json!({"status":"partial","calendar_id":"updated","label":"新名",
            "closed_ranges":[{"start":"2026-01-02","end":"2026-01-03"}],
            "unavailable_ranges":[{"start":"2026-01-04","end":"2026-01-05"}]})
        );
    }

    #[test]
    fn bindings_normalize_offsets_filter_inclusively_and_sort_stably() {
        let connection = fixture();
        connection
            .execute_batch(
                "INSERT INTO portfolioaccount VALUES
             (1,1,5,'2026-01-02T01:00:00Z'),
             (2,1,NULL,'2026-01-01T10:00:00'),
             (3,1,7,'2026-01-02T09:00:00+08:00'),
             (4,1,9,'2026-01-02T09:00:00.000001');",
            )
            .unwrap();
        let mut performance = json!({"baseline":"2026-01-01T10:00:00","end":"2026-01-02T09:00:00"});
        enrich(&connection, 1, &mut performance).unwrap();
        assert_eq!(
            performance["bindings"],
            json!([
            {"time":"2026-01-01T10:00:00","portfolio_id":null},
            {"time":"2026-01-02T09:00:00","portfolio_id":5},
            {"time":"2026-01-02T09:00:00","portfolio_id":7}])
        );
        assert_eq!(performance["calendar"]["status"], "unavailable");
    }

    #[test]
    fn not_required_resets_all_other_calendar_metadata() {
        let connection = fixture();
        connection
            .execute_batch(
                "INSERT INTO analysis_calendar_day VALUES
             ('ctp','2026-01-01','available_closed','china','中国'),
             ('ctp','2026-01-02','not_required',NULL,NULL);",
            )
            .unwrap();
        let mut performance = json!({"baseline":"2026-01-01T10:00:00","end":"2026-01-03T15:00:00"});
        enrich(&connection, 1, &mut performance).unwrap();
        assert_eq!(performance["calendar"], empty_calendar("not_required"));
    }

    #[test]
    fn unregistered_channel_resets_closed_ranges_and_labels() {
        let connection = fixture();
        connection
            .execute_batch(
                "INSERT INTO analysis_calendar_day VALUES
             ('ctp','2026-01-01','available_closed','china','中国'),
             ('ctp','2026-01-02','unregistered',NULL,NULL);",
            )
            .unwrap();
        let mut performance = json!({"baseline":"2026-01-01T10:00:00","end":"2026-01-03T15:00:00"});
        enrich(&connection, 1, &mut performance).unwrap();
        assert_eq!(performance["calendar"], empty_calendar("unavailable"));
    }

    #[test]
    fn empty_performance_needs_no_metadata_tables() {
        let connection = Connection::open_in_memory().unwrap();
        let mut performance = json!({"baseline":null,"end":null});
        enrich(&connection, 1, &mut performance).unwrap();
        assert_eq!(performance["calendar"], empty_calendar("unavailable"));
        assert_eq!(performance["bindings"], json!([]));
    }
}
