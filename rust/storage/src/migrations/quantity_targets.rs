//! 0035 只增加目标口径；旧事实保持原值及缺失语义。

use rusqlite::Connection;

use super::{invalid, sql};
use crate::error::AnalysisError;

pub(super) fn upgrade(connection: &Connection) -> Result<(), AnalysisError> {
    connection.execute_batch(
        "ALTER TABLE target_weight_snapshot ADD COLUMN target_mode TEXT NOT NULL DEFAULT 'weight' CHECK(target_mode IN ('weight','quantity'));
         ALTER TABLE execution_input ADD COLUMN target_mode TEXT CHECK(target_mode IN ('weight','quantity'));"
    ).map_err(sql)
}

pub(super) fn validate(connection: &Connection) -> Result<(), AnalysisError> {
    for table in ["execution_input", "target_weight_snapshot"] {
        let present: bool = connection.query_row(
            "SELECT EXISTS(SELECT 1 FROM pragma_table_info(?1) WHERE name='target_mode' AND upper(type)='TEXT')",
            [table], |row| row.get(0),
        ).map_err(sql)?;
        if !present {
            return Err(invalid(format!("0035 缺少 {table}.target_mode")));
        }
    }
    Ok(())
}

pub(super) fn preflight_downgrade(connection: &Connection) -> Result<(), AnalysisError> {
    let quantities: bool = connection.query_row(
        "SELECT EXISTS(SELECT 1 FROM execution_input WHERE target_mode='quantity') OR EXISTS(SELECT 1 FROM target_weight_snapshot WHERE target_mode='quantity')",
        [], |row| row.get(0),
    ).map_err(sql)?;
    if quantities {
        return Err(invalid(
            "数量目标无法无损降级至 0034；请保留数据库或恢复升级前备份",
        ));
    }
    Ok(())
}

pub(super) fn downgrade(connection: &Connection) -> Result<(), AnalysisError> {
    preflight_downgrade(connection)?;
    connection
        .execute_batch(
            "ALTER TABLE execution_input DROP COLUMN target_mode;
         ALTER TABLE target_weight_snapshot DROP COLUMN target_mode;",
        )
        .map_err(sql)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn old_weights_and_absent_input_modes_survive_upgrade_and_downgrade() {
        let connection = Connection::open_in_memory().unwrap();
        connection.execute_batch("CREATE TABLE target_weight_snapshot(id INTEGER PRIMARY KEY); CREATE TABLE execution_input(id TEXT PRIMARY KEY); INSERT INTO target_weight_snapshot VALUES(1); INSERT INTO execution_input VALUES('old');").unwrap();
        upgrade(&connection).unwrap();
        validate(&connection).unwrap();
        assert_eq!(
            connection
                .query_row("SELECT target_mode FROM target_weight_snapshot", [], |r| {
                    r.get::<_, String>(0)
                })
                .unwrap(),
            "weight"
        );
        assert_eq!(
            connection
                .query_row("SELECT target_mode FROM execution_input", [], |r| r
                    .get::<_, Option<String>>(0))
                .unwrap(),
            None
        );
        downgrade(&connection).unwrap();
        assert_eq!(
            connection
                .query_row("SELECT id FROM target_weight_snapshot", [], |r| r
                    .get::<_, i64>(0))
                .unwrap(),
            1
        );
    }

    #[test]
    fn quantity_evidence_blocks_lossy_downgrade() {
        let connection = Connection::open_in_memory().unwrap();
        connection.execute_batch("CREATE TABLE target_weight_snapshot(id INTEGER PRIMARY KEY); CREATE TABLE execution_input(id TEXT PRIMARY KEY);").unwrap();
        upgrade(&connection).unwrap();
        connection
            .execute(
                "INSERT INTO execution_input(id,target_mode) VALUES('quantity','quantity')",
                [],
            )
            .unwrap();
        assert!(downgrade(&connection).is_err());
        validate(&connection).unwrap();
    }
}
