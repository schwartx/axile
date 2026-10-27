//! Keep Loguru's local wall-clock rotation rules outside the file writer.

use chrono::{Datelike, NaiveDate, NaiveDateTime, NaiveTime, TimeDelta, Timelike};
use serde::Deserialize;
use std::mem::MaybeUninit;

#[derive(Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
enum Rule {
    Interval { microseconds: i64 },
    Frequency { name: Frequency },
    Daytime { weekday: Option<u32>, time_us: u64 },
}

#[derive(Deserialize)]
#[serde(rename_all = "lowercase")]
enum Frequency {
    Hourly,
    Daily,
    Weekly,
    Monthly,
    Yearly,
}

enum Schedule {
    Utc {
        period_us: u64,
        bucket: u64,
    },
    Local {
        rule: Rule,
        next: Option<NaiveDateTime>,
    },
}

pub struct Rotation(Schedule);

impl Rotation {
    pub fn new(rule: Option<&str>, rotation_seconds: u64, base_us: u64) -> Result<Self, String> {
        let Some(rule) = rule else {
            let period_us = rotation_seconds
                .checked_mul(1_000_000)
                .filter(|period| *period > 0)
                .ok_or("invalid logging rotation interval")?;
            return Ok(Self(Schedule::Utc {
                period_us,
                bucket: base_us / period_us,
            }));
        };
        let rule: Rule = serde_json::from_str(rule).map_err(|error| error.to_string())?;
        match &rule {
            Rule::Interval { microseconds } if *microseconds <= 0 => {
                return Err("logging rotation interval must be positive".into());
            }
            Rule::Daytime { weekday, time_us }
                if weekday.is_some_and(|day| day > 6) || *time_us >= 86_400_000_000 =>
            {
                return Err("invalid logging rotation weekday or time".into());
            }
            _ => {}
        }
        let base = local_datetime(base_us).ok_or("cannot read local logging time")?;
        Self::local(rule, base)
    }

    fn local(rule: Rule, base: NaiveDateTime) -> Result<Self, String> {
        let next = rule
            .first(base)
            .ok_or("logging rotation time outside supported range")?;
        Ok(Self(Schedule::Local {
            rule,
            next: Some(next),
        }))
    }

    pub fn due(&mut self, now_us: u64) -> bool {
        if let Schedule::Utc { period_us, bucket } = &mut self.0 {
            let now = now_us / *period_us;
            let due = now != *bucket;
            *bucket = now;
            return due;
        }
        local_datetime(now_us).is_some_and(|now| self.due_local(now))
    }

    fn due_local(&mut self, now: NaiveDateTime) -> bool {
        let Schedule::Local { rule, next } = &mut self.0 else {
            return false;
        };
        let Some(limit) = *next else { return false };
        if now < limit {
            return false;
        }
        *next = rule.after(limit, now);
        true
    }
}

impl Rule {
    fn first(&self, base: NaiveDateTime) -> Option<NaiveDateTime> {
        match self {
            Self::Interval { microseconds } => {
                base.checked_add_signed(TimeDelta::microseconds(*microseconds))
            }
            Self::Frequency { name } => name.after(base),
            Self::Daytime { time_us, .. } => {
                // Loguru first tries today's clock time, even for a named weekday.
                let limit = base.date().and_time(clock_time(*time_us)?);
                if limit > base {
                    Some(limit)
                } else {
                    self.after(limit, base)
                }
            }
        }
    }

    fn after(&self, limit: NaiveDateTime, now: NaiveDateTime) -> Option<NaiveDateTime> {
        match self {
            Self::Interval { microseconds } => {
                let elapsed = now.signed_duration_since(limit).num_microseconds()?;
                let steps = elapsed.checked_div(*microseconds)?.checked_add(1)?;
                limit.checked_add_signed(TimeDelta::microseconds(steps.checked_mul(*microseconds)?))
            }
            Self::Frequency { name } => name.after(now),
            Self::Daytime { weekday, time_us } => {
                let candidate = now.date().and_time(clock_time(*time_us)?);
                let mut days = weekday
                    .map(|day| (7 + day - now.weekday().num_days_from_monday()) % 7)
                    .unwrap_or(0);
                if days == 0 && candidate <= now {
                    days = if weekday.is_some() { 7 } else { 1 };
                }
                candidate.checked_add_signed(TimeDelta::days(i64::from(days)))
            }
        }
    }
}

impl Frequency {
    fn after(&self, now: NaiveDateTime) -> Option<NaiveDateTime> {
        match self {
            Self::Hourly => now
                .date()
                .and_hms_opt(now.hour(), 0, 0)?
                .checked_add_signed(TimeDelta::hours(1)),
            Self::Daily => now
                .date()
                .and_hms_opt(0, 0, 0)?
                .checked_add_signed(TimeDelta::days(1)),
            Self::Weekly => now
                .date()
                .and_hms_opt(0, 0, 0)?
                .checked_add_signed(TimeDelta::days(i64::from(
                    7 - now.weekday().num_days_from_monday(),
                ))),
            Self::Monthly => {
                let (year, month) = if now.month() == 12 {
                    (now.year().checked_add(1)?, 1)
                } else {
                    (now.year(), now.month() + 1)
                };
                NaiveDate::from_ymd_opt(year, month, 1)?.and_hms_opt(0, 0, 0)
            }
            Self::Yearly => {
                NaiveDate::from_ymd_opt(now.year().checked_add(1)?, 1, 1)?.and_hms_opt(0, 0, 0)
            }
        }
    }
}

fn clock_time(time_us: u64) -> Option<NaiveTime> {
    NaiveTime::from_num_seconds_from_midnight_opt(
        (time_us / 1_000_000).try_into().ok()?,
        ((time_us % 1_000_000) * 1000).try_into().ok()?,
    )
}

fn local_datetime(micros: u64) -> Option<NaiveDateTime> {
    let seconds: libc::time_t = (micros / 1_000_000).try_into().ok()?;
    let mut result = MaybeUninit::<libc::tm>::uninit();
    #[cfg(unix)]
    let success = !unsafe { libc::localtime_r(&seconds, result.as_mut_ptr()) }.is_null();
    #[cfg(windows)]
    let success = unsafe { libc::localtime_s(result.as_mut_ptr(), &seconds) } == 0;
    #[cfg(not(any(unix, windows)))]
    let success = false;
    if !success {
        return None;
    }
    // Both platform functions initialize tm only when their success check passes.
    let result = unsafe { result.assume_init() };
    NaiveDate::from_ymd_opt(
        result.tm_year.checked_add(1900)?,
        result.tm_mon.checked_add(1)?.try_into().ok()?,
        result.tm_mday.try_into().ok()?,
    )?
    .and_hms_micro_opt(
        result.tm_hour.try_into().ok()?,
        result.tm_min.try_into().ok()?,
        result.tm_sec.try_into().ok()?,
        (micros % 1_000_000).try_into().ok()?,
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::{DateTime, TimeZone};

    fn date(value: &str) -> NaiveDateTime {
        value.parse().unwrap()
    }

    fn schedule(rule: &str, base: &str) -> Rotation {
        Rotation::local(serde_json::from_str(rule).unwrap(), date(base)).unwrap()
    }

    #[test]
    fn duration_uses_creation_time_and_keeps_microseconds() {
        let mut rotation = schedule(
            r#"{"kind":"interval","microseconds":500}"#,
            "2026-10-27T10:15:00.000200",
        );
        assert!(!rotation.due_local(date("2026-10-27T10:15:00.000699")));
        assert!(rotation.due_local(date("2026-10-27T10:15:00.000700")));
        assert!(rotation.due_local(date("2036-10-27T10:15:00.000700")));
        assert!(!rotation.due_local(date("2036-10-27T10:15:00.000701")));
    }

    #[test]
    fn calendar_rules_follow_month_year_and_week_boundaries() {
        for (name, base, limit) in [
            ("hourly", "2026-10-27T10:15:00", "2026-10-27T11:00:00"),
            ("daily", "2026-10-27T10:15:00", "2026-10-28T00:00:00"),
            ("weekly", "2026-10-27T10:15:00", "2026-11-02T00:00:00"),
            ("monthly", "2024-01-31T10:15:00", "2024-02-01T00:00:00"),
            ("monthly", "2024-02-29T10:15:00", "2024-03-01T00:00:00"),
            ("yearly", "2026-12-31T10:15:00", "2027-01-01T00:00:00"),
        ] {
            let mut rotation =
                schedule(&format!(r#"{{"kind":"frequency","name":"{name}"}}"#), base);
            assert!(!rotation.due_local(date(limit) - TimeDelta::microseconds(1)));
            assert!(rotation.due_local(date(limit)));
            assert!(!rotation.due_local(date(limit)));
        }
    }

    #[test]
    fn weekday_preserves_loguru_first_threshold_then_advances_to_named_day() {
        let mut rotation = schedule(
            r#"{"kind":"daytime","weekday":0,"time_us":50400000000}"#,
            "2026-10-27T10:00:00",
        );
        assert!(rotation.due_local(date("2026-10-27T14:00:00")));
        assert!(!rotation.due_local(date("2026-10-28T14:00:00")));
        assert!(rotation.due_local(date("2026-11-02T14:00:00")));
        let mut rotation = schedule(
            r#"{"kind":"daytime","weekday":0,"time_us":50400000000}"#,
            "2026-10-27T15:00:00",
        );
        assert!(!rotation.due_local(date("2026-10-28T14:00:00")));
        assert!(rotation.due_local(date("2026-11-02T14:00:00")));
    }

    #[test]
    fn local_wall_time_keeps_daylight_saving_gap_and_repeated_hour_semantics() {
        let local = |stamp: &str| {
            DateTime::parse_from_rfc3339(stamp)
                .unwrap()
                .with_timezone(&chrono_tz::America::New_York)
                .naive_local()
        };
        let mut spring = Rotation::local(
            serde_json::from_str(r#"{"kind":"daytime","weekday":null,"time_us":9000000000}"#)
                .unwrap(),
            local("2026-03-08T06:00:00Z"),
        )
        .unwrap();
        assert!(!spring.due_local(local("2026-03-08T06:59:00Z")));
        assert!(spring.due_local(local("2026-03-08T07:00:00Z")));
        assert!(!spring.due_local(local("2026-03-08T07:30:00Z")));
        let mut fall = Rotation::local(
            serde_json::from_str(r#"{"kind":"frequency","name":"hourly"}"#).unwrap(),
            local("2026-11-01T04:30:00Z"),
        )
        .unwrap();
        assert!(fall.due_local(local("2026-11-01T05:00:00Z")));
        assert!(!fall.due_local(local("2026-11-01T06:00:00Z")));
        assert!(fall.due_local(local("2026-11-01T07:00:00Z")));
    }

    #[test]
    fn legacy_utc_buckets_and_invalid_rules_remain_checked() {
        let mut rotation = Rotation::new(None, 3600, 1_000_000).unwrap();
        assert!(!rotation.due(3_599_000_000));
        assert!(rotation.due(3_600_000_000));
        assert!(!rotation.due(3_601_000_000));
        for rule in [
            r#"{"kind":"interval","microseconds":0}"#,
            r#"{"kind":"interval","microseconds":-1}"#,
            r#"{"kind":"daytime","weekday":7,"time_us":0}"#,
            r#"{"kind":"daytime","weekday":null,"time_us":86400000000}"#,
            r#"{"kind":"frequency","name":"sometimes"}"#,
        ] {
            assert!(Rotation::new(Some(rule), 86400, 1_000_000).is_err());
        }
        assert!(Rotation::new(None, 0, 0).is_err());
        assert!(local_datetime(
            chrono_tz::UTC
                .with_ymd_and_hms(2026, 10, 27, 0, 0, 0)
                .unwrap()
                .timestamp_micros() as u64
        )
        .is_some());
    }
}
