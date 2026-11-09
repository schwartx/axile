//! 0036 保留账户、组合及历史，仅增加可恢复的归档标记。

use rusqlite::Connection;

use super::{invalid, sql};
use crate::error::AnalysisError;

pub(super) fn upgrade(connection: &Connection) -> Result<(), AnalysisError> {
    connection
        .execute_batch(
            "ALTER TABLE account ADD COLUMN archived_at_us INTEGER;
         ALTER TABLE portfolio ADD COLUMN archived_at_us INTEGER;",
        )
        .map_err(sql)
}

pub(super) fn validate(connection: &Connection) -> Result<(), AnalysisError> {
    for table in ["account", "portfolio"] {
        let present: bool = connection.query_row(
            "SELECT EXISTS(SELECT 1 FROM pragma_table_info(?1) WHERE name='archived_at_us' AND upper(type)='INTEGER')",
            [table], |row| row.get(0),
        ).map_err(sql)?;
        if !present {
            return Err(invalid(format!("0036 缺少 {table}.archived_at_us")));
        }
    }
    Ok(())
}

pub(super) fn preflight_downgrade(connection: &Connection) -> Result<(), AnalysisError> {
    let archived: bool = connection
        .query_row(
            "SELECT EXISTS(SELECT 1 FROM account WHERE archived_at_us IS NOT NULL)
         OR EXISTS(SELECT 1 FROM portfolio WHERE archived_at_us IS NOT NULL)",
            [],
            |row| row.get(0),
        )
        .map_err(sql)?;
    if archived {
        return Err(invalid(
            "存在已归档账户或组合，无法无损降级；请先恢复归档对象",
        ));
    }
    Ok(())
}

pub(super) fn downgrade(connection: &Connection) -> Result<(), AnalysisError> {
    preflight_downgrade(connection)?;
    connection
        .execute_batch(
            "ALTER TABLE account DROP COLUMN archived_at_us;
         ALTER TABLE portfolio DROP COLUMN archived_at_us;",
        )
        .map_err(sql)
}
