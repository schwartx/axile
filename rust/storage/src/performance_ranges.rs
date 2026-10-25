//! 已发布区间的时间边界，查询端共用且不依赖发布写入器。

use rusqlite::{Connection, OptionalExtension};

pub(crate) fn range_bounds(
    connection: &Connection,
    snapshot_id: &str,
    range: &str,
) -> Result<Option<(Option<f64>, Option<f64>)>, String> {
    let row = connection.query_row(
        "SELECT baseline,end FROM performance_result_range WHERE snapshot_id=?1 AND range_key=?2",
        [snapshot_id,range], |row| Ok((row.get::<_,Option<String>>(0)?,row.get::<_,Option<String>>(1)?))
    ).optional().map_err(|error| error.to_string())?;
    row.map(|(lower, upper)| {
        Ok((
            lower
                .as_deref()
                .map(timestamp)
                .transpose()?,
            upper
                .as_deref()
                .map(timestamp)
                .transpose()?,
        ))
    })
    .transpose()
}

fn timestamp(value: &str) -> Result<f64, String> {
    crate::calendar::timestamp_ms(value)
        .map_err(|_| "invalid range timestamp".to_owned())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn range_lookup_preserves_missing_and_null_bounds() {
        let connection = Connection::open_in_memory().unwrap();
        connection.execute_batch("CREATE TABLE performance_result_range(snapshot_id TEXT,range_key TEXT,baseline TEXT,end TEXT); INSERT INTO performance_result_range VALUES('s','empty',NULL,NULL),('s','mixed','2026-01-01T09:00:00.000003','2026-01-01T01:00:00.000003Z');").unwrap();
        assert_eq!(range_bounds(&connection, "s", "missing").unwrap(), None);
        assert_eq!(
            range_bounds(&connection, "s", "empty").unwrap(),
            Some((None, None))
        );
        assert_eq!(
            range_bounds(&connection, "s", "mixed").unwrap(),
            Some((Some(1767229200000.0032), Some(1767229200000.0032)))
        );
        for (lower, upper) in [(Some("invalid"), None), (None, Some("invalid"))] {
            connection.execute("DELETE FROM performance_result_range WHERE range_key='bad'", []).unwrap();
            connection.execute("INSERT OR REPLACE INTO performance_result_range VALUES('s','bad',?1,?2)", (lower,upper)).unwrap();
            let error = range_bounds(&connection, "s", "bad").unwrap_err();
            assert_eq!(error, "invalid range timestamp");
            let public = crate::error::AnalysisError::from(error);
            assert_eq!(public.code, "ANALYSIS_ERROR");
            assert_eq!(public.error, "invalid range timestamp");
        }

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
        assert_eq!(timestamp("invalid").unwrap_err(), "invalid range timestamp");
    }
}
