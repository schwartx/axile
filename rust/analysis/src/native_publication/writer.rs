//! One short compare-and-publish transaction on the durable business database.

use rusqlite::{params, Connection, OptionalExtension};
use serde_json::{json, Value};

use crate::error::AnalysisError;

use super::{
    copy, staging,
    task::{settings, Task},
};

fn sql(error: rusqlite::Error) -> AnalysisError {
    AnalysisError::new("PUBLICATION_WRITE", error.to_string())
}

/// Return a previously committed receipt without mutating a newer claim.
pub(super) fn already_published(
    connection: &Connection,
    task: &Task,
) -> Result<Option<Value>, AnalysisError> {
    let existing = connection.query_row(
        "SELECT account_id,source_version,logic_version,engine_version,data_until,read_view_schema_version
         FROM main.performance_snapshot WHERE id=?1",
        [&task.snapshot_id],
        |row| Ok((row.get::<_,i64>(0)?,row.get::<_,i64>(1)?,row.get::<_,String>(2)?,row.get::<_,String>(3)?,row.get::<_,Option<String>>(4)?,row.get::<_,i64>(5)?)),
    ).optional().map_err(sql)?;
    let Some((account, version, logic, engine, until, view)) = existing else {
        return Ok(None);
    };
    if account != task.account_id
        || version != task.source_version
        || logic != task.logic_version
        || engine != task.engine_version
        || view != 1
    {
        return Err(AnalysisError::new(
            "SNAPSHOT_CONFLICT",
            "snapshot identity is already in use",
        ));
    }
    Ok(Some(task.receipt(true, json!(until), None)))
}

/// This is checked once in the reader and again while holding the writer slot.
pub(super) fn stale_reason(
    connection: &Connection,
    task: &Task,
) -> Result<Option<&'static str>, AnalysisError> {
    let state = connection
        .query_row(
            "SELECT q.source_version,q.running_version,q.running_token,a.trade_channel,s.backtest
         FROM main.account a LEFT JOIN main.account_analysis q ON q.account_id=a.id
         LEFT JOIN main.account_settings s ON s.account_id=a.id WHERE a.id=?1",
            [task.account_id],
            |row| {
                Ok((
                    row.get::<_, Option<i64>>(0)?,
                    row.get::<_, Option<i64>>(1)?,
                    row.get::<_, Option<String>>(2)?,
                    row.get::<_, String>(3)?,
                    row.get::<_, Option<String>>(4)?,
                ))
            },
        )
        .optional()
        .map_err(sql)?;
    let Some((source, running, token, channel, raw_settings)) = state else {
        return Ok(Some("account_missing"));
    };
    if source != Some(task.source_version) {
        return Ok(Some("source_changed"));
    }
    if running != Some(task.source_version) || token.as_deref() != Some(task.claim_token.as_str()) {
        return Ok(Some("claim_lost"));
    }
    let current_settings = match raw_settings {
        Some(value) => serde_json::from_str::<Value>(&value).map_err(|_| {
            AnalysisError::new(
                "INVALID_ACCOUNT_SETTINGS",
                "invalid stored backtest settings",
            )
        })?,
        None => json!({"backtest_weight_type":"ts","backtest_fee_rate":0.0}),
    };
    let (weight, fee) = settings(&current_settings)?;
    if channel != task.trade_channel || weight != task.weight_type || fee != task.fee_rate {
        return Ok(Some("source_changed"));
    }
    Ok(None)
}

fn release_stale(connection: &Connection, task: &Task, reason: &str) -> Result<(), AnalysisError> {
    if reason == "source_changed" {
        connection.execute(
            "UPDATE main.account_analysis SET running_version=NULL,running_token=NULL,requested=1
             WHERE account_id=?1 AND running_version=?2 AND running_token=?3",
            params![task.account_id,task.source_version,task.claim_token],
        ).map_err(sql)?;
    }
    Ok(())
}

pub(super) fn skip(
    connection: &Connection,
    task: &Task,
    initial_reason: &str,
) -> Result<Value, AnalysisError> {
    transaction(connection, || {
        if let Some(receipt) = already_published(connection, task)? {
            return Ok(receipt);
        }
        let reason = stale_reason(connection, task)?.unwrap_or(initial_reason);
        release_stale(connection, task, reason)?;
        Ok(task.receipt(false, Value::Null, Some(reason)))
    })
}

pub(super) fn publish(
    connection: &Connection,
    task: &Task,
    complete: &Value,
) -> Result<Value, AnalysisError> {
    transaction(connection, || {
        if let Some(receipt) = already_published(connection, task)? {
            return Ok(receipt);
        }
        if let Some(reason) = stale_reason(connection, task)? {
            release_stale(connection, task, reason)?;
            return Ok(task.receipt(false, Value::Null, Some(reason)));
        }
        let previous: Option<String> = connection
            .query_row(
                "SELECT current_snapshot FROM main.account_analysis WHERE account_id=?1",
                [task.account_id],
                |row| row.get(0),
            )
            .map_err(sql)?;
        snapshot(connection, task, complete)?;
        staging::copy_results(connection, &task.snapshot_id)?;
        copy::ranges(connection, &task.snapshot_id)?;
        copy::source_facts(connection, &task.snapshot_id, task.account_id)?;
        validate_view(connection, task)?;
        publish_pointer(connection, task)?;
        cleanup(connection, task, previous.as_deref())?;
        let mut receipt = task.receipt(true, complete["data_until"].clone(), None);
        receipt["row_counts"] = complete["row_counts"].clone();
        Ok(receipt)
    })
}

fn transaction(
    connection: &Connection,
    work: impl FnOnce() -> Result<Value, AnalysisError>,
) -> Result<Value, AnalysisError> {
    connection.execute_batch("BEGIN IMMEDIATE;").map_err(sql)?;
    match work() {
        Ok(value) => {
            if let Err(error) = connection.execute_batch("COMMIT;") {
                let _ = connection.execute_batch("ROLLBACK;");
                return Err(sql(error));
            }
            Ok(value)
        }
        Err(error) => {
            let _ = connection.execute_batch("ROLLBACK;");
            Err(error)
        }
    }
}

fn snapshot(connection: &Connection, task: &Task, complete: &Value) -> Result<(), AnalysisError> {
    let until = complete["data_until"].as_str();
    connection.execute(
        "INSERT INTO main.performance_snapshot
         (id,account_id,source_version,logic_version,engine_version,computed_at,data_until,settings,ranges,
          cost_schema_version,result_schema_version,backtest_weight_type,backtest_fee_rate,read_view_schema_version)
         VALUES (?1,?2,?3,?4,?5,?6,?7,'{}','{}',1,2,?8,?9,1)",
        params![task.snapshot_id,task.account_id,task.source_version,task.logic_version,task.engine_version,task.computed_at,until,task.weight_type,task.fee_rate],
    ).map_err(sql)?;
    Ok(())
}

fn publish_pointer(connection: &Connection, task: &Task) -> Result<(), AnalysisError> {
    let changed = connection.execute(
        "UPDATE main.account_analysis SET previous_snapshot=current_snapshot,current_snapshot=?1,published_version=?2,
         running_version=NULL,running_token=NULL,requested=0,failures=0,retry_at=0,error=NULL,logic_version=?3,engine_version=?4
         WHERE account_id=?5 AND source_version=?2 AND running_version=?2 AND running_token=?6",
        params![task.snapshot_id,task.source_version,task.logic_version,task.engine_version,task.account_id,task.claim_token],
    ).map_err(sql)?;
    if changed != 1 {
        return Err(AnalysisError::new(
            "CLAIM_LOST",
            "publication claim changed inside writer transaction",
        ));
    }
    Ok(())
}

fn validate_view(connection: &Connection, task: &Task) -> Result<(), AnalysisError> {
    let complete: bool = connection.query_row(
        "SELECT
           (SELECT COUNT(*) FROM main.performance_execution_summary WHERE snapshot_id=?1)
           =(SELECT COUNT(*) FROM main.performance_cost_execution WHERE snapshot_id=?1)
           AND NOT EXISTS (
               SELECT 1 FROM main.performance_execution_summary s
               LEFT JOIN main.performance_cost_execution e ON e.snapshot_id=s.snapshot_id AND e.record_id=s.record_id
               WHERE s.snapshot_id=?1 AND (e.record_id IS NULL OR s.time IS NOT e.time)
           )
           AND NOT EXISTS (
               SELECT 1 FROM main.performance_execution_fee f
               LEFT JOIN main.performance_execution_summary s ON s.snapshot_id=f.snapshot_id AND s.record_id=f.record_id
               WHERE f.snapshot_id=?1 AND (s.record_id IS NULL OR f.currency='')
           )",
        [&task.snapshot_id], |row| row.get(0),
    ).map_err(sql)?;
    if !complete {
        return Err(AnalysisError::new(
            "INVALID_NATIVE_PUBLICATION",
            "execution read view is incomplete",
        ));
    }
    Ok(())
}

fn cleanup(
    connection: &Connection,
    task: &Task,
    previous: Option<&str>,
) -> Result<(), AnalysisError> {
    let obsolete = "SELECT id FROM main.performance_snapshot WHERE account_id=?1 AND id!=?2 AND (?3 IS NULL OR id!=?3)";
    for name in staging::RESULT_TABLES
        .iter()
        .chain(copy::COST_TABLES.iter())
    {
        let query = format!("DELETE FROM main.\"{name}\" WHERE snapshot_id IN ({obsolete})");
        connection
            .execute(&query, params![task.account_id, task.snapshot_id, previous])
            .map_err(sql)?;
    }
    let query = format!("DELETE FROM main.performance_snapshot WHERE id IN ({obsolete})");
    connection
        .execute(&query, params![task.account_id, task.snapshot_id, previous])
        .map_err(sql)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn task() -> Task {
        Task::parse(
            &json!({"command":"publish_store","protocol":1,"account_id":1,
            "expected_source_version":2,"snapshot_id":"next","claim_token":"next",
            "logic_version":"18","engine_version":"equity-ledger-1",
            "computed_at":"2026-10-20T10:00:00+08:00","trade_channel":"gm",
            "settings":{"backtest_weight_type":"ts","backtest_fee_rate":0.0}}),
        )
        .unwrap()
    }

    fn fixture() -> Connection {
        let connection = Connection::open_in_memory().unwrap();
        connection.execute_batch(
            "PRAGMA foreign_keys=ON;
             CREATE TABLE account(id INTEGER PRIMARY KEY,trade_channel TEXT);
             CREATE TABLE account_settings(account_id INTEGER PRIMARY KEY,backtest TEXT);
             CREATE TABLE account_analysis(account_id INTEGER PRIMARY KEY,source_version INTEGER,running_version INTEGER,
                 running_token TEXT,current_snapshot TEXT,previous_snapshot TEXT,published_version INTEGER,
                 requested BOOLEAN,failures INTEGER,retry_at REAL,error TEXT,logic_version TEXT,engine_version TEXT);
             CREATE TABLE performance_snapshot(id TEXT PRIMARY KEY,account_id INTEGER,source_version INTEGER,
                 logic_version TEXT,engine_version TEXT,computed_at TEXT,data_until TEXT,settings TEXT,ranges TEXT,
                 cost_schema_version INTEGER,result_schema_version INTEGER,backtest_weight_type TEXT,
                 backtest_fee_rate REAL,read_view_schema_version INTEGER);
             INSERT INTO account VALUES(1,'gm');
             INSERT INTO account_settings VALUES(1,'{\"backtest_weight_type\":\"ts\",\"backtest_fee_rate\":0}');
             INSERT INTO account_analysis(account_id,source_version,running_version,running_token,current_snapshot,requested)
                 VALUES(1,2,2,'next','old',1);"
        ).unwrap();
        connection
    }

    #[test]
    fn source_change_requeues_only_the_matching_token() {
        let connection = fixture();
        connection
            .execute("UPDATE account_analysis SET source_version=3", [])
            .unwrap();
        let receipt = skip(&connection, &task(), "source_changed").unwrap();
        assert_eq!(receipt["published"], false);
        assert_eq!(receipt["reason"], "source_changed");
        let running: Option<i64> = connection
            .query_row("SELECT running_version FROM account_analysis", [], |row| {
                row.get(0)
            })
            .unwrap();
        assert_eq!(running, None);
        connection
            .execute(
                "UPDATE account_analysis SET running_version=3,running_token='newer'",
                [],
            )
            .unwrap();
        skip(&connection, &task(), "source_changed").unwrap();
        let token: String = connection
            .query_row("SELECT running_token FROM account_analysis", [], |row| {
                row.get(0)
            })
            .unwrap();
        assert_eq!(token, "newer");
    }

    #[test]
    fn settings_channel_account_and_claim_are_checked() {
        let connection = fixture();
        assert_eq!(stale_reason(&connection, &task()).unwrap(), None);
        connection
            .execute("UPDATE account SET trade_channel='ctp'", [])
            .unwrap();
        assert_eq!(
            stale_reason(&connection, &task()).unwrap(),
            Some("source_changed")
        );
        connection
            .execute("UPDATE account SET trade_channel='gm'", [])
            .unwrap();
        connection.execute("UPDATE account_settings SET backtest='{\"backtest_weight_type\":\"cs\",\"backtest_fee_rate\":0}'", []).unwrap();
        assert_eq!(
            stale_reason(&connection, &task()).unwrap(),
            Some("source_changed")
        );
        connection
            .execute("UPDATE account_analysis SET running_token='other'", [])
            .unwrap();
        assert_eq!(
            stale_reason(&connection, &task()).unwrap(),
            Some("claim_lost")
        );
        connection.execute("DELETE FROM account", []).unwrap();
        assert_eq!(
            stale_reason(&connection, &task()).unwrap(),
            Some("account_missing")
        );
        let receipt = skip(&connection, &task(), "account_missing").unwrap();
        assert_eq!(receipt["reason"], "account_missing");
        assert_eq!(
            connection
                .query_row("SELECT COUNT(*) FROM account", [], |row| row
                    .get::<_, i64>(0))
                .unwrap(),
            0
        );
    }

    #[test]
    fn a_copy_failure_rolls_back_snapshot_and_already_copied_rows() {
        let connection = fixture();
        connection
            .execute_batch("ATTACH DATABASE '' AS native_stage;")
            .unwrap();
        for name in &staging::RESULT_TABLES[..2] {
            connection.execute_batch(&format!(
                "CREATE TABLE main.\"{name}\" (snapshot_id TEXT NOT NULL,range_key TEXT NOT NULL,PRIMARY KEY(snapshot_id,range_key));
                 CREATE TABLE native_stage.\"{name}\" (range_key TEXT NOT NULL);
                 INSERT INTO native_stage.\"{name}\" VALUES('all');"
            )).unwrap();
        }
        let error = publish(
            &connection,
            &task(),
            &json!({"data_until":null,"row_counts":{}}),
        )
        .unwrap_err();
        assert_eq!(error.code, "SCHEMA_INCOMPATIBLE");
        assert_eq!(
            connection
                .query_row("SELECT COUNT(*) FROM performance_snapshot", [], |row| row
                    .get::<_, i64>(
                    0
                ))
                .unwrap(),
            0
        );
        assert_eq!(
            connection
                .query_row(
                    "SELECT COUNT(*) FROM performance_result_daily_fee",
                    [],
                    |row| row.get::<_, i64>(0)
                )
                .unwrap(),
            0
        );
        let state: (String, String) = connection
            .query_row(
                "SELECT current_snapshot,running_token FROM account_analysis",
                [],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .unwrap();
        assert_eq!(state, ("old".into(), "next".into()));
        assert!(connection.is_autocommit());
    }

    #[test]
    fn a_committed_snapshot_is_idempotent_even_after_a_new_claim() {
        let connection = fixture();
        snapshot(
            &connection,
            &task(),
            &json!({"data_until":"2026-10-20T09:00:00"}),
        )
        .unwrap();
        connection.execute("UPDATE account_analysis SET source_version=3,running_version=3,running_token='newer',current_snapshot='next'", []).unwrap();
        let receipt = publish(&connection, &task(), &json!({})).unwrap();
        assert_eq!(receipt["published"], true);
        let token: String = connection
            .query_row("SELECT running_token FROM account_analysis", [], |row| {
                row.get(0)
            })
            .unwrap();
        assert_eq!(token, "newer");
    }

    #[test]
    fn cleanup_keeps_current_previous_and_other_accounts() {
        let connection = fixture();
        for name in staging::RESULT_TABLES
            .iter()
            .chain(copy::COST_TABLES.iter())
        {
            connection.execute_batch(&format!(
                "CREATE TABLE main.\"{name}\" (snapshot_id TEXT NOT NULL REFERENCES performance_snapshot(id));"
            )).unwrap();
        }
        for (id, account) in [("ancient", 1), ("old", 1), ("next", 1), ("unrelated", 2)] {
            connection
                .execute(
                    "INSERT INTO performance_snapshot(id,account_id) VALUES(?1,?2)",
                    params![id, account],
                )
                .unwrap();
            for name in staging::RESULT_TABLES
                .iter()
                .chain(copy::COST_TABLES.iter())
            {
                connection
                    .execute(&format!("INSERT INTO \"{name}\" VALUES(?1)"), [id])
                    .unwrap();
            }
        }
        transaction(&connection, || {
            cleanup(&connection, &task(), Some("old"))?;
            Ok(Value::Null)
        })
        .unwrap();
        assert_eq!(
            connection
                .query_row("SELECT COUNT(*) FROM performance_snapshot", [], |row| row
                    .get::<_, i64>(
                    0
                ))
                .unwrap(),
            3
        );
        for name in staging::RESULT_TABLES
            .iter()
            .chain(copy::COST_TABLES.iter())
        {
            assert_eq!(
                connection
                    .query_row(&format!("SELECT COUNT(*) FROM \"{name}\""), [], |row| row
                        .get::<_, i64>(
                        0
                    ))
                    .unwrap(),
                3
            );
        }
        assert!(connection
            .prepare("PRAGMA foreign_key_check")
            .unwrap()
            .query([])
            .unwrap()
            .next()
            .unwrap()
            .is_none());
    }
}
