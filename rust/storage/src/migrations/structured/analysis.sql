CREATE TABLE performance_snapshot (
	id TEXT NOT NULL,
	account_id INTEGER NOT NULL,
	source_version INTEGER NOT NULL,
	logic_version TEXT NOT NULL,
	engine_version TEXT NOT NULL,
	computed_at TEXT NOT NULL,
	data_until TEXT,
	cost_schema_version INTEGER NOT NULL DEFAULT 1 CHECK (cost_schema_version=1), result_schema_version INTEGER NOT NULL DEFAULT 2 CHECK (result_schema_version=2), backtest_weight_type TEXT, backtest_fee_rate FLOAT, read_view_schema_version INTEGER NOT NULL DEFAULT 1 CHECK (read_view_schema_version=1),
	PRIMARY KEY (id),
	FOREIGN KEY(account_id) REFERENCES account (id) ON DELETE CASCADE
);

CREATE INDEX ix_performance_snapshot_account_id ON performance_snapshot (account_id);

CREATE TABLE performance_cost_execution (
	snapshot_id TEXT NOT NULL,
	record_id INTEGER NOT NULL,
	time FLOAT NOT NULL,
	success BOOLEAN NOT NULL,
	noop BOOLEAN NOT NULL,
	"key" TEXT, "execution_id" TEXT, "created_at" TEXT, "is_success" INTEGER, "duration_sec" FLOAT, "trade_count" INTEGER, "symbol_count" INTEGER, "position_count" INTEGER, "positions_present" BOOLEAN DEFAULT '0' NOT NULL, "reason" TEXT, "raw_present" INTEGER DEFAULT '0' NOT NULL, "has_symbol_results" BOOLEAN DEFAULT '0' NOT NULL, "raw_status" TEXT, "raw_task_status" TEXT, "raw_error" TEXT, "raw_outcome" TEXT, "raw_outcome_reason" TEXT, "raw_reason_code" TEXT, "raw_msg" TEXT, "raw_execution_kind" TEXT, "value" REAL, "cost" REAL, "loss_bp" REAL, "coverage" REAL, "covered" INTEGER, "count" INTEGER, "fee_covered" INTEGER, "amount_complete" BOOLEAN, "covered_value" REAL, "estimated" INTEGER, "has_transactions" BOOLEAN,
	PRIMARY KEY (snapshot_id, record_id),
	FOREIGN KEY(snapshot_id) REFERENCES performance_snapshot (id) ON DELETE CASCADE
);

CREATE INDEX ix_performance_execution_summary_time ON performance_cost_execution(snapshot_id,time,record_id);

CREATE INDEX ix_performance_execution_time ON performance_cost_execution (snapshot_id, time);

CREATE TABLE performance_cost_trade (
	snapshot_id TEXT NOT NULL,
	id INTEGER NOT NULL,
	record_id INTEGER NOT NULL,
	symbol TEXT NOT NULL,
	time FLOAT NOT NULL,
	day TEXT NOT NULL,
	value FLOAT,
	cost FLOAT,
	loss_bp FLOAT,
	fee FLOAT,
	fee_currency TEXT,
	estimated BOOLEAN NOT NULL,
	"side" TEXT DEFAULT 'none' NOT NULL, "quantity" FLOAT, "price" FLOAT, "reference" FLOAT, "reference_source" TEXT,
	PRIMARY KEY (snapshot_id, id),
	FOREIGN KEY(snapshot_id) REFERENCES performance_snapshot (id) ON DELETE CASCADE
);

CREATE INDEX ix_performance_trade_record ON performance_cost_trade (snapshot_id, record_id, time);

CREATE INDEX ix_performance_trade_symbol ON performance_cost_trade (snapshot_id, symbol, time);

CREATE TABLE performance_cost_execution_symbol(snapshot_id TEXT NOT NULL,record_id INTEGER NOT NULL,symbol TEXT NOT NULL,present INTEGER NOT NULL,status TEXT,error TEXT,outcome TEXT,outcome_reason TEXT,reason_code TEXT, "memory_message" TEXT, "memory_reason_code" TEXT,PRIMARY KEY(snapshot_id,record_id,symbol),FOREIGN KEY(snapshot_id) REFERENCES performance_snapshot(id) ON DELETE CASCADE);

CREATE TABLE performance_cost_position(snapshot_id TEXT NOT NULL,record_id INTEGER NOT NULL,idx INTEGER NOT NULL,symbol TEXT,quantity FLOAT,direction TEXT,PRIMARY KEY(snapshot_id,record_id,idx),FOREIGN KEY(snapshot_id) REFERENCES performance_snapshot(id) ON DELETE CASCADE);

CREATE TABLE performance_cost_attempt(snapshot_id TEXT NOT NULL,record_id INTEGER NOT NULL,idx INTEGER NOT NULL,symbol TEXT,planned FLOAT,filled FLOAT,reason TEXT,PRIMARY KEY(snapshot_id,record_id,idx),FOREIGN KEY(snapshot_id) REFERENCES performance_snapshot(id) ON DELETE CASCADE);

CREATE TABLE performance_cost_transaction(snapshot_id TEXT NOT NULL,record_id INTEGER NOT NULL,idx INTEGER NOT NULL,symbol TEXT,side TEXT,quantity FLOAT,price FLOAT,reference FLOAT,reference_source TEXT,time FLOAT,end_time FLOAT,time_estimated BOOLEAN NOT NULL,before FLOAT,target FLOAT,value FLOAT,cost FLOAT,loss_bp FLOAT,coverage FLOAT,covered INTEGER,count INTEGER,amount_complete BOOLEAN NOT NULL,fee_covered INTEGER,PRIMARY KEY(snapshot_id,record_id,idx),FOREIGN KEY(snapshot_id) REFERENCES performance_snapshot(id) ON DELETE CASCADE);

CREATE TABLE performance_cost_transaction_fee(snapshot_id TEXT NOT NULL,record_id INTEGER NOT NULL,transaction_idx INTEGER NOT NULL,currency TEXT NOT NULL,value FLOAT NOT NULL,PRIMARY KEY(snapshot_id,record_id,transaction_idx,currency),FOREIGN KEY(snapshot_id) REFERENCES performance_snapshot(id) ON DELETE CASCADE);

CREATE TABLE performance_execution_fee(snapshot_id TEXT NOT NULL REFERENCES performance_snapshot(id) ON DELETE CASCADE,record_id INTEGER NOT NULL,currency TEXT NOT NULL,value FLOAT NOT NULL,count INTEGER NOT NULL,PRIMARY KEY(snapshot_id,record_id,currency));

CREATE TABLE performance_result_range(snapshot_id TEXT NOT NULL,range_key TEXT NOT NULL,backtest_included BOOLEAN NOT NULL,engine_version TEXT NOT NULL,baseline TEXT,end TEXT,record_count INTEGER NOT NULL,observation_count INTEGER NOT NULL,used_record_count INTEGER NOT NULL,target_used_record_count INTEGER NOT NULL,invalid_asset_count INTEGER NOT NULL,sizing_fallback_count INTEGER,calendar_status TEXT NOT NULL,calendar_id TEXT,calendar_label TEXT,event_count INTEGER NOT NULL,PRIMARY KEY(snapshot_id,range_key),FOREIGN KEY(snapshot_id) REFERENCES performance_snapshot(id) ON DELETE CASCADE);

CREATE TABLE performance_result_point(snapshot_id TEXT NOT NULL,range_key TEXT NOT NULL,idx INTEGER NOT NULL,date TEXT NOT NULL,observed_at TEXT NOT NULL,record_id INTEGER,execution_id TEXT,account_return FLOAT,account_equity FLOAT,portfolio_return FLOAT,account_daily_return FLOAT,portfolio_daily_return FLOAT,target_portfolio_return FLOAT,target_portfolio_daily_return FLOAT,difference FLOAT,sizing_fallback BOOLEAN NOT NULL,PRIMARY KEY(snapshot_id,range_key,idx),FOREIGN KEY(snapshot_id) REFERENCES performance_snapshot(id) ON DELETE CASCADE);

CREATE TABLE performance_result_skip(snapshot_id TEXT NOT NULL,range_key TEXT NOT NULL,kind TEXT NOT NULL,count INTEGER NOT NULL,missing_target INTEGER NOT NULL,missing_ticks INTEGER NOT NULL,first_time TEXT,last_time TEXT,PRIMARY KEY(snapshot_id,range_key,kind),FOREIGN KEY(snapshot_id) REFERENCES performance_snapshot(id) ON DELETE CASCADE);

CREATE TABLE performance_result_fallback(snapshot_id TEXT NOT NULL,range_key TEXT NOT NULL,idx INTEGER NOT NULL,start TEXT NOT NULL,end TEXT NOT NULL,count INTEGER NOT NULL,PRIMARY KEY(snapshot_id,range_key,idx),FOREIGN KEY(snapshot_id) REFERENCES performance_snapshot(id) ON DELETE CASCADE);

CREATE TABLE performance_result_binding(snapshot_id TEXT NOT NULL,range_key TEXT NOT NULL,idx INTEGER NOT NULL,time TEXT NOT NULL,portfolio_id INTEGER,PRIMARY KEY(snapshot_id,range_key,idx),FOREIGN KEY(snapshot_id) REFERENCES performance_snapshot(id) ON DELETE CASCADE);

CREATE TABLE performance_result_calendar_range(snapshot_id TEXT NOT NULL,range_key TEXT NOT NULL,kind TEXT NOT NULL,idx INTEGER NOT NULL,start TEXT NOT NULL,end TEXT NOT NULL,PRIMARY KEY(snapshot_id,range_key,kind,idx),FOREIGN KEY(snapshot_id) REFERENCES performance_snapshot(id) ON DELETE CASCADE);

CREATE TABLE performance_result_event(snapshot_id TEXT NOT NULL,range_key TEXT NOT NULL,idx INTEGER NOT NULL,time TEXT NOT NULL,tag TEXT NOT NULL,text TEXT NOT NULL,execution_id TEXT,PRIMARY KEY(snapshot_id,range_key,idx),FOREIGN KEY(snapshot_id) REFERENCES performance_snapshot(id) ON DELETE CASCADE);

CREATE TABLE performance_result_daily_cost(snapshot_id TEXT NOT NULL,range_key TEXT NOT NULL,day TEXT NOT NULL,value FLOAT,cost FLOAT,loss_bp FLOAT,coverage FLOAT,covered_value FLOAT,covered INTEGER,count INTEGER,fee_covered INTEGER,estimated INTEGER,amount_complete BOOLEAN NOT NULL,present INTEGER NOT NULL,PRIMARY KEY(snapshot_id,range_key,day),FOREIGN KEY(snapshot_id) REFERENCES performance_snapshot(id) ON DELETE CASCADE);

CREATE TABLE performance_result_daily_fee(snapshot_id TEXT NOT NULL,range_key TEXT NOT NULL,day TEXT NOT NULL,currency TEXT NOT NULL,value FLOAT NOT NULL,PRIMARY KEY(snapshot_id,range_key,day,currency),FOREIGN KEY(snapshot_id) REFERENCES performance_snapshot(id) ON DELETE CASCADE);

CREATE TABLE analysis_calendar_day(channel TEXT NOT NULL,day TEXT NOT NULL,status TEXT NOT NULL,calendar_id TEXT,label TEXT,PRIMARY KEY(channel,day));
