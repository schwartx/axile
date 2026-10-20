//! Validate immutable per-execution facts without reading raw payloads.
//!
//! Python writes these rows in the same transaction as the original execution.
//! Each computation validates completeness before selecting a consistent range.

use rusqlite::{Connection, OptionalExtension};

fn database_error(error: rusqlite::Error) -> String {
    format!("SOURCE_FACTS_READ: {error}")
}

/// Reject missing or unsupported projections instead of silently dropping facts.
pub fn validate(connection: &Connection, account_id: i64) -> Result<(), String> {
    let invalid = connection
        .query_row(
            "SELECT e.id, s.projection_error
             FROM executerecord e
             LEFT JOIN analysis_source_execution s ON s.record_id=e.id
             WHERE e.account_id=?1 AND (
                   s.record_id IS NULL OR s.account_id IS NOT e.account_id OR s.projection_version!=1
                   OR s.time IS NULL
                   OR s.execution_id IS NOT e.execution_id
                   OR s.created_at IS NOT e.created_at
                   OR s.is_success IS NOT e.is_success
                   OR s.trade_count IS NOT (SELECT COUNT(*) FROM analysis_source_trade t WHERE t.record_id=e.id)
             )
             ORDER BY e.id LIMIT 1",
            [account_id],
            |row| Ok((row.get::<_, i64>(0)?, row.get::<_, Option<String>>(1)?)),
        )
        .optional()
        .map_err(database_error)?;
    if let Some((record_id, reason)) = invalid {
        return Err(format!(
            "SOURCE_FACTS_INCOMPLETE: record {record_id}: {}",
            reason
                .as_deref()
                .unwrap_or("projection is missing, stale, or unsupported")
        ));
    }
    let orphan = connection
        .query_row(
            "SELECT s.record_id FROM analysis_source_execution s
         LEFT JOIN executerecord e ON e.id=s.record_id
         WHERE s.account_id=?1 AND (e.id IS NULL OR e.account_id IS NOT s.account_id)
         ORDER BY s.record_id LIMIT 1",
            [account_id],
            |row| row.get::<_, i64>(0),
        )
        .optional()
        .map_err(database_error)?;
    if let Some(record_id) = orphan {
        return Err(format!(
            "SOURCE_FACTS_INCOMPLETE: orphan projection for record {record_id}"
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    fn database() -> Connection {
        let connection = Connection::open_in_memory().unwrap();
        connection.execute_batch(
            "CREATE TABLE executerecord(id INTEGER PRIMARY KEY,account_id INTEGER,execution_id TEXT,created_at TEXT,is_success INTEGER);
             CREATE TABLE analysis_source_execution(record_id INTEGER PRIMARY KEY,account_id INTEGER,projection_version INTEGER,projection_error TEXT,time REAL,success INTEGER,noop INTEGER,created_at TEXT,execution_id TEXT,is_success INTEGER,trade_count INTEGER);
             CREATE TABLE analysis_source_trade(record_id INTEGER,trade_index INTEGER,day TEXT,time REAL,value REAL,cost REAL,loss_bp REAL,fee REAL,fee_currency TEXT);
             INSERT INTO executerecord VALUES(1,2,'exec-1','2026-01-01T23:59:00',1),(2,2,'exec-2','2026-01-02T09:00:00',1),(3,7,'exec-3','2026-01-01T10:00:00',0);
             INSERT INTO analysis_source_execution VALUES(1,2,1,NULL,100,1,0,'2026-01-01T23:59:00','exec-1',1,0),(2,2,1,NULL,200,1,1,'2026-01-02T09:00:00','exec-2',1,0),(3,7,0,'unsupported',100,0,0,'2026-01-01T10:00:00','exec-3',0,0);"
        ).unwrap();
        connection
    }
    #[test]
    fn source_validation_never_silently_omits_missing_or_invalid_rows() {
        let connection = database();
        validate(&connection, 2).unwrap();
        assert!(validate(&connection, 7)
            .unwrap_err()
            .contains("unsupported"));
        connection
            .execute(
                "DELETE FROM analysis_source_execution WHERE record_id=2",
                [],
            )
            .unwrap();
        assert!(validate(&connection, 2).unwrap_err().contains("record 2"));
    }
    #[test]
    fn source_validation_rejects_stale_headers_truncated_trades_and_orphans() {
        let connection = database();
        connection
            .execute(
                "UPDATE analysis_source_execution SET execution_id='stale' WHERE record_id=1",
                [],
            )
            .unwrap();
        assert!(validate(&connection, 2).unwrap_err().contains("record 1"));
        connection.execute("UPDATE analysis_source_execution SET execution_id='exec-1',trade_count=1 WHERE record_id=1",[]).unwrap();
        assert!(validate(&connection, 2).unwrap_err().contains("record 1"));
        connection
            .execute(
                "INSERT INTO analysis_source_trade(record_id,trade_index) VALUES(1,0)",
                [],
            )
            .unwrap();
        validate(&connection, 2).unwrap();
        connection
            .execute("DELETE FROM executerecord WHERE id=1", [])
            .unwrap();
        assert!(validate(&connection, 2).unwrap_err().contains("orphan"));
    }
    #[test]
    fn source_validation_rejects_cross_account_projection_and_missing_time() {
        let connection = database();
        connection.execute("UPDATE analysis_source_execution SET account_id=2,projection_version=1,projection_error=NULL WHERE record_id=3",[]).unwrap();
        assert!(validate(&connection, 2)
            .unwrap_err()
            .contains("orphan projection for record 3"));
        connection
            .execute(
                "UPDATE analysis_source_execution SET account_id=7 WHERE record_id=3",
                [],
            )
            .unwrap();
        validate(&connection, 2).unwrap();
        connection
            .execute(
                "UPDATE analysis_source_execution SET time=NULL WHERE record_id=1",
                [],
            )
            .unwrap();
        assert!(validate(&connection, 2).unwrap_err().contains("record 1"));
    }
}
