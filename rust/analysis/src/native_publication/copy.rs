//! Copy frozen scalar source facts without materializing historical rows.

use chrono::{DateTime, NaiveDateTime};
use rusqlite::{Connection, OptionalExtension};

use crate::error::AnalysisError;

pub(super) const COST_TABLES: &[&str] = &[
    "performance_cost_transaction_fee",
    "performance_cost_transaction",
    "performance_cost_attempt",
    "performance_cost_position",
    "performance_cost_execution_symbol",
    "performance_cost_trade",
    "performance_cost_execution",
];

pub(crate) fn range_bounds(connection: &Connection, snapshot_id: &str, range: &str) -> Result<Option<(Option<f64>, Option<f64>)>, String> {
    let row = connection.query_row(
        "SELECT baseline,end FROM performance_result_range WHERE snapshot_id=?1 AND range_key=?2",
        [snapshot_id,range], |row| Ok((row.get::<_,Option<String>>(0)?,row.get::<_,Option<String>>(1)?))
    ).optional().map_err(|error| error.to_string())?;
    row.map(|(lower,upper)| Ok((lower.as_deref().map(timestamp).transpose().map_err(|e|e.to_string())?,
        upper.as_deref().map(timestamp).transpose().map_err(|e|e.to_string())?))).transpose()
}

fn timestamp(value: &str) -> Result<f64, AnalysisError> {
    if let Ok(time) = DateTime::parse_from_rfc3339(value) {
        return Ok(time.timestamp_micros() as f64 / 1_000_000.0 * 1000.0);
    }
    let parsed = NaiveDateTime::parse_from_str(value, "%Y-%m-%dT%H:%M:%S%.f")
        .map_err(|_| AnalysisError::new("INVALID_RESULT_TIME", "invalid range timestamp"))?;
    Ok((parsed.and_utc().timestamp_micros() - 28_800_000_000) as f64 / 1_000_000.0 * 1000.0)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn range_lookup_preserves_missing_and_null_bounds() {
        let connection = Connection::open_in_memory().unwrap();
        connection.execute_batch("CREATE TABLE performance_result_range(snapshot_id TEXT,range_key TEXT,baseline TEXT,end TEXT); INSERT INTO performance_result_range VALUES('s','empty',NULL,NULL),('s','mixed','2026-01-01T09:00:00.000003','2026-01-01T01:00:00.000003Z');").unwrap();
        assert_eq!(range_bounds(&connection,"s","missing").unwrap(),None);
        assert_eq!(range_bounds(&connection,"s","empty").unwrap(),Some((None,None)));
        assert_eq!(range_bounds(&connection,"s","mixed").unwrap(),Some((Some(1767229200000.0032),Some(1767229200000.0032))));
    }

    #[test]
    fn range_boundaries_match_historical_microsecond_precision() {
        let expected = 1767229200000.0032;
        assert_eq!(timestamp("2026-01-01T09:00:00.000003").unwrap(), expected);
        assert_eq!(
            timestamp("2026-01-01T09:00:00.000003+08:00").unwrap(),
            expected
        );
        assert_eq!(timestamp("2026-01-01T01:00:00.000003Z").unwrap(), expected);
        assert!(timestamp("invalid").is_err());
    }
}
