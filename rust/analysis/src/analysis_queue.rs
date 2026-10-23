//! Durable queue operations; each writer owns one short IMMEDIATE transaction.
use std::time::Duration;

use rusqlite::{params, Connection, OpenFlags, OptionalExtension, TransactionBehavior};
use serde_json::{json, Value};

use crate::error::AnalysisError;

fn database(error: rusqlite::Error) -> AnalysisError {
    AnalysisError::new("DATABASE_QUEUE", error.to_string())
}

fn open(path: &str, write: bool) -> Result<Connection, AnalysisError> {
    if path.is_empty() || path == ":memory:" {
        return Err(AnalysisError::new(
            "DATABASE_OPEN",
            "Queue requires a file-backed SQLite database",
        ));
    }
    let flags = if write {
        OpenFlags::SQLITE_OPEN_READ_WRITE
    } else {
        OpenFlags::SQLITE_OPEN_READ_ONLY
    };
    let connection = Connection::open_with_flags(path, flags | OpenFlags::SQLITE_OPEN_NO_MUTEX)
        .map_err(|error| AnalysisError::new("DATABASE_OPEN", error.to_string()))?;
    connection
        .busy_timeout(Duration::from_secs(5))
        .map_err(database)?;
    connection
        .execute_batch("PRAGMA foreign_keys=ON;")
        .map_err(database)?;
    Ok(connection)
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
            connection.execute("UPDATE account_analysis SET source_version=source_version+1, failures=0, retry_at=0, error=NULL WHERE account_id=?1 AND running_version IS NULL AND (error IS NOT NULL OR (published_version=source_version AND NOT requested))", [account_id])?;
        }
        connection.execute(
            "UPDATE account_analysis SET requested=1 WHERE account_id=?1",
            [account_id],
        )?;
        Ok(())
    })
}

pub fn status(
    path: &str,
    account_id: i64,
    logic: &str,
    engine: &str,
) -> Result<String, AnalysisError> {
    let connection = open(path, false)?;
    let result = connection.query_row(
        "SELECT p.id, p.logic_version, p.engine_version, s.error, s.requested OR s.running_version IS NOT NULL, s.published_version=s.source_version FROM account_analysis s LEFT JOIN performance_snapshot p ON p.id=s.current_snapshot WHERE s.account_id=?1",
        [account_id], |row| {
            let id: Option<String> = row.get(0)?;
            let stored_logic: Option<String> = row.get(1)?;
            let stored_engine: Option<String> = row.get(2)?;
            let error: Option<String> = row.get(3)?;
            Ok(crate::snapshots::publication_status(id.as_deref(), stored_logic.as_deref()==Some(logic) && stored_engine.as_deref()==Some(engine), error.as_deref(), row.get(4)?, row.get(5)?).to_owned())
        }).optional().map_err(database)?;
    Ok(result.unwrap_or_else(|| "empty".to_owned()))
}

pub fn recover(path: &str, logic: &str, engine: &str) -> Result<(), AnalysisError> {
    write(path, |connection| {
        connection.execute("UPDATE account_analysis SET running_version=NULL, running_token=NULL, requested=1 WHERE running_version IS NOT NULL", [])?;
        connection.execute("UPDATE account_analysis SET source_version=source_version+1, requested=1, failures=0, retry_at=0, error=NULL WHERE current_snapshot IS NOT NULL AND (logic_version IS NULL OR engine_version IS NULL OR logic_version!=?1 OR engine_version!=?2)", params![logic, engine])?;
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
    write(path, |connection| {
        let row = connection.query_row("SELECT account_id, source_version, failures FROM account_analysis WHERE running_version IS NULL AND failures<=3 AND retry_at<=?1 AND (requested OR (current_snapshot IS NOT NULL AND source_version!=published_version)) ORDER BY retry_at, account_id LIMIT 1", [now], |row| Ok((row.get::<_, i64>(0)?, row.get::<_, i64>(1)?, row.get::<_, i64>(2)?))).optional()?;
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
    write(path, |connection| {
        connection.execute("UPDATE account_analysis SET running_version=NULL, running_token=NULL, requested=(failures+1<=3), retry_at=?4 + CASE failures WHEN 0 THEN 5 WHEN 1 THEN 30 ELSE 120 END, failures=failures+1, error='绩效计算失败，请重试或查看服务日志' WHERE account_id=?1 AND source_version=?2 AND running_version=?2 AND running_token=?3", params![account, version, token, now])?;
        connection.execute("UPDATE account_analysis SET running_version=NULL, running_token=NULL, requested=1 WHERE account_id=?1 AND source_version!=?2 AND running_version=?2 AND running_token=?3", params![account, version, token])?;
        Ok(())
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn queue_waits_for_committed_source_changes() {
        use std::sync::mpsc;
        use std::thread;

        for operation in ["claim", "fail", "release"] {
            let path = std::env::temp_dir().join(format!(
                "axile-queue-contention-{}-{operation}.sqlite",
                std::process::id()
            ));
            let mut writer = Connection::open(&path).unwrap();
            writer.busy_timeout(Duration::from_secs(2)).unwrap();
            writer
                .execute_batch(
                    "CREATE TABLE account_analysis(account_id INTEGER PRIMARY KEY,
                source_version INTEGER DEFAULT 1, published_version INTEGER DEFAULT 0,
                current_snapshot TEXT, requested INTEGER DEFAULT 1, running_version INTEGER,
                running_token TEXT, failures INTEGER DEFAULT 0, retry_at REAL DEFAULT 0,
                error TEXT);
                INSERT INTO account_analysis(account_id) VALUES (1);",
                )
                .unwrap();
            if operation != "claim" {
                claim(path.to_str().unwrap(), 100.0, "old")
                    .unwrap()
                    .unwrap();
            }
            let transaction = writer
                .transaction_with_behavior(TransactionBehavior::Immediate)
                .unwrap();
            transaction
                .execute(
                    "UPDATE account_analysis SET source_version=2, requested=1",
                    [],
                )
                .unwrap();
            let (started_tx, started_rx) = mpsc::channel();
            let (done_tx, done_rx) = mpsc::channel();
            let worker_path = path.clone();
            let worker = thread::spawn(move || {
                started_tx.send(()).unwrap();
                let path = worker_path.to_str().unwrap();
                let result = match operation {
                    "claim" => claim(path, 100.0, "new"),
                    "fail" => fail(path, 1, 1, "old", 100.0).map(|()| None),
                    _ => release(path, 1, 1, "old").map(|()| None),
                };
                done_tx.send(result).unwrap();
            });
            started_rx.recv_timeout(Duration::from_secs(2)).unwrap();
            assert!(matches!(
                done_rx.recv_timeout(Duration::from_millis(100)),
                Err(mpsc::RecvTimeoutError::Timeout)
            ));
            transaction.commit().unwrap();
            let result = done_rx
                .recv_timeout(Duration::from_secs(6))
                .unwrap()
                .unwrap();
            worker.join().unwrap();
            let next = if operation == "claim" {
                result.unwrap()
            } else {
                claim(path.to_str().unwrap(), 100.0, "new")
                    .unwrap()
                    .unwrap()
            };
            assert_eq!(next["source_version"], 2);
            assert_eq!(next["failures"], 0);
            // An old claim cannot clear or penalize the new owner's source.
            fail(path.to_str().unwrap(), 1, 1, "old", 100.0).unwrap();
            release(path.to_str().unwrap(), 1, 1, "old").unwrap();
            let state: (i64, String, i64, f64, Option<String>) = writer.query_row(
                "SELECT running_version, running_token, failures, retry_at, error FROM account_analysis",
                [], |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?, row.get(4)?)),
            ).unwrap();
            assert_eq!(state, (2, "new".to_owned(), 0, 0.0, None));
            drop(writer);
            std::fs::remove_file(path).unwrap();
        }
    }

    #[test]
    fn queue_lifecycle_and_missing_file() {
        let directory =
            std::env::temp_dir().join(format!("axile-queue-test-{}", std::process::id()));
        std::fs::create_dir_all(&directory).unwrap();
        let path = directory.join("queue.sqlite");
        let path = path.to_str().unwrap();
        assert_eq!(enqueue(path, 1, false).unwrap_err().code, "DATABASE_OPEN");
        assert!(!std::path::Path::new(path).exists());
        let connection = Connection::open(path).unwrap();
        connection.execute_batch("CREATE TABLE account_analysis(account_id INTEGER PRIMARY KEY,
            source_version INTEGER NOT NULL DEFAULT 1, published_version INTEGER NOT NULL DEFAULT 0,
            current_snapshot TEXT, requested INTEGER NOT NULL DEFAULT 0, running_version INTEGER,
            running_token TEXT, failures INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
            error TEXT, logic_version TEXT, engine_version TEXT);
            CREATE TABLE performance_snapshot(id TEXT PRIMARY KEY, logic_version TEXT, engine_version TEXT);").unwrap();
        enqueue(path, 1, false).unwrap();
        enqueue(path, 1, true).unwrap();
        assert_eq!(status(path, 1, "18", "1").unwrap(), "pending");
        let first = claim(path, 100.0, "a").unwrap().unwrap();
        assert_eq!(first["source_version"], 1);
        assert!(claim(path, 100.0, "b").unwrap().is_none());
        fail(path, 1, 1, "expired", 100.0).unwrap();
        assert!(claim(path, 100.0, "b").unwrap().is_none());
        fail(path, 1, 1, "a", 100.0).unwrap();
        assert!(claim(path, 104.0, "b").unwrap().is_none());
        assert_eq!(claim(path, 105.0, "b").unwrap().unwrap()["failures"], 1);
        recover(path, "18", "1").unwrap();
        assert!(claim(path, 105.0, "c").unwrap().is_some());
        release(path, 1, 1, "b").unwrap();
        assert!(claim(path, 105.0, "d").unwrap().is_none());
        fail(path, 1, 1, "c", 105.0).unwrap();
        assert!(claim(path, 134.0, "d").unwrap().is_none());
        claim(path, 135.0, "d").unwrap().unwrap();
        fail(path, 1, 1, "d", 135.0).unwrap();
        claim(path, 255.0, "e").unwrap().unwrap();
        fail(path, 1, 1, "e", 255.0).unwrap();
        assert!(claim(path, 1000.0, "f").unwrap().is_none());
        enqueue(path, 1, true).unwrap();
        assert_eq!(
            claim(path, 1000.0, "f").unwrap().unwrap()["source_version"],
            2
        );
        connection
            .execute(
                "UPDATE account_analysis SET source_version=3, failures=0",
                [],
            )
            .unwrap();
        fail(path, 1, 2, "f", 1000.0).unwrap();
        assert_eq!(claim(path, 1000.0, "g").unwrap().unwrap()["failures"], 0);
        connection
            .execute(
                "UPDATE account_analysis SET current_snapshot='old', failures=4",
                [],
            )
            .unwrap();
        recover(path, "18", "1").unwrap();
        assert_eq!(
            claim(path, 1000.0, "h").unwrap().unwrap()["source_version"],
            4
        );
        drop(connection);
        std::fs::remove_dir_all(directory).unwrap();
    }
}
