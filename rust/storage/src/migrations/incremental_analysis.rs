//! 0038 为已存在的浏览器分析库安装持久增量游标，保留事实与数据库身份。
use super::sql;
use crate::error::AnalysisError;
use rusqlite::Connection;

// Coalesce repeated writes to one durable revision per execution; deletes keep a tombstone.
pub(super) fn upgrade(connection: &Connection) -> Result<(), AnalysisError> {
    connection.execute_batch("CREATE INDEX ix_executerecord_analysis_input ON executerecord(input_id); CREATE INDEX ix_executerecord_analysis_result_input ON executerecord(result_input_id); CREATE TABLE analysis_change(seq INTEGER PRIMARY KEY AUTOINCREMENT,account_id INTEGER NOT NULL,record_id INTEGER NOT NULL,UNIQUE(account_id,record_id));
        CREATE INDEX analysis_change_account ON analysis_change(account_id,seq);
        INSERT INTO analysis_change(account_id,record_id) SELECT id,0 FROM account;
        INSERT INTO analysis_change(account_id,record_id) SELECT account_id,id FROM executerecord ORDER BY id;").map_err(sql)?;
    let mut tables: Vec<&str> = crate::browser_fact_schema::TABLES
        .iter()
        .map(|t| t.name)
        .collect();
    tables.extend(["account_settings", "account_asset_snapshot"]);
    for table in tables {
        for (event, refs) in [
            ("INSERT", vec!["NEW"]),
            ("UPDATE", vec!["OLD", "NEW"]),
            ("DELETE", vec!["OLD"]),
        ] {
            let owners = refs
                .iter()
                .map(|r| change_owners(table, r))
                .collect::<Vec<_>>()
                .join(" UNION ");
            let timing = if event == "DELETE" { "BEFORE" } else { "AFTER" };
            let condition = if event == "UPDATE" {
                let columns = connection
                    .prepare(&format!("PRAGMA table_info({table})"))
                    .map_err(sql)?
                    .query_map([], |row| row.get::<_, String>(1))
                    .map_err(sql)?
                    .collect::<rusqlite::Result<Vec<_>>>()
                    .map_err(sql)?;
                format!(
                    "WHEN {}",
                    columns
                        .iter()
                        .map(|name| format!("OLD.\"{name}\" IS NOT NEW.\"{name}\""))
                        .collect::<Vec<_>>()
                        .join(" OR ")
                )
            } else {
                String::new()
            };
            connection.execute_batch(&format!("CREATE TRIGGER browser_change_{table}_{} {timing} {event} ON {table} {condition} BEGIN
                INSERT INTO analysis_change(account_id,record_id) SELECT account_id,record_id FROM ({owners}) WHERE account_id IS NOT NULL AND 1
                ON CONFLICT(account_id,record_id) DO UPDATE SET seq=excluded.seq; END", event.to_lowercase())).map_err(sql)?;
        }
    }
    connection.execute_batch("CREATE TRIGGER browser_change_account_cleanup AFTER DELETE ON account BEGIN DELETE FROM analysis_change WHERE account_id=OLD.id; END").map_err(sql)
}
fn change_owners(table: &str, r: &str) -> String {
    match table {
        "account" => format!("SELECT {r}.id AS account_id,0 AS record_id"),
        "account_settings" | "portfolioaccount" | "schedule_skip" => format!("SELECT {r}.account_id AS account_id,0 AS record_id"),
        "analysis_calendar_day" => format!("SELECT id AS account_id,0 AS record_id FROM account WHERE trade_channel={r}.channel"),
        "executerecord" => format!("SELECT {r}.account_id AS account_id,{r}.id AS record_id"),
        "account_asset_snapshot" => format!("SELECT account_id,id AS record_id FROM executerecord WHERE account_id={r}.account_id AND execution_id={r}.execution_id"),
        "execution_input" | "execution_input_target" => {
            let key = if table == "execution_input" { "id" } else { "input_id" };
            format!("SELECT account_id,id AS record_id FROM executerecord WHERE input_id={r}.{key} OR result_input_id={r}.{key}")
        },
        "business_asset_snapshot" | "business_asset_position" => format!("SELECT e.account_id,e.id AS record_id FROM business_asset_binding b JOIN executerecord e ON e.id=b.record_id WHERE b.asset_id={r}.asset_id"),
        "target_weight_snapshot" => format!("SELECT account_id,id AS record_id FROM executerecord WHERE account_id={r}.account_id AND execution_id={r}.execution_id"),
        "target_weight_snapshot_weight" => format!("SELECT e.account_id,e.id AS record_id FROM target_weight_snapshot s JOIN executerecord e ON e.account_id=s.account_id AND e.execution_id=s.execution_id WHERE s.id={r}.snapshot_id"),
        _ => format!("SELECT account_id,id AS record_id FROM executerecord WHERE id={r}.record_id"),
    }
}
pub(super) fn downgrade(connection: &Connection) -> Result<(), AnalysisError> {
    let names = connection
        .prepare(
            "SELECT name FROM sqlite_schema WHERE type='trigger' AND name GLOB 'browser_change_*'",
        )
        .map_err(sql)?
        .query_map([], |r| r.get::<_, String>(0))
        .map_err(sql)?
        .collect::<Result<Vec<_>, _>>()
        .map_err(sql)?;
    for name in names {
        connection
            .execute_batch(&format!("DROP TRIGGER {name}"))
            .map_err(sql)?;
    }
    connection
        .execute_batch("DROP TABLE analysis_change; DROP INDEX ix_executerecord_analysis_input; DROP INDEX ix_executerecord_analysis_result_input;")
        .map_err(sql)
}
