CREATE TABLE account_analysis (
	account_id INTEGER NOT NULL, 
	source_version INTEGER DEFAULT '1' NOT NULL, 
	published_version INTEGER DEFAULT '0' NOT NULL, 
	current_snapshot TEXT, 
	previous_snapshot TEXT, 
	requested BOOLEAN DEFAULT '0' NOT NULL, 
	running_version INTEGER, 
	failures INTEGER DEFAULT '0' NOT NULL, 
	retry_at FLOAT DEFAULT '0' NOT NULL, 
	error TEXT, 
	logic_version TEXT, 
	engine_version TEXT, 
	PRIMARY KEY (account_id), 
	FOREIGN KEY(account_id) REFERENCES account (id) ON DELETE CASCADE
);

CREATE TABLE performance_snapshot (
	id TEXT NOT NULL, 
	account_id INTEGER NOT NULL, 
	source_version INTEGER NOT NULL, 
	logic_version TEXT NOT NULL, 
	engine_version TEXT NOT NULL, 
	computed_at TEXT NOT NULL, 
	data_until TEXT, 
	settings JSON NOT NULL, 
	ranges JSON NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(account_id) REFERENCES account (id) ON DELETE CASCADE
);

CREATE TABLE performance_cost_execution (
	snapshot_id TEXT NOT NULL, 
	record_id INTEGER NOT NULL, 
	time FLOAT NOT NULL, 
	success BOOLEAN NOT NULL, 
	noop BOOLEAN NOT NULL, 
	payload JSON NOT NULL, 
	PRIMARY KEY (snapshot_id, record_id), 
	FOREIGN KEY(snapshot_id) REFERENCES performance_snapshot (id) ON DELETE CASCADE
);

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
	payload JSON NOT NULL, 
	PRIMARY KEY (snapshot_id, id), 
	FOREIGN KEY(snapshot_id) REFERENCES performance_snapshot (id) ON DELETE CASCADE
);

CREATE INDEX ix_performance_snapshot_account_id ON performance_snapshot (account_id);

CREATE INDEX ix_performance_execution_time ON performance_cost_execution (snapshot_id, time);

CREATE INDEX ix_performance_trade_record ON performance_cost_trade (snapshot_id, record_id, time);

CREATE INDEX ix_performance_trade_symbol ON performance_cost_trade (snapshot_id, symbol, time);
