//! The browser receives only the account's analysis inputs, never credentials or raw evidence.

pub struct FactTable {
    pub name: &'static str,
    pub columns: &'static str,
    pub key: &'static str,
    pub scope: &'static str,
}
impl FactTable {
    pub fn names(&self) -> Vec<&str> {
        self.columns
            .split(',')
            .map(|column| column.trim().split_once(' ').unwrap().0)
            .collect()
    }
    pub fn create_sql(&self) -> String {
        format!(
            "CREATE TABLE {} ({},PRIMARY KEY ({}));",
            self.name, self.columns, self.key
        )
    }
}
const RECORD: &str = "record_id IN (SELECT id FROM executerecord WHERE account_id=?1)";

macro_rules! fact {
    ($name:ident, $columns:literal, $key:literal, $scope:expr) => {
        FactTable {
            name: stringify!($name),
            columns: $columns,
            key: $key,
            scope: $scope,
        }
    };
}
pub const TABLES: &[FactTable] = &[
    fact!(account, "id INTEGER,trade_channel TEXT", "id", "id=?1"),
    fact!(executerecord, "id INTEGER,account_id INTEGER,execution_id TEXT,created_at TEXT,is_success INTEGER,execution_time REAL,display_status TEXT,task_status TEXT,display_error TEXT,display_outcome TEXT,display_outcome_reason TEXT,reason_code TEXT,msg TEXT,execution_kind TEXT,facts_complete INTEGER,input_id TEXT,result_input_id TEXT,status TEXT", "id", "account_id=?1"),
    fact!(execution_input, "id TEXT,account_id INTEGER,curr_target_state TEXT", "id", "account_id=?1"),
    fact!(execution_input_target, "input_id TEXT,role TEXT,map_key TEXT,value REAL", "input_id,role,map_key", "input_id IN (SELECT id FROM execution_input WHERE account_id=?1)"),
    fact!(execution_symbol_collection, "record_id INTEGER,path TEXT,node_kind TEXT,member_count INTEGER", "record_id,path", RECORD),
    fact!(execution_symbol_result, "record_id INTEGER,path TEXT,ordinal INTEGER,map_key TEXT,node_kind TEXT,status TEXT,error TEXT,outcome TEXT,outcome_reason TEXT,reason_code TEXT,memory TEXT,sizing_recorded INTEGER,tick_recorded INTEGER", "record_id,path", RECORD),
    fact!(execution_symbol_sizing, "record_id INTEGER,path TEXT,sizing_mode TEXT,status TEXT,target_quantity REAL,current_quantity REAL,equity REAL,unit_notional REAL,reference_price REAL,unit_multiplier REAL,conversion_recorded INTEGER", "record_id,path", RECORD),
    fact!(execution_symbol_conversion, "record_id INTEGER,path TEXT,rate REAL", "record_id,path", RECORD),
    fact!(execution_symbol_tick, "record_id INTEGER,path TEXT,ordinal INTEGER,map_key TEXT,bid_price REAL,ask_price REAL,last_price REAL,book_valid INTEGER", "record_id,path", RECORD),
    fact!(execution_order_collection, "record_id INTEGER,path TEXT,kind TEXT,item_count INTEGER", "record_id,path", RECORD),
    fact!(execution_order, "record_id INTEGER,path TEXT,ordinal INTEGER,order_id JSON,direction TEXT,filled_volume REAL", "record_id,path,ordinal", RECORD),
    fact!(business_trade_collection, "record_id INTEGER,path TEXT,parent_path TEXT,state TEXT,member_count INTEGER,malformed_value TEXT", "record_id,path", RECORD),
    fact!(business_trade, "record_id INTEGER,path TEXT,collection_path TEXT,ordinal INTEGER,symbol TEXT,trade_id JSON,order_id JSON,trade_time TEXT,trade_price REAL,trade_volume REAL,extra TEXT,fee REAL,fee_state TEXT,fee_currency TEXT,conversion_recorded INTEGER", "record_id,path", RECORD),
    fact!(business_trade_conversion, "record_id INTEGER,trade_path TEXT,rate REAL,value_currency TEXT,state TEXT,malformed_value TEXT", "record_id,trade_path", RECORD),
    fact!(business_asset_binding, "record_id INTEGER,path TEXT,asset_id TEXT", "record_id,path", RECORD),
    fact!(business_asset_snapshot, "asset_id TEXT,positions_kind TEXT,positions_count INTEGER,source TEXT,total_asset REAL,observation_valid INTEGER,currency TEXT", "asset_id", "asset_id IN (SELECT asset_id FROM business_asset_binding WHERE record_id IN (SELECT id FROM executerecord WHERE account_id=?1))"),
    fact!(business_asset_position, "asset_id TEXT,ordinal INTEGER,symbol TEXT,volume REAL,direction TEXT,market_value REAL,extra TEXT,volume_state TEXT,market_value_state TEXT", "asset_id,ordinal", "asset_id IN (SELECT asset_id FROM business_asset_binding WHERE record_id IN (SELECT id FROM executerecord WHERE account_id=?1))"),
    fact!(target_weight_snapshot, "id INTEGER,account_id INTEGER,execution_id TEXT,normalized_weights_kind TEXT", "id", "account_id=?1"),
    fact!(target_weight_snapshot_weight, "snapshot_id INTEGER,role TEXT,map_key TEXT,value REAL", "snapshot_id,role,map_key", "snapshot_id IN (SELECT id FROM target_weight_snapshot WHERE account_id=?1)"),
    fact!(portfolioaccount, "id INTEGER,account_id INTEGER,created_at TEXT,portfolio_id INTEGER", "id", "account_id=?1"),
    fact!(schedule_skip, "id INTEGER,account_id INTEGER,triggered_at TEXT,reason_code TEXT", "id", "account_id=?1"),
    fact!(analysis_calendar_day, "channel TEXT,day TEXT,status TEXT,calendar_id TEXT,label TEXT", "channel,day", "channel=(SELECT trade_channel FROM account WHERE id=?1)"),
];
