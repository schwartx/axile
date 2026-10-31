//! Durable queue operations; each writer owns one short IMMEDIATE transaction.
use std::path::Path;

use rusqlite::{params, Connection, OptionalExtension, TransactionBehavior};
use serde_json::{json, Value};

use crate::error::AnalysisError;
use crate::storage_connection::ConnectionLease;

fn database(error: rusqlite::Error) -> AnalysisError {
    AnalysisError::new("DATABASE_QUEUE", error.to_string())
}

fn open(path: &str, write: bool) -> Result<ConnectionLease, AnalysisError> {
    if path.is_empty() || path == ":memory:" {
        return Err(AnalysisError::new(
            "DATABASE_OPEN",
            "Queue requires a file-backed SQLite database",
        ));
    }
    if write {
        crate::storage_connection::open_writer(Path::new(path), true)
    } else {
        crate::storage_connection::open_reader(Path::new(path))
    }
}

fn write<T>(
    path: &str,
    task: impl FnOnce(&Connection) -> rusqlite::Result<T>,
) -> Result<T, AnalysisError> {
    let mut connection = open(path, true)?;
    let transaction = connection
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(database)?;
    let result = task(&transaction).map_err(database)?;
    transaction.commit().map_err(database)?;
    Ok(result)
}

pub fn enqueue(path: &str, account_id: i64, force: bool) -> Result<(), AnalysisError> {
    write(path, |connection| {
        connection.execute(
            "INSERT INTO account_analysis(account_id) VALUES (?1) ON CONFLICT DO NOTHING",
            [account_id],
        )?;
        if force {
            connection.execute("UPDATE account_analysis SET source_version=source_version+1, dirty_record=0, failures=0, retry_at_us=0, error=NULL WHERE account_id=?1 AND running_version IS NULL AND (error IS NOT NULL OR NOT requested)", [account_id])?;
        }
        connection.execute(
            "UPDATE account_analysis SET requested=1 WHERE account_id=?1",
            [account_id],
        )?;
        Ok(())
    })
}

pub fn recover(path: &str, logic: &str, engine: &str) -> Result<(), AnalysisError> {
    write(path, |connection| {
        connection.execute("UPDATE account_analysis SET running_version=NULL, running_token=NULL, requested=1 WHERE running_version IS NOT NULL", [])?;
        connection.execute("UPDATE account_analysis SET source_version=source_version+1,dirty_record=0,requested=1,failures=0,retry_at_us=0,error=NULL WHERE account_id IN (SELECT account_id FROM analysis_state WHERE logic_version!=?1 OR engine_version!=?2)", params![logic, engine])?;
        Ok(())
    })
}

pub fn claim(path: &str, now: f64, token: &str) -> Result<Option<Value>, AnalysisError> {
    if !now.is_finite() || token.is_empty() {
        return Err(AnalysisError::new(
            "INVALID_REQUEST",
            "Claim requires finite now and a token",
        ));
    }
    let now = micros(now)?;
    write(path, |connection| {
        let row = connection.query_row("SELECT account_id, source_version, failures FROM account_analysis WHERE running_version IS NULL AND failures<=3 AND retry_at_us<=?1 AND (requested OR EXISTS(SELECT 1 FROM analysis_state s WHERE s.account_id=account_analysis.account_id AND s.version!=source_version)) ORDER BY retry_at_us, account_id LIMIT 1", [now], |row| Ok((row.get::<_, i64>(0)?, row.get::<_, i64>(1)?, row.get::<_, i64>(2)?))).optional()?;
        let Some((account, version, failures)) = row else {
            return Ok(None);
        };
        let changed = connection.execute("UPDATE account_analysis SET running_version=source_version, running_token=?3, requested=0 WHERE account_id=?1 AND source_version=?2 AND running_version IS NULL", params![account, version, token])?;
        Ok((changed == 1).then(|| json!({"account_id": account, "source_version": version, "running_version": version, "running_token": token, "failures": failures})))
    })
}

pub fn release(path: &str, account: i64, version: i64, token: &str) -> Result<(), AnalysisError> {
    write(path, |connection| {
        connection.execute("UPDATE account_analysis SET running_version=NULL, running_token=NULL, requested=1 WHERE account_id=?1 AND running_version=?2 AND running_token=?3", params![account, version, token])?;
        Ok(())
    })
}

pub fn fail(
    path: &str,
    account: i64,
    version: i64,
    token: &str,
    now: f64,
) -> Result<(), AnalysisError> {
    if !now.is_finite() {
        return Err(AnalysisError::new(
            "INVALID_REQUEST",
            "Failure requires finite now",
        ));
    }
    let now = micros(now)?;
    write(path, |connection| {
        connection.execute("UPDATE account_analysis SET running_version=NULL, running_token=NULL, requested=(failures+1<=3), retry_at_us=?4 + CASE failures WHEN 0 THEN 5000000 WHEN 1 THEN 30000000 ELSE 120000000 END, failures=failures+1, error='绩效计算失败，请重试或查看服务日志' WHERE account_id=?1 AND source_version=?2 AND running_version=?2 AND running_token=?3", params![account, version, token, now])?;
        connection.execute("UPDATE account_analysis SET running_version=NULL, running_token=NULL, requested=1 WHERE account_id=?1 AND source_version!=?2 AND running_version=?2 AND running_token=?3", params![account, version, token])?;
        Ok(())
    })
}

fn micros(seconds: f64) -> Result<i64, AnalysisError> {
    let value = seconds * 1_000_000.0;
    if !value.is_finite() || value < i64::MIN as f64 || value >= i64::MAX as f64 {
        return Err(AnalysisError::new(
            "INVALID_REQUEST",
            "queue time exceeds microsecond range",
        ));
    }
    Ok(value.round() as i64)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn queue_time_is_an_integer_microsecond_and_checks_range() {
        assert_eq!(micros(1.000001).unwrap(), 1_000_001);
        assert_eq!(micros(-0.000001).unwrap(), -1);
        assert!(micros(f64::NAN).is_err());
        assert!(micros(f64::INFINITY).is_err());
        assert!(micros(f64::MAX).is_err());
    }
}
