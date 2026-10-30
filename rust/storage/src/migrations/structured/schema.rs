//! 0034 的最终表结构；业务事实与可重算分析缓存分别处理。

use super::*;

pub(super) fn add_columns(
    connection: &Connection,
    table: &str,
    columns: &[(String, String)],
) -> Result<()> {
    for (name, ddl) in columns {
        execute(
            connection,
            &format!(
                "ALTER TABLE {} ADD COLUMN {} {ddl}",
                quote(table),
                quote(name)
            ),
        )?;
    }
    Ok(())
}
pub(super) fn drop_columns(
    connection: &Connection,
    table: &str,
    columns: &[(String, String)],
) -> Result<()> {
    for (name, _) in columns.iter().rev() {
        execute(
            connection,
            &format!("ALTER TABLE {} DROP COLUMN {}", quote(table), quote(name)),
        )?;
    }
    Ok(())
}
pub(super) const BUSINESS_TABLES: &[&str] = &[
    "execution_input",
    "execution_input_target",
    "execution_input_symbol",
    "execution_input_plan",
    "executerecord",
    "execution_termination_cancel_failure",
    "execution_symbol_collection",
    "execution_symbol_result",
    "execution_symbol_sizing",
    "execution_symbol_conversion",
    "execution_symbol_tick",
    "execution_symbol_target",
    "business_asset_snapshot",
    "business_asset_position",
    "business_asset_binding",
    "execution_order_collection",
    "execution_order",
    "business_trade_collection",
    "business_trade",
    "business_trade_conversion",
    "execution_extension",
    "target_weight_snapshot_weight",
    "account_asset_snapshot",
    "target_weight_snapshot",
];
pub(super) const NEW_BUSINESS_TABLES: &[&str] = &[
    "execution_input",
    "execution_input_target",
    "execution_input_symbol",
    "execution_input_plan",
    "execution_termination_cancel_failure",
    "execution_symbol_collection",
    "execution_symbol_result",
    "execution_symbol_sizing",
    "execution_symbol_conversion",
    "execution_symbol_tick",
    "execution_symbol_target",
    "business_asset_snapshot",
    "business_asset_position",
    "business_asset_binding",
    "execution_order_collection",
    "execution_order",
    "business_trade_collection",
    "business_trade",
    "business_trade_conversion",
    "execution_extension",
    "target_weight_snapshot_weight",
];
pub(super) fn business_columns() -> Vec<(&'static str, Vec<(String, String)>)> {
    let record = [
        ("input_id", "TEXT REFERENCES execution_input(id)"),
        ("result_input_id", "TEXT REFERENCES execution_input(id)"),
        ("status", "TEXT"),
        ("symbol", "TEXT"),
        ("technical_detail", "TEXT"),
        ("task_status", "TEXT"),
        ("error", "TEXT"),
        ("outcome", "TEXT"),
        ("outcome_reason", "TEXT"),
        ("reason_code", "TEXT"),
        ("msg", "TEXT"),
        ("execution_kind", "TEXT"),
        ("channel_type", "TEXT"),
        ("execution_time", "FLOAT"),
        ("success", "BOOLEAN"),
        ("display_status", "TEXT"),
        ("display_error", "TEXT"),
        ("display_outcome", "TEXT"),
        ("display_outcome_reason", "TEXT"),
        ("termination_reason", "TEXT"),
        ("termination_mode", "TEXT"),
        ("termination_trigger", "TEXT"),
        ("termination_requested_at", "TEXT"),
        ("termination_acked_at", "TEXT"),
        ("termination_finished_at", "TEXT"),
        ("termination_cancel_attempted", "BOOLEAN"),
        ("termination_forced", "BOOLEAN"),
        ("termination_cancel_unconfirmed", "BOOLEAN"),
        ("result_input_kind", "TEXT NOT NULL DEFAULT 'absent'"),
        ("result_input_value", "JSON"),
        ("audit_evidence", "JSON NOT NULL DEFAULT '{}'"),
        ("termination_evidence", "JSON NOT NULL DEFAULT '{}'"),
        ("termination_kind", "TEXT NOT NULL DEFAULT 'absent'"),
        (
            "termination_cancel_ids_kind",
            "TEXT NOT NULL DEFAULT 'absent'",
        ),
        ("termination_cancel_count", "INTEGER"),
        ("facts_complete", "BOOLEAN NOT NULL DEFAULT 0"),
        ("result_staged", "INTEGER NOT NULL DEFAULT 0"),
    ]
    .into_iter()
    .map(|(name, kind)| (name.into(), kind.into()))
    .collect();
    vec![
        ("executerecord", record),
        (
            "account_asset_snapshot",
            vec![(
                "asset_fact_id".into(),
                "TEXT REFERENCES business_asset_snapshot(asset_id) ON DELETE SET NULL".into(),
            )],
        ),
        (
            "target_weight_snapshot",
            [
                ("raw_weights_kind", "TEXT DEFAULT 'null' NOT NULL"),
                ("normalized_weights_kind", "TEXT DEFAULT 'null' NOT NULL"),
                ("weights_metadata", "JSON DEFAULT '{}' NOT NULL"),
            ]
            .into_iter()
            .map(|(name, kind)| (name.into(), kind.into()))
            .collect(),
        ),
    ]
}

const TIME_INDEXES: &[(&str, &str, &str)] = &[
    (
        "ix_executerecord_account_time_id",
        "executerecord",
        "created_at_us",
    ),
    (
        "ix_schedule_skip_account_time_id",
        "schedule_skip",
        "triggered_at_us",
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
        "account_id,status,finished_at_us",
    ),
];

pub(super) fn install_query_indexes(connection: &Connection) -> Result<()> {
    for (name, table, column) in TIME_INDEXES {
        connection
            .execute_batch(&format!(
                "CREATE INDEX IF NOT EXISTS {name} ON {table}(account_id,{column} DESC,id DESC)"
            ))
            .map_err(sql)?;
    }
    for (name, table, columns) in OTHER_INDEXES {
        connection
            .execute_batch(&format!(
                "CREATE INDEX IF NOT EXISTS {name} ON {table}({columns})"
            ))
            .map_err(sql)?;
    }
    Ok(())
}

pub(super) fn drop_query_indexes(connection: &Connection) -> Result<()> {
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

    #[test]
    fn integer_time_boundaries_use_the_plain_covering_index() {
        let connection = Connection::open_in_memory().unwrap();
        connection.execute_batch(
            "CREATE TABLE executerecord(id INTEGER PRIMARY KEY,account_id INTEGER,created_at_us INTEGER);
             CREATE TABLE schedule_skip(id INTEGER PRIMARY KEY,account_id INTEGER,triggered_at_us INTEGER);
             CREATE TABLE portfolioaccount(id INTEGER PRIMARY KEY,account_id INTEGER);
             CREATE TABLE account_control_session(id INTEGER PRIMARY KEY,account_id INTEGER,status TEXT,finished_at_us INTEGER);
             INSERT INTO executerecord VALUES(1,7,1000),(2,7,1001),(3,7,999);"
        ).unwrap();
        install_query_indexes(&connection).unwrap();
        let sql = "SELECT id FROM executerecord WHERE account_id=7 AND created_at_us>=1000 ORDER BY created_at_us DESC,id DESC";
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
                    && line.contains("created_at_us>")),
            "{plan:?}"
        );
        drop_query_indexes(&connection).unwrap();
        assert_eq!(
            connection
                .query_row(
                    "SELECT created_at_us FROM executerecord WHERE id=1",
                    [],
                    |row| row.get::<_, i64>(0)
                )
                .unwrap(),
            1000
        );
    }
}
