//! 0034 的最终表结构；业务事实与可重算分析缓存分别处理。

use super::*;

pub(super) const ANALYSIS_TABLES: &[&str] = &[
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
    "analysis_calendar_day",
];

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
