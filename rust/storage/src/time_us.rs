//! Canonical scalar time storage. JSON columns are never inspected.

use chrono::{
    DateTime, Datelike, FixedOffset, NaiveDate, NaiveDateTime, SecondsFormat, Timelike, Utc,
};
use rusqlite::{Connection, Row, RowIndex};
use serde_json::{Map, Value};

#[derive(Clone, Copy, Debug)]
pub(crate) enum Unit {
    Iso,
    Millis,
    Seconds,
    DurationSeconds,
}
#[derive(Clone, Copy, Debug)]
pub(crate) struct Column {
    pub name: &'static str,
    pub storage: &'static str,
    pub unit: Unit,
}
macro_rules! cols {
    ($($name:literal => $storage:literal : $unit:ident),* $(,)?) => { &[$(Column { name:$name, storage:$storage, unit:Unit::$unit }),*] };
}

pub(crate) const TABLES: &[&str] = &[
    "account",
    "portfolio",
    "account_asset_snapshot",
    "portfolioaccount",
    "execution_artifact",
    "execution_input",
    "account_connection_state",
    "account_control_event",
    "account_control_session",
    "account_notification_state",
    "accountruntimesync",
    "accountruntimesyncattempt",
    "executerecord",
    "execution_event",
    "execution_intent",
    "notification_event",
    "order_submission",
    "order_submission_warning",
    "schedule_skip",
    "supplement_group",
    "supplement_step",
    "system_notification_state",
    "target_weight_snapshot",
    "execution_symbol_tick",
    "business_asset_snapshot",
    "execution_order",
    "business_trade",
    "account_analysis",
    "analysis_state",
    "analysis_day",
];

pub(crate) fn columns(table: &str) -> &'static [Column] {
    match table {
        "account" | "portfolio" => {
            cols!("created_at"=>"created_at_us":Iso,"updated_at"=>"updated_at_us":Iso)
        }
        "account_asset_snapshot"
        | "portfolioaccount"
        | "execution_artifact"
        | "execution_input" => cols!("created_at"=>"created_at_us":Iso),
        "account_connection_state" => {
            cols!("observed_at"=>"observed_at_us":Iso,"last_success_at"=>"last_success_at_us":Iso)
        }
        "account_control_event" => {
            cols!("created_at"=>"created_at_us":Iso,"occurred_at_ms"=>"occurred_at_us":Millis)
        }
        "account_control_session" => {
            cols!("started_at_ms"=>"started_at_us":Millis,"finished_at_ms"=>"finished_at_us":Millis)
        }
        "account_notification_state" => {
            cols!("last_success_at"=>"last_success_at_us":Iso,"last_attempt_at"=>"last_attempt_at_us":Iso)
        }
        "accountruntimesync" => {
            cols!("requested_at"=>"requested_at_us":Iso,"last_attempt_at"=>"last_attempt_at_us":Iso,"synchronized_at"=>"synchronized_at_us":Iso)
        }
        "accountruntimesyncattempt" => cols!("attempted_at"=>"attempted_at_us":Iso),
        "executerecord" => {
            cols!("created_at"=>"created_at_us":Iso,"execution_time"=>"execution_time_us":DurationSeconds,"termination_requested_at"=>"termination_requested_at_us":Iso,"termination_acked_at"=>"termination_acked_at_us":Iso,"termination_finished_at"=>"termination_finished_at_us":Iso)
        }
        "execution_event" => {
            cols!("ts_local_created"=>"ts_local_created_us":Iso,"ts_exchange"=>"ts_exchange_us":Iso,"ts_persisted"=>"ts_persisted_us":Iso)
        }
        "execution_intent" => {
            cols!("created_at"=>"created_at_us":Iso,"started_at"=>"started_at_us":Iso,"finished_at"=>"finished_at_us":Iso,"cancel_requested_at"=>"cancel_requested_at_us":Iso)
        }
        "notification_event" => {
            cols!("created_at"=>"created_at_us":Iso,"finished_at"=>"finished_at_us":Iso)
        }
        "order_submission" | "order_submission_warning" => {
            cols!("occurred_at_ms"=>"occurred_at_us":Millis)
        }
        "schedule_skip" => cols!("triggered_at"=>"triggered_at_us":Iso),
        "supplement_group" => {
            cols!("base_scheduled_at"=>"base_scheduled_at_us":Iso,"expires_at"=>"expires_at_us":Iso,"cancel_requested_at"=>"cancel_requested_at_us":Iso)
        }
        "supplement_step" => cols!("scheduled_at"=>"scheduled_at_us":Iso),
        "system_notification_state" => cols!("finished_at"=>"finished_at_us":Iso),
        "target_weight_snapshot" => cols!("calculated_at"=>"calculated_at_us":Iso),
        "execution_symbol_tick" => {
            cols!("timestamp"=>"timestamp_us":Millis,"timestamp_datetime"=>"timestamp_datetime_us":Iso,"update_time"=>"update_time_us":Iso)
        }
        "business_asset_snapshot" => cols!("update_time"=>"update_time_us":Iso),
        "execution_order" => {
            cols!("create_time"=>"create_time_us":Iso,"update_time"=>"update_time_us":Iso,"update_timestamp"=>"update_timestamp_us":Seconds)
        }
        "business_trade" => cols!("trade_time"=>"trade_time_us":Iso),
        "account_analysis" => cols!("retry_at"=>"retry_at_us":Seconds),
        "analysis_state" => {
            cols!("computed_at"=>"computed_at_us":Iso,"start"=>"start_us":Iso,"last_time"=>"last_time_us":Iso,"baseline_time"=>"baseline_time_us":Iso,"skip_first"=>"skip_first_us":Iso,"skip_last"=>"skip_last_us":Iso,"target_skip_first"=>"target_skip_first_us":Iso,"target_skip_last"=>"target_skip_last_us":Iso,"executable_start"=>"executable_start_us":Iso,"target_start"=>"target_start_us":Iso,"contribution_time"=>"contribution_time_us":Iso)
        }
        "analysis_day" => cols!("observed_at"=>"observed_at_us":Iso),
        _ => &[],
    }
}

pub(crate) fn storage_name(table: &str, name: &str) -> Option<&'static str> {
    columns(table)
        .iter()
        .find(|c| c.name == name)
        .map(|c| c.storage)
}

pub(crate) fn from_millis(value: i64) -> Result<i64, String> {
    value
        .checked_mul(1000)
        .ok_or_else(|| "millisecond timestamp overflows microseconds".into())
}
pub(crate) fn from_seconds(value: f64) -> Result<i64, String> {
    let micros = (value * 1_000_000.0).round();
    if !micros.is_finite() || micros < i64::MIN as f64 || micros >= i64::MAX as f64 {
        return Err("second timestamp overflows microseconds".into());
    }
    Ok(micros as i64)
}

fn naive(value: &str) -> Result<NaiveDateTime, String> {
    for pattern in ["%Y-%m-%dT%H:%M:%S%.f", "%Y-%m-%dT%H:%M"] {
        if let Ok(value) = NaiveDateTime::parse_from_str(value, pattern) {
            return Ok(value);
        }
    }
    NaiveDate::parse_from_str(value, "%Y-%m-%d")
        .ok()
        .and_then(|d| d.and_hms_opt(0, 0, 0))
        .ok_or_else(|| format!("invalid scalar time {value:?}"))
}

/// Naive historical values use the host timezone at that date, including DST.
pub(crate) fn parse(value: &str) -> Result<i64, String> {
    let normalized = value.replacen(' ', "T", 1);
    if let Ok(value) = DateTime::parse_from_rfc3339(&normalized) {
        return Ok(value.timestamp_micros());
    }
    let value = naive(&normalized)?;
    // mktime resolves local DST using the platform's timezone database.
    let mut local: libc::tm = unsafe { std::mem::zeroed() };
    local.tm_year = value.year() - 1900;
    local.tm_mon = value.month0() as i32;
    local.tm_mday = value.day() as i32;
    local.tm_hour = value.hour() as i32;
    local.tm_min = value.minute() as i32;
    local.tm_sec = value.second() as i32;
    local.tm_isdst = -1;
    let seconds = unsafe { libc::mktime(&mut local) };
    if seconds == -1 {
        // -1 is also a valid instant; verify it before treating it as mktime failure.
        let mut check: libc::tm = unsafe { std::mem::zeroed() };
        #[cfg(unix)]
        let valid = !unsafe { libc::localtime_r(&seconds, &mut check) }.is_null();
        #[cfg(windows)]
        let valid = unsafe { libc::localtime_s(&mut check, &seconds) } == 0;
        #[cfg(not(any(unix, windows)))]
        let valid = false;
        if !valid
            || (
                check.tm_year,
                check.tm_mon,
                check.tm_mday,
                check.tm_hour,
                check.tm_min,
                check.tm_sec,
            ) != (
                local.tm_year,
                local.tm_mon,
                local.tm_mday,
                local.tm_hour,
                local.tm_min,
                local.tm_sec,
            )
        {
            return Err("local time is outside the platform's supported range".into());
        }
    }
    (seconds as i64)
        .checked_mul(1_000_000)
        .and_then(|v| v.checked_add(i64::from(value.nanosecond() / 1000)))
        .ok_or_else(|| "local time overflows microseconds".into())
}

pub(crate) fn format(value: i64) -> Result<String, String> {
    DateTime::<Utc>::from_timestamp_micros(value)
        .map(|v| v.to_rfc3339_opts(SecondsFormat::Micros, true))
        .ok_or_else(|| "microsecond timestamp is out of range".into())
}
pub(crate) fn local_datetime(value: i64) -> Result<NaiveDateTime, String> {
    DateTime::<Utc>::from_timestamp_micros(value)
        .map(|v| {
            v.with_timezone(&FixedOffset::east_opt(28800).expect("Shanghai offset"))
                .naive_local()
        })
        .ok_or_else(|| "microsecond timestamp is out of range".into())
}
/// Analysis date boundaries remain Shanghai-local; explicit offsets take precedence.
pub(crate) fn boundary(value: &str) -> Result<i64, String> {
    let normalized = match value.len() {
        4 => format!("{value}-01-01"),
        7 => format!("{value}-01"),
        _ => value.into(),
    }
    .replacen(' ', "T", 1);
    if let Ok(value) = DateTime::parse_from_rfc3339(&normalized) {
        return Ok(value.timestamp_micros());
    }
    naive(&normalized)?
        .and_utc()
        .timestamp_micros()
        .checked_sub(28_800_000_000)
        .ok_or_else(|| "query boundary overflows microseconds".into())
}
fn conversion(error: String) -> rusqlite::Error {
    rusqlite::Error::FromSqlConversionFailure(
        0,
        rusqlite::types::Type::Integer,
        Box::new(std::io::Error::new(std::io::ErrorKind::InvalidData, error)),
    )
}
pub(crate) fn read<I: RowIndex>(row: &Row<'_>, index: I) -> rusqlite::Result<String> {
    format(row.get(index)?).map_err(conversion)
}
pub(crate) fn read_optional<I: RowIndex>(
    row: &Row<'_>,
    index: I,
) -> rusqlite::Result<Option<String>> {
    row.get::<_, Option<i64>>(index)?
        .map(format)
        .transpose()
        .map_err(conversion)
}

pub(crate) fn encode_columns(table: &str, row: &mut Map<String, Value>) -> Result<(), String> {
    for column in columns(table) {
        if let Some(value) = row.remove(column.name) {
            let value = if value.is_null() {
                Value::Null
            } else {
                let micros = match column.unit {
                    Unit::Iso => parse(value.as_str().ok_or_else(|| {
                        format!("{}.{} must be a time string", table, column.name)
                    })?)?,
                    Unit::Millis => from_millis(value.as_i64().ok_or_else(|| {
                        format!("{}.{} must be integer milliseconds", table, column.name)
                    })?)?,
                    Unit::Seconds | Unit::DurationSeconds => from_seconds(
                        value
                            .as_f64()
                            .ok_or_else(|| format!("{}.{} must be seconds", table, column.name))?,
                    )?,
                };
                Value::from(micros)
            };
            row.insert(column.storage.into(), value);
        }
    }
    Ok(())
}
pub(crate) fn decode_columns(table: &str, row: &mut Map<String, Value>) -> Result<(), String> {
    for column in columns(table) {
        if let Some(value) = row.remove(column.storage) {
            let value = if value.is_null() {
                Value::Null
            } else {
                let micros = value.as_i64().ok_or_else(|| {
                    format!("{}.{} must be integer microseconds", table, column.storage)
                })?;
                match column.unit {
                    Unit::Iso => Value::from(format(micros)?),
                    Unit::Millis => Value::from(micros.div_euclid(1000)),
                    Unit::Seconds | Unit::DurationSeconds => {
                        Value::from(micros as f64 / 1_000_000.0)
                    }
                }
            };
            row.insert(column.name.into(), value);
        }
    }
    Ok(())
}
/// Only the existing 0033 conversion uses the temporary legacy table layout.
pub(crate) fn encode_on(
    connection: &Connection,
    table: &str,
    row: &mut Map<String, Value>,
) -> Result<(), String> {
    if let Some(column) = columns(table).first() {
        let present: bool = connection
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM pragma_table_info(?1) WHERE name=?2)",
                [table, column.storage],
                |r| r.get(0),
            )
            .map_err(|e| e.to_string())?;
        if present {
            encode_columns(table, row)?;
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn exact_microseconds_and_negative_epoch() {
        for (text, us) in [
            ("1970-01-01T00:00:00.000001Z", 1),
            ("1969-12-31T23:59:59.999999Z", -1),
            ("1970-01-01T08:00:00.000001+08:00", 1),
        ] {
            assert_eq!(parse(text).unwrap(), us);
            assert_eq!(parse(&format(us).unwrap()).unwrap(), us);
        }
        assert!(from_millis(i64::MAX).is_err());
        assert!(parse("garbage").is_err());
    }
    #[test]
    fn only_manifest_scalars_change_and_business_dates_stay_local() {
        let extra =
            serde_json::json!({"created_at":"not-a-time","nested":{"time":"2026-01-01T09:00:00"}});
        let mut row = serde_json::json!({"created_at":"1970-01-01T00:00:00.123456Z","extra":extra})
            .as_object()
            .unwrap()
            .clone();
        encode_columns("executerecord", &mut row).unwrap();
        assert_eq!(row["created_at_us"], 123456);
        assert_eq!(row["extra"], extra);
        decode_columns("executerecord", &mut row).unwrap();
        assert_eq!(row["created_at"], "1970-01-01T00:00:00.123456Z");
        assert_eq!(row["extra"], extra);
        assert_eq!(boundary("1970-01-01").unwrap(), -28_800_000_000);
    }
}
