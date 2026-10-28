//! 0037 只删除可重算结果和队列状态；保留事实、日历和原有失效序号。

use super::sql;
use crate::error::AnalysisError;
use rusqlite::Connection;

const QUEUE_COLUMNS: &[(&str, &str)] = &[
    ("published_version", "INTEGER NOT NULL DEFAULT 0"),
    ("current_snapshot", "TEXT"),
    ("previous_snapshot", "TEXT"),
    ("requested", "BOOLEAN NOT NULL DEFAULT 0"),
    ("running_version", "INTEGER"),
    ("failures", "INTEGER NOT NULL DEFAULT 0"),
    ("retry_at", "FLOAT NOT NULL DEFAULT 0"),
    ("error", "TEXT"),
    ("logic_version", "TEXT"),
    ("engine_version", "TEXT"),
    ("running_token", "TEXT"),
];
const QUEUE_UPDATE: &str = ",requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL THEN 1 ELSE requested END,failures=0,retry_at=0,error=NULL";

fn triggers(connection: &Connection, downgrade: bool) -> Result<(), AnalysisError> {
    // 已发布的 0033 触发器含换行和不同空格，不能按整段 SQL 字面替换。
    let version_update =
        regex::Regex::new(r"source_version\s*=\s*source_version\s*\+\s*1").unwrap();
    let queue_update =
        regex::Regex::new(r",\s*requested\s*=\s*CASE\b[^;]*?\berror\s*=\s*NULL").unwrap();
    let rows = connection
        .prepare(
            "SELECT name,sql FROM sqlite_schema WHERE type='trigger'
         AND (sql LIKE '%account_analysis%' OR sql LIKE '%performance_snapshot%')",
        )
        .map_err(sql)?
        .query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)))
        .map_err(sql)?
        .collect::<Result<Vec<_>, _>>()
        .map_err(sql)?;
    for (name, definition) in rows {
        connection
            .execute_batch(&format!("DROP TRIGGER \"{}\"", name.replace('"', "\"\"")))
            .map_err(sql)?;
        if matches!(
            name.as_str(),
            "analysis_snapshot_delete" | "analysis_account_delete"
        ) {
            continue;
        }
        let definition = version_update.replace_all(&definition, "source_version=source_version+1");
        let definition = if downgrade {
            definition.replace(
                "source_version=source_version+1",
                &format!("source_version=source_version+1{QUEUE_UPDATE}"),
            )
        } else {
            queue_update.replace_all(&definition, "").into_owned()
        };
        connection.execute_batch(&definition).map_err(sql)?;
    }
    connection.execute_batch("CREATE TRIGGER analysis_account_delete AFTER DELETE ON account BEGIN DELETE FROM account_analysis WHERE account_id=OLD.id; END;").map_err(sql)
}

pub(super) fn upgrade(connection: &Connection) -> Result<(), AnalysisError> {
    triggers(connection, false)?;
    connection.execute_batch("CREATE TABLE analysis_source(identity TEXT NOT NULL); INSERT INTO analysis_source VALUES(lower(hex(randomblob(16))));").map_err(sql)?;
    // 此名单对应 0036 的冻结结构，不删除任何业务证据。
    for table in [
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
    ] {
        connection
            .execute_batch(&format!("DROP TABLE {table}"))
            .map_err(sql)?;
    }
    for (column, _) in QUEUE_COLUMNS {
        connection
            .execute_batch(&format!(
                "ALTER TABLE account_analysis DROP COLUMN {column}"
            ))
            .map_err(sql)?;
    }
    for (event, owner, condition) in [
        ("INSERT", "NEW", ""),
        ("DELETE", "OLD", ""),
        ("UPDATE", "NEW", "WHEN OLD.status IS NOT NEW.status OR OLD.calendar_id IS NOT NEW.calendar_id OR OLD.label IS NOT NEW.label"),
    ] {
        connection.execute_batch(&format!(
            "CREATE TRIGGER analysis_calendar_{} AFTER {event} ON analysis_calendar_day {condition}
             BEGIN INSERT INTO account_analysis(account_id,source_version)
             SELECT id,1 FROM account WHERE trade_channel={owner}.channel
             ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1; END;",
             event.to_lowercase(),
        )).map_err(sql)?;
    }
    Ok(())
}

pub(super) fn downgrade(connection: &Connection) -> Result<(), AnalysisError> {
    connection
        .execute_batch("DROP TABLE analysis_source")
        .map_err(sql)?;
    connection.execute_batch("DROP TRIGGER analysis_calendar_insert; DROP TRIGGER analysis_calendar_update; DROP TRIGGER analysis_calendar_delete;").map_err(sql)?;
    let schema = include_str!("structured/analysis.sql");
    connection
        .execute_batch(&schema[..schema.find("CREATE TABLE analysis_calendar_day").unwrap()])
        .map_err(sql)?;
    connection
        .execute_batch(include_str!("contributions.sql"))
        .map_err(sql)?;
    for (column, definition) in QUEUE_COLUMNS {
        connection
            .execute_batch(&format!(
                "ALTER TABLE account_analysis ADD COLUMN {column} {definition}"
            ))
            .map_err(sql)?;
    }
    triggers(connection, true)?;
    // 空缓存由旧程序按需排队重建，回退同样不改业务事实。
    super::structured::restore_analysis_triggers(connection)
}
