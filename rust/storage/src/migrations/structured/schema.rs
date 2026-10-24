//! 历史表定义只属于对应修订，不依赖运行时模型。

use super::*;

pub(super) use crate::legacy_execution::{RAW_FIELDS, SYMBOL_FIELDS};

pub(super) const COST_CHILDREN: &[&str] = &[
    "performance_cost_transaction_fee",
    "performance_cost_transaction",
    "performance_cost_attempt",
    "performance_cost_position",
    "performance_cost_execution_symbol",
    "performance_cost_range",
];
pub(super) const RESULT_NAMES: &[&str] = &[
    "performance_result_daily_fee",
    "performance_result_daily_cost",
    "performance_result_event",
    "performance_result_calendar_range",
    "performance_result_binding",
    "performance_result_fallback",
    "performance_result_skip",
    "performance_result_point",
    "performance_result_range",
];
pub(super) const SOURCE_NAMES: &[&str] = &[
    "analysis_source_transaction_fee",
    "analysis_source_transaction",
    "analysis_source_attempt",
    "analysis_source_position",
    "analysis_source_execution_symbol",
    "analysis_source_trade",
    "analysis_source_execution",
];
pub(super) const DERIVED: &[&str] = &[
    "analysis_source_transaction_fee",
    "analysis_source_transaction",
    "analysis_source_attempt",
    "analysis_source_position",
    "analysis_source_execution_symbol",
    "analysis_source_trade",
    "analysis_source_execution",
    "replay_observation_symbol",
    "replay_observation",
];
pub(super) const SUMMARY_COLUMNS: &[(&str, &str)] = &[
    ("value", "REAL"),
    ("cost", "REAL"),
    ("loss_bp", "REAL"),
    ("coverage", "REAL"),
    ("covered", "INTEGER"),
    ("count", "INTEGER"),
    ("fee_covered", "INTEGER"),
    ("amount_complete", "BOOLEAN"),
    ("covered_value", "REAL"),
    ("estimated", "INTEGER"),
    ("has_transactions", "BOOLEAN"),
];

pub(super) fn execution_columns() -> Vec<(String, String)> {
    let mut columns = vec![
        ("key", "TEXT"),
        ("execution_id", "TEXT"),
        ("created_at", "TEXT"),
        ("is_success", "INTEGER"),
        ("duration_sec", "FLOAT"),
        ("trade_count", "INTEGER"),
        ("symbol_count", "INTEGER"),
        ("position_count", "INTEGER"),
        ("positions_present", "BOOLEAN DEFAULT '0' NOT NULL"),
        ("reason", "TEXT"),
        ("raw_present", "INTEGER DEFAULT '0' NOT NULL"),
        ("has_symbol_results", "BOOLEAN DEFAULT '0' NOT NULL"),
    ]
    .into_iter()
    .map(|(name, ddl)| (name.to_owned(), ddl.to_owned()))
    .collect::<Vec<_>>();
    columns.extend(
        RAW_FIELDS
            .iter()
            .map(|name| (format!("raw_{name}"), "TEXT".to_owned())),
    );
    columns
}
pub(super) fn trade_columns() -> Vec<(String, String)> {
    [
        ("side", "TEXT DEFAULT 'none' NOT NULL"),
        ("quantity", "FLOAT"),
        ("price", "FLOAT"),
        ("reference", "FLOAT"),
        ("reference_source", "TEXT"),
    ]
    .into_iter()
    .map(|(name, ddl)| (name.to_owned(), ddl.to_owned()))
    .collect()
}
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
fn result_table(connection: &Connection, name: &str, keys: &str, values: &str) -> Result<()> {
    let suffix = if keys.is_empty() {
        String::new()
    } else {
        format!(",{keys}")
    };
    let key_names = keys
        .split(',')
        .filter(|key| !key.trim().is_empty())
        .map(|key| key.split_whitespace().next().unwrap())
        .collect::<Vec<_>>();
    let pk = if key_names.is_empty() {
        String::new()
    } else {
        format!(",{}", key_names.join(","))
    };
    execute(connection,&format!("CREATE TABLE {name}(snapshot_id TEXT NOT NULL,range_key TEXT NOT NULL{suffix},{values},PRIMARY KEY(snapshot_id,range_key{pk}),FOREIGN KEY(snapshot_id) REFERENCES performance_snapshot(id) ON DELETE CASCADE)"))
}
fn child_table(
    connection: &Connection,
    name: &str,
    root: &str,
    keys: &str,
    values: &str,
) -> Result<()> {
    let (root_key, root_kind, parent, parent_key) = match root {
        "cost" => ("snapshot_id", "TEXT", "performance_snapshot", "id"),
        _ => (
            "record_id",
            "INTEGER",
            "analysis_source_execution",
            "record_id",
        ),
    };
    let key_names = keys
        .split(',')
        .filter(|key| !key.trim().is_empty())
        .map(|key| key.split_whitespace().next().unwrap())
        .collect::<Vec<_>>();
    let suffix = if keys.is_empty() {
        String::new()
    } else {
        format!(",{keys}")
    };
    let pk = if key_names.is_empty() {
        String::new()
    } else {
        format!(",{}", key_names.join(","))
    };
    execute(connection,&format!("CREATE TABLE {name}({root_key} {root_kind} NOT NULL{suffix},{values},PRIMARY KEY({root_key}{pk}),FOREIGN KEY({root_key}) REFERENCES {parent}({parent_key}) ON DELETE CASCADE)"))
}
const TRANSACTION_VALUES:&str="symbol TEXT,side TEXT,quantity FLOAT,price FLOAT,reference FLOAT,reference_source TEXT,time FLOAT,end_time FLOAT,time_estimated BOOLEAN NOT NULL,before FLOAT,target FLOAT,value FLOAT,cost FLOAT,loss_bp FLOAT,coverage FLOAT,covered INTEGER,count INTEGER,amount_complete BOOLEAN NOT NULL,fee_covered INTEGER";

pub(super) fn create34(connection: &Connection) -> Result<()> {
    child_table(
        connection,
        "performance_cost_range",
        "cost",
        "range_key TEXT NOT NULL",
        "baseline FLOAT,end FLOAT",
    )?;
    child_table(connection,"performance_cost_execution_symbol","cost","record_id INTEGER NOT NULL,symbol TEXT NOT NULL","present INTEGER NOT NULL,status TEXT,error TEXT,outcome TEXT,outcome_reason TEXT,reason_code TEXT")?;
    child_table(
        connection,
        "performance_cost_position",
        "cost",
        "record_id INTEGER NOT NULL,idx INTEGER NOT NULL",
        "symbol TEXT,quantity FLOAT,direction TEXT",
    )?;
    child_table(
        connection,
        "performance_cost_attempt",
        "cost",
        "record_id INTEGER NOT NULL,idx INTEGER NOT NULL",
        "symbol TEXT,planned FLOAT,filled FLOAT,reason TEXT",
    )?;
    child_table(
        connection,
        "performance_cost_transaction",
        "cost",
        "record_id INTEGER NOT NULL,idx INTEGER NOT NULL",
        TRANSACTION_VALUES,
    )?;
    child_table(
        connection,
        "performance_cost_transaction_fee",
        "cost",
        "record_id INTEGER NOT NULL,transaction_idx INTEGER NOT NULL,currency TEXT NOT NULL",
        "value FLOAT NOT NULL",
    )?;
    create_replay(connection, false)
}
fn table_exists(connection: &Connection, name: &str) -> Result<bool> {
    Ok(!rows(
        connection,
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        [name],
    )?
    .is_empty())
}
pub(super) fn create_replay(connection: &Connection, if_missing: bool) -> Result<()> {
    if !if_missing || !table_exists(connection, "replay_observation")? {
        execute(connection,"CREATE TABLE replay_observation(record_id INTEGER NOT NULL PRIMARY KEY REFERENCES executerecord(id) ON DELETE CASCADE,account_id INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,execution_id TEXT,time TEXT NOT NULL,asset FLOAT,sizing_fallback BOOLEAN NOT NULL,target_present BOOLEAN NOT NULL,target_weight_present BOOLEAN NOT NULL,target_units_present BOOLEAN NOT NULL,target_quantities_present BOOLEAN NOT NULL); CREATE INDEX ix_replay_observation_account_time ON replay_observation(account_id,time,record_id);")?;
    }
    if !if_missing || !table_exists(connection, "replay_observation_symbol")? {
        execute(connection,"CREATE TABLE replay_observation_symbol(record_id INTEGER NOT NULL REFERENCES replay_observation(record_id) ON DELETE CASCADE,symbol TEXT NOT NULL,target FLOAT,price FLOAT,target_weight FLOAT,target_units FLOAT,target_quantity FLOAT,PRIMARY KEY(record_id,symbol))")?;
    }
    Ok(())
}
pub(super) fn create_sources(connection: &Connection, if_missing: bool) -> Result<()> {
    if !if_missing || !table_exists(connection, "analysis_source_execution")? {
        let mut fields=String::from("record_id INTEGER NOT NULL PRIMARY KEY REFERENCES executerecord(id) ON DELETE CASCADE,account_id INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,projection_version INTEGER DEFAULT '1' NOT NULL,projection_error TEXT,event_tag TEXT,event_text TEXT,time FLOAT,success BOOLEAN DEFAULT '0' NOT NULL,noop BOOLEAN DEFAULT '0' NOT NULL");
        for (name, kind) in execution_columns() {
            fields.push_str(&format!(",{} {kind}", quote(&name)));
        }
        execute(connection,&format!("CREATE TABLE analysis_source_execution({fields}); CREATE INDEX ix_analysis_source_execution_account_time ON analysis_source_execution(account_id,time,record_id);"))?;
    }
    for(name,keys,values) in [
        ("analysis_source_trade","trade_index INTEGER NOT NULL","symbol TEXT NOT NULL,time FLOAT NOT NULL,day TEXT NOT NULL,value FLOAT,cost FLOAT,loss_bp FLOAT,fee FLOAT,fee_currency TEXT,estimated BOOLEAN NOT NULL,side TEXT DEFAULT 'none' NOT NULL,quantity FLOAT,price FLOAT,reference FLOAT,reference_source TEXT"),
        ("analysis_source_execution_symbol","symbol TEXT NOT NULL","present INTEGER NOT NULL,status TEXT,error TEXT,outcome TEXT,outcome_reason TEXT,reason_code TEXT"),
        ("analysis_source_position","idx INTEGER NOT NULL","symbol TEXT,quantity FLOAT,direction TEXT"),
        ("analysis_source_attempt","idx INTEGER NOT NULL","symbol TEXT,planned FLOAT,filled FLOAT,reason TEXT"),
        ("analysis_source_transaction","idx INTEGER NOT NULL",TRANSACTION_VALUES),
        ("analysis_source_transaction_fee","transaction_idx INTEGER NOT NULL,currency TEXT NOT NULL","value FLOAT NOT NULL"),
    ] {
        if !if_missing || !table_exists(connection,name)? { child_table(connection,name,"source",keys,values)?; }
    }
    Ok(())
}
pub(super) fn create35(connection: &Connection) -> Result<()> {
    create_sources(connection, false)?;
    result_table(connection,"performance_result_range","","backtest_included BOOLEAN NOT NULL,engine_version TEXT NOT NULL,baseline TEXT,end TEXT,record_count INTEGER NOT NULL,observation_count INTEGER NOT NULL,used_record_count INTEGER NOT NULL,target_used_record_count INTEGER NOT NULL,invalid_asset_count INTEGER NOT NULL,sizing_fallback_count INTEGER,calendar_status TEXT NOT NULL,calendar_id TEXT,calendar_label TEXT,event_count INTEGER NOT NULL")?;
    result_table(connection,"performance_result_point","idx INTEGER NOT NULL","date TEXT NOT NULL,observed_at TEXT NOT NULL,record_id INTEGER,execution_id TEXT,account_return FLOAT,account_equity FLOAT,portfolio_return FLOAT,account_daily_return FLOAT,portfolio_daily_return FLOAT,target_portfolio_return FLOAT,target_portfolio_daily_return FLOAT,difference FLOAT,sizing_fallback BOOLEAN NOT NULL")?;
    result_table(connection,"performance_result_skip","kind TEXT NOT NULL","count INTEGER NOT NULL,missing_target INTEGER NOT NULL,missing_ticks INTEGER NOT NULL,first_time TEXT,last_time TEXT")?;
    result_table(
        connection,
        "performance_result_fallback",
        "idx INTEGER NOT NULL",
        "start TEXT NOT NULL,end TEXT NOT NULL,count INTEGER NOT NULL",
    )?;
    result_table(
        connection,
        "performance_result_binding",
        "idx INTEGER NOT NULL",
        "time TEXT NOT NULL,portfolio_id INTEGER",
    )?;
    result_table(
        connection,
        "performance_result_calendar_range",
        "kind TEXT NOT NULL,idx INTEGER NOT NULL",
        "start TEXT NOT NULL,end TEXT NOT NULL",
    )?;
    result_table(
        connection,
        "performance_result_event",
        "idx INTEGER NOT NULL",
        "time TEXT NOT NULL,tag TEXT NOT NULL,text TEXT NOT NULL,execution_id TEXT",
    )?;
    result_table(connection,"performance_result_daily_cost","day TEXT NOT NULL","value FLOAT,cost FLOAT,loss_bp FLOAT,coverage FLOAT,covered_value FLOAT,covered INTEGER,count INTEGER,fee_covered INTEGER,estimated INTEGER,amount_complete BOOLEAN NOT NULL,present INTEGER NOT NULL")?;
    result_table(
        connection,
        "performance_result_daily_fee",
        "day TEXT NOT NULL,currency TEXT NOT NULL",
        "value FLOAT NOT NULL",
    )?;
    execute(connection,"CREATE TABLE analysis_calendar_day(channel TEXT NOT NULL,day TEXT NOT NULL,status TEXT NOT NULL,calendar_id TEXT,label TEXT,PRIMARY KEY(channel,day))")
}
pub(super) fn create_summary(connection: &Connection) -> Result<()> {
    execute(connection,"CREATE TABLE performance_execution_summary(snapshot_id TEXT NOT NULL REFERENCES performance_snapshot(id) ON DELETE CASCADE,record_id INTEGER NOT NULL,time FLOAT NOT NULL,value FLOAT,cost FLOAT,loss_bp FLOAT,coverage FLOAT,covered INTEGER NOT NULL,count INTEGER NOT NULL,fee_covered INTEGER NOT NULL,amount_complete BOOLEAN NOT NULL,covered_value FLOAT NOT NULL,estimated INTEGER NOT NULL,symbol_count INTEGER,has_transactions BOOLEAN NOT NULL,PRIMARY KEY(snapshot_id,record_id)); CREATE INDEX ix_performance_execution_summary_time ON performance_execution_summary(snapshot_id,time,record_id);")
}
pub(super) fn create36(connection: &Connection) -> Result<()> {
    create_summary(connection)?;
    execute(connection,"CREATE TABLE performance_execution_fee(snapshot_id TEXT NOT NULL REFERENCES performance_snapshot(id) ON DELETE CASCADE,record_id INTEGER NOT NULL,currency TEXT NOT NULL,value FLOAT NOT NULL,count INTEGER NOT NULL,PRIMARY KEY(snapshot_id,record_id,currency))")
}
pub(super) fn create_cost_range(connection: &Connection) -> Result<()> {
    child_table(
        connection,
        "performance_cost_range",
        "cost",
        "range_key TEXT NOT NULL",
        "baseline FLOAT,end FLOAT",
    )
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
