//! 查询索引增量迁移；业务事实和既有时间文本保持原样。

use rusqlite::Connection;

use super::{invalid, sql};
use crate::error::AnalysisError;

const TIME_INDEXES: &[(&str, &str, &str)] = &[
    (
        "ix_executerecord_account_time_id",
        "executerecord",
        "created_at",
    ),
    (
        "ix_schedule_skip_account_time_id",
        "schedule_skip",
        "triggered_at",
    ),
];
const OTHER_INDEXES: &[(&str, &str, &str)] = &[
    (
        "ix_portfolioaccount_account_id_id",
        "portfolioaccount",
        "account_id,id",
    ),
    (
        "ix_control_session_account_status_finished",
        "account_control_session",
        "account_id,status,finished_at_ms",
    ),
];

pub(super) fn upgrade(connection: &Connection) -> Result<(), AnalysisError> {
    for (name, table, column) in TIME_INDEXES {
        if let Err(error) = connection.execute_batch(&format!(
            "CREATE INDEX {name} ON {table}(account_id,axile_time({column}) DESC,id DESC)"
        )) {
            // 只在建索引失败时定位坏时间，避免正常升级重复扫描历史。
            let mut statement = connection
                .prepare(&format!("SELECT id,{column} FROM {table} ORDER BY id"))
                .map_err(sql)?;
            let mut rows = statement.query([]).map_err(sql)?;
            while let Some(row) = rows.next().map_err(sql)? {
                let id: i64 = row.get(0).map_err(sql)?;
                let value: String = row.get(1).map_err(sql)?;
                if let Err(reason) = crate::calendar::canonical_time(&value) {
                    return Err(invalid(format!(
                        "Cannot create {name}: {table} id={id} {column}={value:?}: {reason}"
                    )));
                }
            }
            return Err(sql(error));
        }
    }
    for (name, table, columns) in OTHER_INDEXES {
        connection
            .execute_batch(&format!("CREATE INDEX {name} ON {table}({columns})"))
            .map_err(sql)?;
    }
    Ok(())
}

pub(super) fn downgrade(connection: &Connection) -> Result<(), AnalysisError> {
    for (name, _, _) in OTHER_INDEXES.iter().chain(TIME_INDEXES).rev() {
        connection
            .execute_batch(&format!("DROP INDEX {name}"))
            .map_err(sql)?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn schema(connection: &Connection) {
        crate::calendar::register_time_function(connection).unwrap();
        connection.execute_batch(
            "CREATE TABLE executerecord(id INTEGER PRIMARY KEY,account_id INTEGER,created_at TEXT);
             CREATE TABLE schedule_skip(id INTEGER PRIMARY KEY,account_id INTEGER,triggered_at TEXT);
             CREATE TABLE portfolioaccount(id INTEGER PRIMARY KEY,account_id INTEGER);
             CREATE TABLE account_control_session(id INTEGER PRIMARY KEY,account_id INTEGER,status TEXT,finished_at_ms INTEGER);"
        ).unwrap();
    }

    #[test]
    fn indexes_preserve_mixed_time_order_and_leave_facts_unchanged() {
        let connection = Connection::open_in_memory().unwrap();
        schema(&connection);
        connection
            .execute_batch(
                "INSERT INTO executerecord VALUES
             (1,7,'2026-01-01T16:00:00Z'),
             (2,7,'2026-01-02 00:00:00.000001'),
             (3,7,'2026-01-01T23:00:00+08:00');",
            )
            .unwrap();
        upgrade(&connection).unwrap();
        let sql = "SELECT id FROM executerecord WHERE account_id=7 AND axile_time(created_at)>='2026-01-02T00:00:00.000000' ORDER BY axile_time(created_at) DESC,id DESC";
        let ids = connection
            .prepare(sql)
            .unwrap()
            .query_map([], |row| row.get::<_, i64>(0))
            .unwrap()
            .collect::<rusqlite::Result<Vec<_>>>()
            .unwrap();
        assert_eq!(ids, [2, 1]);
        let plan = connection
            .prepare(&format!("EXPLAIN QUERY PLAN {sql}"))
            .unwrap()
            .query_map([], |row| row.get::<_, String>(3))
            .unwrap()
            .collect::<rusqlite::Result<Vec<_>>>()
            .unwrap();
        assert!(
            plan.iter()
                .any(|line| line.contains("ix_executerecord_account_time_id")
                    && line.contains("<expr>>")),
            "{plan:?}"
        );
        downgrade(&connection).unwrap();
        let original: String = connection
            .query_row(
                "SELECT created_at FROM executerecord WHERE id=1",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(original, "2026-01-01T16:00:00Z");
    }

    #[test]
    fn invalid_history_identifies_the_record_and_rolls_back_indexes() {
        let mut connection = Connection::open_in_memory().unwrap();
        schema(&connection);
        connection
            .execute_batch("INSERT INTO schedule_skip VALUES(9,7,'bad-time')")
            .unwrap();
        let transaction = connection.transaction().unwrap();
        let error = upgrade(&transaction).unwrap_err();
        assert!(
            error
                .error
                .contains("schedule_skip id=9 triggered_at=\"bad-time\""),
            "{error:?}"
        );
        transaction.rollback().unwrap();
        let count: i64 = connection
            .query_row(
                "SELECT count(*) FROM sqlite_master WHERE type='index'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(count, 0);
        assert_eq!(
            connection
                .query_row("SELECT count(*) FROM schedule_skip", [], |row| row
                    .get::<_, i64>(0))
                .unwrap(),
            1
        );
    }
}
