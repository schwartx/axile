//! 0039 replaces nineteen result tables with current typed state and daily results.
use super::{sql, structured};
use crate::{analysis, error::AnalysisError};
use rusqlite::Connection;
const REMOVED: &[&str] = &[
    "performance_contribution",
    "performance_cost_transaction_fee",
    "performance_cost_transaction",
    "performance_cost_attempt",
    "performance_cost_position",
    "performance_cost_execution_symbol",
    "performance_execution_fee",
    "performance_result_daily_fee",
    "performance_result_daily_cost",
    "performance_result_event",
    "performance_result_calendar_range",
    "performance_result_binding",
    "performance_result_fallback",
    "performance_result_skip",
    "performance_result_point",
    "performance_result_range",
    "performance_cost_trade",
    "performance_cost_execution",
    "performance_snapshot",
];
const COLUMNS: &[(&str, &str)] = &[
    ("published_version", "INTEGER NOT NULL DEFAULT 0"),
    ("current_snapshot", "TEXT"),
    ("previous_snapshot", "TEXT"),
    ("logic_version", "TEXT"),
    ("engine_version", "TEXT"),
];
fn triggers(c: &Connection, upgrade: bool) -> Result<(), AnalysisError> {
    let rows=c.prepare("SELECT name,sql FROM sqlite_schema WHERE type='trigger' AND sql LIKE '%account_analysis%'").map_err(sql)?.query_map([],|r|Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?))).map_err(sql)?.collect::<rusqlite::Result<Vec<_>>>().map_err(sql)?;
    let queue = regex::Regex::new(r"requested\s*=\s*CASE\b[\s\S]*?\bEND").unwrap();
    for (name, body) in rows {
        c.execute_batch(&format!("DROP TRIGGER \"{}\"", name.replace('"', "\"\"")))
            .map_err(sql)?;
        if name == "analysis_account_delete" {
            continue;
        }
        let body = if upgrade {
            queue.replace_all(&body, "requested=1").into_owned()
        } else {
            body.replace("requested=1,failures=0,retry_at=0,error=NULL","requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL THEN 1 ELSE requested END,failures=0,retry_at=0,error=NULL")
        };
        c.execute_batch(&body).map_err(sql)?;
    }
    Ok(())
}
pub(super) fn upgrade(c: &Connection) -> Result<(), AnalysisError> {
    triggers(c, true)?;
    c.execute_batch("DROP TRIGGER IF EXISTS analysis_snapshot_delete")
        .map_err(sql)?;
    for table in REMOVED {
        c.execute_batch(&format!("DROP TABLE {table}"))
            .map_err(sql)?;
    }
    for (column, _) in COLUMNS {
        c.execute_batch(&format!(
            "ALTER TABLE account_analysis DROP COLUMN {column}"
        ))
        .map_err(sql)?;
    }
    c.execute_batch("ALTER TABLE account_analysis ADD COLUMN dirty_record INTEGER; CREATE INDEX ix_business_trade_identity ON business_trade(trade_id,record_id);UPDATE account_analysis SET requested=1,running_version=NULL,running_token=NULL,failures=0,retry_at=0,error=NULL").map_err(sql)?;
    c.execute_batch(include_str!("../analysis/schema.sql"))
        .map_err(sql)?;
    let cleanup = analysis::TABLES
        .iter()
        .map(|t| format!("DELETE FROM {t} WHERE account_id=OLD.id;"))
        .collect::<String>();
    c.execute_batch(&format!("CREATE TRIGGER analysis_account_delete AFTER DELETE ON account BEGIN {cleanup} DELETE FROM account_analysis WHERE account_id=OLD.id; END")).map_err(sql)?;
    c.execute_batch("CREATE TRIGGER analysis_channel_change AFTER UPDATE OF trade_channel ON account WHEN OLD.trade_channel IS NOT NEW.trade_channel BEGIN UPDATE account_analysis SET dirty_record=0 WHERE account_id=NEW.id; END").map_err(sql)?;
    structured::install_incremental(c)
}
pub(super) fn downgrade(c: &Connection) -> Result<(), AnalysisError> {
    let names=c.prepare("SELECT name FROM sqlite_schema WHERE type='trigger' AND (name GLOB 'incremental_*' OR name='analysis_channel_change')").map_err(sql)?.query_map([],|r|r.get::<_,String>(0)).map_err(sql)?.collect::<rusqlite::Result<Vec<_>>>().map_err(sql)?;
    for name in names {
        c.execute_batch(&format!("DROP TRIGGER {name}"))
            .map_err(sql)?;
    }
    c.execute_batch("DROP TRIGGER analysis_account_delete;DROP INDEX ix_business_trade_identity;ALTER TABLE account_analysis DROP COLUMN dirty_record").map_err(sql)?;
    for table in analysis::TABLES {
        c.execute_batch(&format!("DROP TABLE {table}"))
            .map_err(sql)?;
    }
    for (column, kind) in COLUMNS {
        c.execute_batch(&format!(
            "ALTER TABLE account_analysis ADD COLUMN {column} {kind}"
        ))
        .map_err(sql)?;
    }
    let schema = include_str!("structured/analysis.sql");
    c.execute_batch(&schema[..schema.find("CREATE TABLE analysis_calendar_day").unwrap()])
        .map_err(sql)?;
    c.execute_batch(include_str!("contributions.sql"))
        .map_err(sql)?;
    triggers(c, false)?;
    c.execute_batch("CREATE TRIGGER analysis_account_delete AFTER DELETE ON account BEGIN DELETE FROM account_analysis WHERE account_id=OLD.id;DELETE FROM performance_snapshot WHERE account_id=OLD.id;END").map_err(sql)?;
    structured::restore_analysis_triggers(c)?;
    c.execute_batch("UPDATE account_analysis SET requested=1,running_version=NULL,running_token=NULL,failures=0,retry_at=0,error=NULL").map_err(sql)
}
