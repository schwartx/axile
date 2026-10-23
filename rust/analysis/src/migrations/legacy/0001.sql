CREATE TABLE execution_artifact (
	execution_id TEXT NOT NULL, 
	artifact_type VARCHAR(23) NOT NULL, 
	schema_version INTEGER NOT NULL, 
	content JSON NOT NULL, 
	created_at TEXT NOT NULL, 
	id INTEGER NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_execution_artifact_execution_type UNIQUE (execution_id, artifact_type)
);

CREATE TABLE portfolio (
	name TEXT NOT NULL, 
	market TEXT NOT NULL, 
	description TEXT, 
	custom_calc_py_code TEXT NOT NULL, 
	status TEXT, 
	tag TEXT, 
	id INTEGER NOT NULL, 
	updated_at TEXT NOT NULL, 
	created_at TEXT NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE account (
	name TEXT NOT NULL, 
	market TEXT NOT NULL, 
	trade_channel TEXT NOT NULL, 
	account_control_preset TEXT NOT NULL, 
	account_control_override JSON, 
	account_config JSON NOT NULL, 
	is_started BOOLEAN NOT NULL, 
	cron_expr TEXT NOT NULL, 
	remark TEXT, 
	brokerage TEXT NOT NULL, 
	weight_precision FLOAT NOT NULL, 
	long_leverage FLOAT, 
	short_leverage FLOAT, 
	algorithm JSON, 
	empty_positions_algorithm JSON, 
	trade_rules JSON, 
	forbidden_symbols JSON, 
	risk_symbols JSON, 
	feishu_key TEXT, 
	portfolio_id INTEGER, 
	write_empty_record INTEGER, 
	execution_timeout INTEGER DEFAULT '180' NOT NULL, 
	id INTEGER NOT NULL, 
	updated_at TEXT NOT NULL, 
	created_at TEXT NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(portfolio_id) REFERENCES portfolio (id) ON DELETE SET NULL
);

CREATE TABLE account_control_counter_delta (
	account_id INTEGER NOT NULL, 
	execution_id TEXT NOT NULL, 
	control_date TEXT NOT NULL, 
	bucket_type VARCHAR(6) NOT NULL, 
	bucket_start TEXT NOT NULL, 
	scope_type VARCHAR(7) NOT NULL, 
	symbol TEXT, 
	operation TEXT NOT NULL, 
	delta_count INTEGER NOT NULL, 
	delta_uid TEXT NOT NULL, 
	id INTEGER NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(account_id) REFERENCES account (id) ON DELETE CASCADE, 
	CONSTRAINT uq_account_control_counter_delta_delta_uid UNIQUE (delta_uid)
);

CREATE TABLE account_control_event (
	account_id INTEGER NOT NULL, 
	control_date TEXT NOT NULL, 
	execution_id TEXT NOT NULL, 
	seq INTEGER NOT NULL, 
	channel TEXT NOT NULL, 
	operation TEXT NOT NULL, 
	symbol TEXT, 
	metadata JSON NOT NULL, 
	decision VARCHAR(7) NOT NULL, 
	counted BOOLEAN NOT NULL, 
	outcome TEXT NOT NULL, 
	event_uid TEXT NOT NULL, 
	created_at TEXT NOT NULL, 
	occurred_at_ms INTEGER NOT NULL, 
	id INTEGER NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(account_id) REFERENCES account (id) ON DELETE CASCADE, 
	CONSTRAINT uq_account_control_event_event_uid UNIQUE (event_uid)
);

CREATE TABLE executerecord (
	execution_id TEXT, 
	raw_input JSON NOT NULL, 
	raw_result JSON NOT NULL, 
	is_success INTEGER NOT NULL, 
	id INTEGER NOT NULL, 
	account_id INTEGER NOT NULL, 
	created_at TEXT NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(account_id) REFERENCES account (id)
);

CREATE TABLE execution_event (
	execution_id TEXT NOT NULL, 
	account_id INTEGER NOT NULL, 
	channel TEXT NOT NULL, 
	algorithm TEXT NOT NULL, 
	schema_version INTEGER NOT NULL, 
	event_uid TEXT NOT NULL, 
	event_type VARCHAR(31) NOT NULL, 
	status VARCHAR(7) NOT NULL, 
	reason_family VARCHAR(18) NOT NULL, 
	reason_code TEXT NOT NULL, 
	symbol TEXT, 
	intent_id TEXT, 
	order_id TEXT, 
	client_order_id TEXT, 
	ts_local_created TEXT NOT NULL, 
	ts_exchange TEXT, 
	ts_persisted TEXT NOT NULL, 
	seq INTEGER NOT NULL, 
	details JSON NOT NULL, 
	id INTEGER NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(account_id) REFERENCES account (id) ON DELETE CASCADE, 
	CONSTRAINT uq_execution_event_event_uid UNIQUE (event_uid)
);

CREATE TABLE schedule_skip (
	id INTEGER NOT NULL, 
	account_id INTEGER NOT NULL, 
	channel TEXT NOT NULL, 
	triggered_at TEXT NOT NULL, 
	calendar_id TEXT NOT NULL, 
	calendar_day DATE NOT NULL, 
	calendar_label TEXT NOT NULL, 
	reason_code TEXT NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(account_id) REFERENCES account (id) ON DELETE CASCADE
);

CREATE TABLE portfolioaccount (
	account_id INTEGER NOT NULL, 
	portfolio_id INTEGER, 
	created_at TEXT NOT NULL, 
	id INTEGER NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(account_id) REFERENCES account (id) ON DELETE CASCADE, 
	FOREIGN KEY(portfolio_id) REFERENCES portfolio (id) ON DELETE CASCADE
);

CREATE INDEX ix_execution_artifact_execution ON execution_artifact (execution_id);

CREATE INDEX ix_account_control_counter_delta_account_date ON account_control_counter_delta (account_id, control_date);

CREATE INDEX ix_account_control_counter_delta_execution ON account_control_counter_delta (execution_id);

CREATE INDEX ix_account_control_event_account_date_created ON account_control_event (account_id, control_date, created_at);

CREATE INDEX ix_account_control_event_account_date_symbol_created ON account_control_event (account_id, control_date, symbol, created_at);

CREATE INDEX ix_account_control_event_account_operation_occurred ON account_control_event (account_id, operation, occurred_at_ms);

CREATE INDEX ix_account_control_event_account_symbol_operation_occurred ON account_control_event (account_id, symbol, operation, occurred_at_ms);

CREATE INDEX ix_account_control_event_execution_seq ON account_control_event (execution_id, seq);

CREATE UNIQUE INDEX ix_executerecord_execution_id ON executerecord (execution_id);

CREATE INDEX ix_executerecord_account_id_created_at ON executerecord (account_id, created_at);

CREATE INDEX ix_executerecord_account_id_id ON executerecord (account_id, id);

CREATE INDEX ix_executerecord_account_id_success_id ON executerecord (account_id, is_success, id);

CREATE INDEX ix_execution_event_account_created ON execution_event (account_id, ts_local_created);

CREATE INDEX ix_execution_event_execution_seq ON execution_event (execution_id, seq);

CREATE INDEX ix_execution_event_reason_created ON execution_event (reason_code, ts_local_created);

CREATE INDEX ix_execution_event_symbol_created ON execution_event (symbol, ts_local_created);

CREATE INDEX ix_execution_event_type_created ON execution_event (event_type, ts_local_created);

CREATE INDEX ix_schedule_skip_account_triggered ON schedule_skip (account_id, triggered_at);
