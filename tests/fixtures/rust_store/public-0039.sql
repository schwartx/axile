-- Generated from frozen Axile 509b3627ca5fc6ee5f5a72e91b5ebebed3cf9879 (public head 0039), empty SQLite.
CREATE INDEX ix_account_asset_snapshot_account_created ON account_asset_snapshot (account_id, created_at);

CREATE INDEX ix_account_asset_snapshot_account_id_id ON account_asset_snapshot (account_id, id);

CREATE UNIQUE INDEX ix_account_asset_snapshot_execution_id ON account_asset_snapshot (execution_id);

CREATE INDEX ix_account_control_counter_delta_account_date ON account_control_counter_delta (account_id, control_date);

CREATE INDEX ix_account_control_counter_delta_execution ON account_control_counter_delta (execution_id);

CREATE INDEX ix_account_control_event_account_date_created ON account_control_event (account_id, control_date, created_at);

CREATE INDEX ix_account_control_event_account_date_symbol_created ON account_control_event (account_id, control_date, symbol, created_at);

CREATE INDEX ix_account_control_event_account_operation_occurred ON account_control_event (account_id, operation, occurred_at_ms);

CREATE INDEX ix_account_control_event_account_symbol_operation_occurred ON account_control_event (account_id, symbol, operation, occurred_at_ms);

CREATE INDEX ix_account_control_event_execution_seq ON account_control_event (execution_id, seq);

CREATE INDEX ix_account_control_session_account_id ON account_control_session (account_id);

CREATE INDEX ix_account_runtime_sync_attempt_account ON accountruntimesyncattempt (account_id);

CREATE INDEX ix_business_asset_binding_asset ON business_asset_binding (asset_id);

CREATE UNIQUE INDEX ix_business_asset_snapshot_account_snapshot ON business_asset_snapshot (account_snapshot_id);

CREATE UNIQUE INDEX ix_business_asset_snapshot_input_path ON business_asset_snapshot (input_id, path);

CREATE UNIQUE INDEX ix_business_asset_snapshot_record_path ON business_asset_snapshot (record_id, path);

CREATE INDEX ix_executerecord_account_id_created_at ON executerecord (account_id, created_at);

CREATE INDEX ix_executerecord_account_id_id ON executerecord (account_id, id);

CREATE INDEX ix_executerecord_account_id_success_id ON executerecord (account_id, is_success, id);

CREATE UNIQUE INDEX ix_executerecord_execution_id ON executerecord (execution_id);

CREATE INDEX ix_execution_artifact_execution ON execution_artifact (execution_id);

CREATE INDEX ix_execution_event_account_created ON execution_event (account_id, ts_local_created);

CREATE INDEX ix_execution_event_execution_seq ON execution_event (execution_id, seq);

CREATE INDEX ix_execution_event_reason_created ON execution_event (reason_code, ts_local_created);

CREATE INDEX ix_execution_event_symbol_created ON execution_event (symbol, ts_local_created);

CREATE INDEX ix_execution_event_type_created ON execution_event (event_type, ts_local_created);

CREATE INDEX ix_execution_input_account_id_created_at ON execution_input (account_id, created_at);

CREATE INDEX ix_execution_input_owner_execution_id ON execution_input (owner_execution_id);

CREATE INDEX ix_execution_intent_account_id_created_at ON execution_intent (account_id, created_at);

CREATE UNIQUE INDEX ix_execution_intent_execution_id ON execution_intent (execution_id);

CREATE INDEX ix_execution_symbol_result_record_ordinal ON execution_symbol_result (record_id, ordinal);

CREATE INDEX ix_order_submission_account_time ON order_submission (account_id, occurred_at_ms);

CREATE INDEX ix_performance_execution_summary_time ON performance_cost_execution(snapshot_id,time,record_id);

CREATE INDEX ix_performance_execution_time ON performance_cost_execution (snapshot_id, time);

CREATE INDEX ix_performance_snapshot_account_id ON performance_snapshot (account_id);

CREATE INDEX ix_performance_trade_record ON performance_cost_trade (snapshot_id, record_id, time);

CREATE INDEX ix_performance_trade_symbol ON performance_cost_trade (snapshot_id, symbol, time);

CREATE INDEX ix_schedule_skip_account_triggered ON schedule_skip (account_id, triggered_at);

CREATE INDEX ix_submission_warning_account_time ON order_submission_warning (account_id, occurred_at_ms);

CREATE INDEX ix_supplement_step_execution_id ON supplement_step (execution_id);

CREATE INDEX ix_target_weight_snapshot_account_id_id ON target_weight_snapshot (account_id, id);

CREATE UNIQUE INDEX ix_target_weight_snapshot_execution_id ON target_weight_snapshot (execution_id);

CREATE INDEX ix_target_weight_snapshot_portfolio_id_id ON target_weight_snapshot (portfolio_id, id);

CREATE UNIQUE INDEX uq_intent_account_queued ON execution_intent (account_id) WHERE status = 'QUEUED';

CREATE UNIQUE INDEX uq_intent_account_running ON execution_intent (account_id) WHERE status IN ('RUNNING', 'TERMINATING');

CREATE TABLE _futures_close_policy_backup (
	account_id INTEGER NOT NULL, 
	previous_execution JSON NOT NULL, 
	migrated_execution JSON NOT NULL, 
	PRIMARY KEY (account_id)
);

CREATE TABLE account (
	name TEXT NOT NULL, 
	market TEXT NOT NULL, 
	trade_channel TEXT NOT NULL, 
	remark TEXT, 
	brokerage TEXT NOT NULL, 
	portfolio_id INTEGER, 
	id INTEGER NOT NULL, 
	updated_at TEXT NOT NULL, 
	created_at TEXT NOT NULL, copied_from_account_id INTEGER, copied_from_account_name TEXT, 
	PRIMARY KEY (id), 
	FOREIGN KEY(portfolio_id) REFERENCES portfolio (id) ON DELETE SET NULL
);

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
	engine_version TEXT, running_token TEXT, 
	PRIMARY KEY (account_id), 
	FOREIGN KEY(account_id) REFERENCES account (id) ON DELETE CASCADE
);

CREATE TABLE account_asset_snapshot (
	id INTEGER NOT NULL, 
	account_id INTEGER NOT NULL, 
	source TEXT NOT NULL, 
	execution_id TEXT, 
	created_at TEXT NOT NULL, asset_fact_id TEXT REFERENCES "business_asset_snapshot" ("asset_id") ON DELETE SET NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(account_id) REFERENCES account (id) ON DELETE CASCADE
);

CREATE TABLE account_connection_state (
	account_id INTEGER NOT NULL, 
	config_key TEXT NOT NULL, 
	instance_id TEXT NOT NULL, 
	sequence INTEGER NOT NULL, 
	status TEXT NOT NULL, 
	observed_at TEXT, 
	message TEXT, 
	error JSON, label TEXT, presentation JSON, last_success_at TEXT, last_failure JSON, manually_disconnected BOOLEAN DEFAULT 0 NOT NULL, 
	PRIMARY KEY (account_id), 
	FOREIGN KEY(account_id) REFERENCES account (id) ON DELETE CASCADE
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
	id INTEGER NOT NULL, context_kind TEXT DEFAULT 'unknown' NOT NULL, source TEXT DEFAULT 'unknown' NOT NULL, 
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
	id INTEGER NOT NULL, context_kind TEXT DEFAULT 'unknown' NOT NULL, source TEXT DEFAULT 'unknown' NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(account_id) REFERENCES account (id) ON DELETE CASCADE, 
	CONSTRAINT uq_account_control_event_event_uid UNIQUE (event_uid)
);

CREATE TABLE account_control_session (
	id INTEGER NOT NULL, 
	instance_id VARCHAR NOT NULL, 
	account_id INTEGER NOT NULL, 
	execution_id VARCHAR NOT NULL, 
	timezone VARCHAR NOT NULL, 
	started_at_ms INTEGER NOT NULL, 
	finished_at_ms INTEGER, 
	status VARCHAR NOT NULL, context_kind TEXT DEFAULT 'unknown' NOT NULL, source TEXT DEFAULT 'unknown' NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (instance_id), 
	FOREIGN KEY(account_id) REFERENCES account (id) ON DELETE CASCADE
);

CREATE TABLE "account_notification_state" (
	account_id INTEGER NOT NULL, 
	last_success_at TEXT, 
	execution_id TEXT, 
	last_attempt_at TEXT, 
	last_attempt_execution_id TEXT, 
	last_attempt_ok BOOLEAN, 
	last_attempt_error TEXT, last_attempt_event_type VARCHAR, 
	PRIMARY KEY (account_id), 
	FOREIGN KEY(account_id) REFERENCES account (id) ON DELETE CASCADE
);

CREATE TABLE account_settings (
	account_id INTEGER NOT NULL, 
	connection JSON NOT NULL, 
	execution JSON NOT NULL, 
	schedule JSON NOT NULL, 
	notification JSON NOT NULL, 
	backtest JSON NOT NULL, 
	PRIMARY KEY (account_id), 
	FOREIGN KEY(account_id) REFERENCES account (id) ON DELETE CASCADE
);

CREATE TABLE accountruntimesync (
	id INTEGER NOT NULL, 
	account_id INTEGER NOT NULL, 
	revision INTEGER NOT NULL, 
	status TEXT NOT NULL, 
	reset_worker BOOLEAN NOT NULL, 
	create_request_key TEXT, 
	attempts INTEGER NOT NULL, 
	last_error TEXT, 
	requested_at TEXT NOT NULL, 
	last_attempt_at TEXT, 
	synchronized_at TEXT, 
	PRIMARY KEY (id), 
	FOREIGN KEY(account_id) REFERENCES account (id) ON DELETE CASCADE, 
	CONSTRAINT uq_account_runtime_sync_account UNIQUE (account_id), 
	UNIQUE (create_request_key)
);

CREATE TABLE accountruntimesyncattempt (
	id INTEGER NOT NULL, 
	account_id INTEGER NOT NULL, 
	revision INTEGER NOT NULL, 
	succeeded BOOLEAN NOT NULL, 
	error TEXT, 
	attempted_at TEXT NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(account_id) REFERENCES account (id) ON DELETE CASCADE
);

CREATE TABLE alembic_version (
	version_num VARCHAR(32) NOT NULL, 
	CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
);

CREATE TABLE analysis_calendar_day (
	channel TEXT NOT NULL, 
	day TEXT NOT NULL, 
	status TEXT NOT NULL, 
	calendar_id TEXT, 
	label TEXT, 
	PRIMARY KEY (channel, day)
);

CREATE TABLE axile_0013_error_backup (
	id INTEGER NOT NULL, 
	table_name TEXT NOT NULL, 
	row_id TEXT NOT NULL, 
	json_column TEXT NOT NULL, 
	path JSON NOT NULL, 
	before_present BOOLEAN NOT NULL, 
	before_value JSON, 
	after_present BOOLEAN NOT NULL, 
	after_value JSON, 
	rule TEXT NOT NULL, 
	identity JSON NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE axile_0014_market_value_backup (
	id INTEGER NOT NULL, 
	table_name TEXT NOT NULL, 
	row_id INTEGER NOT NULL, 
	json_column TEXT NOT NULL, 
	"before" JSON NOT NULL, 
	"after" JSON NOT NULL, 
	execution_id TEXT, 
	PRIMARY KEY (id)
);

CREATE TABLE axile_0015_tq_market_value_backup (
	id INTEGER NOT NULL, 
	table_name TEXT NOT NULL, 
	row_id INTEGER NOT NULL, 
	json_column TEXT NOT NULL, 
	"before" JSON NOT NULL, 
	"after" JSON NOT NULL, 
	execution_id TEXT, 
	PRIMARY KEY (id)
);

CREATE TABLE business_asset_binding (
	record_id INTEGER NOT NULL, 
	path TEXT NOT NULL, 
	asset_id TEXT NOT NULL, 
	scope TEXT NOT NULL, 
	ordinal INTEGER NOT NULL, 
	map_key TEXT, 
	PRIMARY KEY (record_id, path), 
	FOREIGN KEY(record_id) REFERENCES executerecord (id) ON DELETE CASCADE, 
	FOREIGN KEY(asset_id) REFERENCES business_asset_snapshot (asset_id) ON DELETE RESTRICT
);

CREATE TABLE business_asset_position (
	asset_id TEXT NOT NULL, 
	ordinal INTEGER NOT NULL, 
	payload_kind TEXT NOT NULL, 
	payload_value JSON, 
	audit_evidence JSON NOT NULL, 
	volume_state TEXT NOT NULL, 
	available_volume_state TEXT NOT NULL, 
	market_value_state TEXT NOT NULL, 
	avg_price_state TEXT NOT NULL, 
	symbol TEXT, 
	volume FLOAT, 
	available_volume FLOAT, 
	market_value FLOAT, 
	direction TEXT, 
	avg_price FLOAT, 
	extra JSON, 
	PRIMARY KEY (asset_id, ordinal), 
	FOREIGN KEY(asset_id) REFERENCES business_asset_snapshot (asset_id) ON DELETE CASCADE
);

CREATE TABLE business_asset_snapshot (
	asset_id TEXT NOT NULL, 
	record_id INTEGER, 
	input_id TEXT, 
	account_snapshot_id INTEGER, 
	path TEXT NOT NULL, 
	scope TEXT NOT NULL, 
	ordinal INTEGER NOT NULL, 
	map_key TEXT, 
	payload_kind TEXT NOT NULL, 
	payload_value JSON, 
	audit_evidence JSON NOT NULL, 
	cash_state TEXT NOT NULL, 
	equity_state TEXT NOT NULL, 
	market_state TEXT NOT NULL, 
	positions_kind TEXT NOT NULL, 
	positions_count INTEGER, 
	positions_value JSON, 
	observation_valid BOOLEAN NOT NULL, 
	available_cash FLOAT, 
	total_asset FLOAT, 
	market_value FLOAT, 
	currency TEXT, 
	update_time TEXT, 
	source TEXT, 
	extra JSON, 
	PRIMARY KEY (asset_id), 
	CONSTRAINT ck_business_asset_snapshot_one_owner CHECK ((record_id IS NOT NULL) + (input_id IS NOT NULL) + (account_snapshot_id IS NOT NULL) = 1), 
	FOREIGN KEY(record_id) REFERENCES executerecord (id) ON DELETE CASCADE, 
	FOREIGN KEY(input_id) REFERENCES execution_input (id) ON DELETE CASCADE, 
	FOREIGN KEY(account_snapshot_id) REFERENCES account_asset_snapshot (id) ON DELETE CASCADE
);

CREATE TABLE business_trade (
	record_id INTEGER NOT NULL, 
	path TEXT NOT NULL, 
	collection_path TEXT NOT NULL, 
	ordinal INTEGER NOT NULL, 
	state TEXT NOT NULL, 
	malformed_value JSON, 
	symbol TEXT, 
	trade_time TEXT, 
	trade_id JSON, 
	order_id JSON, 
	trade_volume FLOAT, 
	trade_price FLOAT, 
	trade_value FLOAT, 
	extra JSON, 
	trade_id_state TEXT NOT NULL, 
	order_id_state TEXT NOT NULL, 
	conversion_recorded BOOLEAN NOT NULL, 
	fee FLOAT, 
	fee_state TEXT NOT NULL, 
	fee_currency TEXT, 
	audit_evidence JSON NOT NULL, 
	PRIMARY KEY (record_id, path), 
	FOREIGN KEY(record_id) REFERENCES executerecord (id) ON DELETE CASCADE
);

CREATE TABLE business_trade_collection (
	record_id INTEGER NOT NULL, 
	path TEXT NOT NULL, 
	parent_path TEXT NOT NULL, 
	ordinal INTEGER NOT NULL, 
	map_key TEXT, 
	state TEXT NOT NULL, 
	member_count INTEGER, 
	malformed_value JSON, 
	audit_evidence JSON NOT NULL, 
	PRIMARY KEY (record_id, path), 
	FOREIGN KEY(record_id) REFERENCES executerecord (id) ON DELETE CASCADE
);

CREATE TABLE business_trade_conversion (
	record_id INTEGER NOT NULL, 
	trade_path TEXT NOT NULL, 
	state TEXT NOT NULL, 
	malformed_value JSON, 
	price_currency TEXT, 
	value_currency TEXT, 
	rate FLOAT, 
	reference JSON, 
	audit_evidence JSON NOT NULL, 
	PRIMARY KEY (record_id, trade_path), 
	FOREIGN KEY(record_id) REFERENCES executerecord (id) ON DELETE CASCADE
);

CREATE TABLE executerecord (
	execution_id TEXT, 
	is_success INTEGER NOT NULL, 
	id INTEGER NOT NULL, 
	account_id INTEGER NOT NULL, 
	created_at TEXT NOT NULL, input_id TEXT REFERENCES execution_input(id), result_input_id TEXT REFERENCES execution_input(id), status TEXT, symbol TEXT, technical_detail TEXT, task_status TEXT, error TEXT, outcome TEXT, outcome_reason TEXT, reason_code TEXT, msg TEXT, execution_kind TEXT, channel_type TEXT, execution_time FLOAT, success BOOLEAN, display_status TEXT, display_error TEXT, display_outcome TEXT, display_outcome_reason TEXT, termination_reason TEXT, termination_mode TEXT, termination_trigger TEXT, termination_requested_at TEXT, termination_acked_at TEXT, termination_finished_at TEXT, termination_cancel_attempted BOOLEAN, termination_forced BOOLEAN, termination_cancel_unconfirmed BOOLEAN, result_input_kind TEXT NOT NULL DEFAULT 'absent', result_input_value JSON, audit_evidence JSON NOT NULL DEFAULT '{}', termination_evidence JSON NOT NULL DEFAULT '{}', termination_kind TEXT NOT NULL DEFAULT 'absent', termination_cancel_ids_kind TEXT NOT NULL DEFAULT 'absent', termination_cancel_count INTEGER, facts_complete BOOLEAN NOT NULL DEFAULT 0, 
	PRIMARY KEY (id), 
	FOREIGN KEY(account_id) REFERENCES account (id)
);

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

CREATE TABLE execution_extension (
	record_id INTEGER NOT NULL, 
	memory JSON, 
	extra JSON, 
	reconciliation JSON, 
	root_kind TEXT DEFAULT 'object' NOT NULL, 
	root_value JSON, 
	audit_evidence JSON NOT NULL, 
	PRIMARY KEY (record_id), 
	FOREIGN KEY(record_id) REFERENCES executerecord (id) ON DELETE CASCADE
);

CREATE TABLE execution_input (
	id TEXT NOT NULL, 
	account_id INTEGER NOT NULL, 
	execution_id TEXT, 
	owner_execution_id TEXT, 
	created_at TEXT NOT NULL, 
	root_kind TEXT NOT NULL, 
	root_value JSON, 
	channel_type TEXT, 
	execution_timeout INTEGER, 
	algorithm JSON, 
	symbol_algorithms JSON, 
	trade_rules JSON, 
	futures_close_policy JSON, 
	feishu_key JSON, 
	feishu_account JSON, 
	extra JSON, 
	long_leverage FLOAT, 
	short_leverage FLOAT, 
	weight_precision FLOAT, 
	curr_target_state TEXT NOT NULL, 
	last_target_state TEXT NOT NULL, 
	strategy_target_state TEXT NOT NULL, 
	forbidden_symbols_state TEXT NOT NULL, 
	risk_symbols_state TEXT NOT NULL, 
	sizing_context_state TEXT NOT NULL, 
	quantities_state TEXT NOT NULL, 
	target_units_state TEXT NOT NULL, 
	curr_target_count INTEGER, 
	last_target_count INTEGER, 
	strategy_target_count INTEGER, 
	quantities_count INTEGER, 
	target_units_count INTEGER, 
	forbidden_symbols_count INTEGER, 
	risk_symbols_count INTEGER, 
	audit_evidence JSON NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(account_id) REFERENCES account (id) ON DELETE CASCADE, 
	UNIQUE (execution_id)
);

CREATE TABLE execution_input_plan (
	input_id TEXT NOT NULL, 
	symbol TEXT NOT NULL, 
	quantity FLOAT, 
	units FLOAT, 
	quantity_state TEXT NOT NULL, 
	units_state TEXT NOT NULL, 
	audit_evidence JSON NOT NULL, 
	PRIMARY KEY (input_id, symbol), 
	FOREIGN KEY(input_id) REFERENCES execution_input (id) ON DELETE CASCADE
)
 WITHOUT ROWID;

CREATE TABLE execution_input_symbol (
	input_id TEXT NOT NULL, 
	role TEXT NOT NULL, 
	ordinal INTEGER NOT NULL, 
	value TEXT, 
	audit_evidence JSON NOT NULL, 
	PRIMARY KEY (input_id, role, ordinal), 
	FOREIGN KEY(input_id) REFERENCES execution_input (id) ON DELETE CASCADE
);

CREATE TABLE execution_input_target (
	input_id TEXT NOT NULL, 
	role TEXT NOT NULL, 
	map_key TEXT NOT NULL, 
	value FLOAT, 
	audit_evidence JSON NOT NULL, 
	PRIMARY KEY (input_id, role, map_key), 
	FOREIGN KEY(input_id) REFERENCES execution_input (id) ON DELETE CASCADE
)
 WITHOUT ROWID;

CREATE TABLE execution_intent (
	id INTEGER NOT NULL, 
	execution_id TEXT NOT NULL, 
	account_id INTEGER NOT NULL, 
	kind TEXT NOT NULL, 
	trigger_source TEXT NOT NULL, 
	status TEXT NOT NULL, 
	channel TEXT, 
	algorithm TEXT, 
	payload JSON NOT NULL, 
	created_at TEXT NOT NULL, 
	started_at TEXT, 
	finished_at TEXT, 
	error TEXT, 
	cancel_requested_at TEXT, 
	cancel_reason TEXT, 
	terminate_mode TEXT, 
	PRIMARY KEY (id), 
	FOREIGN KEY(account_id) REFERENCES account (id) ON DELETE CASCADE
);

CREATE TABLE execution_order (
	record_id INTEGER NOT NULL, 
	path TEXT NOT NULL, 
	ordinal INTEGER NOT NULL, 
	map_key TEXT, 
	kind TEXT NOT NULL, 
	invalid_value JSON, 
	order_id JSON, 
	symbol TEXT, 
	direction TEXT, 
	order_type TEXT, 
	volume FLOAT, 
	price FLOAT, 
	status TEXT, 
	filled_volume FLOAT, 
	avg_price FLOAT, 
	create_time TEXT, 
	update_time TEXT, 
	update_timestamp FLOAT, 
	remaining_volume FLOAT, 
	filled_ratio FLOAT, 
	extra JSON, 
	audit_evidence JSON NOT NULL, 
	order_id_state TEXT NOT NULL, 
	conversion_kind TEXT, 
	conversion_invalid_value JSON, 
	conversion_price_currency TEXT, 
	conversion_value_currency TEXT, 
	conversion_rate FLOAT, 
	conversion_reference JSON, 
	conversion_evidence JSON, 
	PRIMARY KEY (record_id, path, ordinal), 
	CHECK (kind IN ('object', 'null', 'invalid')), 
	CHECK (conversion_kind IS NULL OR conversion_kind IN ('object', 'null', 'invalid')), 
	FOREIGN KEY(record_id, path) REFERENCES execution_order_collection (record_id, path) ON DELETE CASCADE, 
	FOREIGN KEY(record_id) REFERENCES executerecord (id) ON DELETE CASCADE
);

CREATE TABLE execution_order_collection (
	record_id INTEGER NOT NULL, 
	path TEXT NOT NULL, 
	ordinal INTEGER NOT NULL, 
	map_key TEXT, 
	kind TEXT NOT NULL, 
	invalid_value JSON, 
	item_count INTEGER, 
	audit_evidence JSON NOT NULL, 
	PRIMARY KEY (record_id, path), 
	CHECK (kind IN ('array', 'null', 'invalid')), 
	FOREIGN KEY(record_id) REFERENCES executerecord (id) ON DELETE CASCADE
);

CREATE TABLE execution_symbol_collection (
	record_id INTEGER NOT NULL, 
	path TEXT NOT NULL, 
	ordinal INTEGER NOT NULL, 
	map_key TEXT, 
	node_kind TEXT NOT NULL, 
	invalid_value JSON, 
	audit_evidence JSON NOT NULL, 
	member_count INTEGER, 
	PRIMARY KEY (record_id, path), 
	CHECK (node_kind IN ('object', 'value')), 
	CHECK (ordinal >= 0), 
	FOREIGN KEY(record_id) REFERENCES executerecord (id) ON DELETE CASCADE
);

CREATE TABLE execution_symbol_conversion (
	record_id INTEGER NOT NULL, 
	path TEXT NOT NULL, 
	ordinal INTEGER NOT NULL, 
	map_key TEXT, 
	node_kind TEXT NOT NULL, 
	invalid_value JSON, 
	audit_evidence JSON NOT NULL, 
	price_currency TEXT, 
	value_currency TEXT, 
	rate FLOAT, 
	reference JSON, 
	PRIMARY KEY (record_id, path), 
	CHECK (node_kind IN ('object', 'value')), 
	CHECK (ordinal >= 0), 
	FOREIGN KEY(record_id) REFERENCES executerecord (id) ON DELETE CASCADE
);

CREATE TABLE execution_symbol_result (
	record_id INTEGER NOT NULL, 
	path TEXT NOT NULL, 
	ordinal INTEGER NOT NULL, 
	map_key TEXT, 
	node_kind TEXT NOT NULL, 
	invalid_value JSON, 
	audit_evidence JSON NOT NULL, 
	sizing_recorded BOOLEAN NOT NULL, 
	tick_recorded BOOLEAN NOT NULL, 
	target_volume_state TEXT NOT NULL, 
	final_volume_state TEXT NOT NULL, 
	symbol TEXT, 
	algorithm TEXT, 
	status TEXT, 
	error TEXT, 
	technical_detail TEXT, 
	reason_code TEXT, 
	outcome TEXT, 
	outcome_reason TEXT, 
	target_volume FLOAT, 
	final_volume FLOAT, 
	success BOOLEAN, 
	memory JSON, 
	PRIMARY KEY (record_id, path), 
	CHECK (node_kind IN ('object', 'value')), 
	CHECK (ordinal >= 0), 
	FOREIGN KEY(record_id) REFERENCES executerecord (id) ON DELETE CASCADE
);

CREATE TABLE execution_symbol_sizing (
	record_id INTEGER NOT NULL, 
	path TEXT NOT NULL, 
	ordinal INTEGER NOT NULL, 
	map_key TEXT, 
	node_kind TEXT NOT NULL, 
	invalid_value JSON, 
	audit_evidence JSON NOT NULL, 
	conversion_recorded BOOLEAN NOT NULL, 
	symbol TEXT, 
	sizing_mode TEXT, 
	status TEXT, 
	reason_code TEXT, 
	account_weight FLOAT, 
	equity FLOAT, 
	reference_price FLOAT, 
	unit_multiplier FLOAT, 
	unit_notional FLOAT, 
	target_notional FLOAT, 
	raw_quantity FLOAT, 
	target_quantity FLOAT, 
	current_quantity FLOAT, 
	quantity_step FLOAT, 
	min_quantity FLOAT, 
	min_notional FLOAT, 
	PRIMARY KEY (record_id, path), 
	CHECK (node_kind IN ('object', 'value')), 
	CHECK (ordinal >= 0), 
	FOREIGN KEY(record_id) REFERENCES executerecord (id) ON DELETE CASCADE
);

CREATE TABLE execution_symbol_target (
	record_id INTEGER NOT NULL, 
	path TEXT NOT NULL, 
	ordinal INTEGER NOT NULL, 
	map_key TEXT, 
	node_kind TEXT NOT NULL, 
	invalid_value JSON, 
	audit_evidence JSON NOT NULL, 
	value FLOAT, 
	PRIMARY KEY (record_id, path), 
	CHECK (node_kind IN ('object', 'value')), 
	CHECK (ordinal >= 0), 
	FOREIGN KEY(record_id) REFERENCES executerecord (id) ON DELETE CASCADE
);

CREATE TABLE execution_symbol_tick (
	record_id INTEGER NOT NULL, 
	path TEXT NOT NULL, 
	ordinal INTEGER NOT NULL, 
	map_key TEXT, 
	node_kind TEXT NOT NULL, 
	invalid_value JSON, 
	audit_evidence JSON NOT NULL, 
	symbol TEXT, 
	last_price FLOAT, 
	bid_price FLOAT, 
	bid_price_2 FLOAT, 
	bid_price_3 FLOAT, 
	bid_price_4 FLOAT, 
	bid_price_5 FLOAT, 
	bid_volume FLOAT, 
	bid_volume_2 FLOAT, 
	bid_volume_3 FLOAT, 
	bid_volume_4 FLOAT, 
	bid_volume_5 FLOAT, 
	ask_price FLOAT, 
	ask_price_2 FLOAT, 
	ask_price_3 FLOAT, 
	ask_price_4 FLOAT, 
	ask_price_5 FLOAT, 
	ask_volume FLOAT, 
	ask_volume_2 FLOAT, 
	ask_volume_3 FLOAT, 
	ask_volume_4 FLOAT, 
	ask_volume_5 FLOAT, 
	volume FLOAT, 
	turnover FLOAT, 
	timestamp BIGINT, 
	update_time TEXT, 
	timestamp_datetime TEXT, 
	book_valid BOOLEAN, 
	extra JSON, 
	PRIMARY KEY (record_id, path), 
	CHECK (node_kind IN ('object', 'value')), 
	CHECK (ordinal >= 0), 
	FOREIGN KEY(record_id) REFERENCES executerecord (id) ON DELETE CASCADE
);

CREATE TABLE execution_termination_cancel_failure (
	record_id INTEGER NOT NULL, 
	ordinal INTEGER NOT NULL, 
	order_id JSON, 
	audit_evidence JSON NOT NULL, 
	PRIMARY KEY (record_id, ordinal), 
	FOREIGN KEY(record_id) REFERENCES executerecord (id) ON DELETE CASCADE
);

CREATE TABLE notification_event (
	sequence INTEGER NOT NULL, 
	id TEXT NOT NULL, 
	account_id INTEGER NOT NULL, 
	created_at VARCHAR NOT NULL, 
	event_type VARCHAR NOT NULL, 
	context JSON NOT NULL, 
	status VARCHAR NOT NULL, 
	finished_at VARCHAR, 
	error VARCHAR, 
	PRIMARY KEY (sequence), 
	UNIQUE (id), 
	FOREIGN KEY(account_id) REFERENCES account (id) ON DELETE CASCADE
);

CREATE TABLE order_submission (
	submission_uid TEXT NOT NULL, 
	account_id INTEGER NOT NULL, 
	occurred_at_ms INTEGER NOT NULL, 
	revision INTEGER NOT NULL, 
	data JSON NOT NULL, 
	PRIMARY KEY (submission_uid), 
	FOREIGN KEY(account_id) REFERENCES account (id) ON DELETE CASCADE
);

CREATE TABLE order_submission_monitor (
	account_id INTEGER NOT NULL, 
	data JSON NOT NULL, 
	PRIMARY KEY (account_id), 
	FOREIGN KEY(account_id) REFERENCES account (id) ON DELETE CASCADE
);

CREATE TABLE order_submission_warning (
	event_uid TEXT NOT NULL, 
	account_id INTEGER NOT NULL, 
	occurred_at_ms INTEGER NOT NULL, 
	data JSON NOT NULL, 
	PRIMARY KEY (event_uid), 
	FOREIGN KEY(account_id) REFERENCES account (id) ON DELETE CASCADE
);

CREATE TABLE performance_cost_attempt (
	snapshot_id TEXT NOT NULL, 
	record_id INTEGER NOT NULL, 
	idx INTEGER NOT NULL, 
	symbol TEXT, 
	planned FLOAT, 
	filled FLOAT, 
	reason TEXT, 
	PRIMARY KEY (snapshot_id, record_id, idx), 
	FOREIGN KEY(snapshot_id) REFERENCES performance_snapshot (id) ON DELETE CASCADE
);

CREATE TABLE performance_cost_execution (
	snapshot_id TEXT NOT NULL, 
	record_id INTEGER NOT NULL, 
	time FLOAT NOT NULL, 
	success BOOLEAN NOT NULL, 
	noop BOOLEAN NOT NULL, 
	payload JSON NOT NULL, "key" TEXT, execution_id TEXT, created_at TEXT, is_success INTEGER, duration_sec FLOAT, trade_count INTEGER, symbol_count INTEGER, position_count INTEGER, positions_present BOOLEAN DEFAULT '0' NOT NULL, reason TEXT, raw_present INTEGER DEFAULT '0' NOT NULL, has_symbol_results BOOLEAN DEFAULT '0' NOT NULL, raw_status TEXT, raw_task_status TEXT, raw_error TEXT, raw_outcome TEXT, raw_outcome_reason TEXT, raw_reason_code TEXT, raw_msg TEXT, raw_execution_kind TEXT, "value" REAL, "cost" REAL, "loss_bp" REAL, "coverage" REAL, "covered" INTEGER, "count" INTEGER, "fee_covered" INTEGER, "amount_complete" BOOLEAN, "covered_value" REAL, "estimated" INTEGER, "has_transactions" BOOLEAN, 
	PRIMARY KEY (snapshot_id, record_id), 
	FOREIGN KEY(snapshot_id) REFERENCES performance_snapshot (id) ON DELETE CASCADE
);

CREATE TABLE performance_cost_execution_symbol (
	snapshot_id TEXT NOT NULL, 
	record_id INTEGER NOT NULL, 
	symbol TEXT NOT NULL, 
	present INTEGER NOT NULL, 
	status TEXT, 
	error TEXT, 
	outcome TEXT, 
	outcome_reason TEXT, 
	reason_code TEXT, "memory_message" TEXT, "memory_reason_code" TEXT, 
	PRIMARY KEY (snapshot_id, record_id, symbol), 
	FOREIGN KEY(snapshot_id) REFERENCES performance_snapshot (id) ON DELETE CASCADE
);

CREATE TABLE performance_cost_position (
	snapshot_id TEXT NOT NULL, 
	record_id INTEGER NOT NULL, 
	idx INTEGER NOT NULL, 
	symbol TEXT, 
	quantity FLOAT, 
	direction TEXT, 
	PRIMARY KEY (snapshot_id, record_id, idx), 
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
	payload JSON NOT NULL, side TEXT DEFAULT 'none' NOT NULL, quantity FLOAT, price FLOAT, reference FLOAT, reference_source TEXT, 
	PRIMARY KEY (snapshot_id, id), 
	FOREIGN KEY(snapshot_id) REFERENCES performance_snapshot (id) ON DELETE CASCADE
);

CREATE TABLE performance_cost_transaction (
	snapshot_id TEXT NOT NULL, 
	record_id INTEGER NOT NULL, 
	idx INTEGER NOT NULL, 
	symbol TEXT, 
	side TEXT, 
	quantity FLOAT, 
	price FLOAT, 
	reference FLOAT, 
	reference_source TEXT, 
	time FLOAT, 
	end_time FLOAT, 
	time_estimated BOOLEAN NOT NULL, 
	"before" FLOAT, 
	target FLOAT, 
	value FLOAT, 
	cost FLOAT, 
	loss_bp FLOAT, 
	coverage FLOAT, 
	covered INTEGER, 
	count INTEGER, 
	amount_complete BOOLEAN NOT NULL, 
	fee_covered INTEGER, 
	PRIMARY KEY (snapshot_id, record_id, idx), 
	FOREIGN KEY(snapshot_id) REFERENCES performance_snapshot (id) ON DELETE CASCADE
);

CREATE TABLE performance_cost_transaction_fee (
	snapshot_id TEXT NOT NULL, 
	record_id INTEGER NOT NULL, 
	transaction_idx INTEGER NOT NULL, 
	currency TEXT NOT NULL, 
	value FLOAT NOT NULL, 
	PRIMARY KEY (snapshot_id, record_id, transaction_idx, currency), 
	FOREIGN KEY(snapshot_id) REFERENCES performance_snapshot (id) ON DELETE CASCADE
);

CREATE TABLE performance_execution_fee (
	snapshot_id TEXT NOT NULL, 
	record_id INTEGER NOT NULL, 
	currency TEXT NOT NULL, 
	value FLOAT NOT NULL, 
	count INTEGER NOT NULL, 
	PRIMARY KEY (snapshot_id, record_id, currency), 
	FOREIGN KEY(snapshot_id) REFERENCES performance_snapshot (id) ON DELETE CASCADE
);

CREATE TABLE performance_result_binding (
	snapshot_id TEXT NOT NULL, 
	range_key TEXT NOT NULL, 
	idx INTEGER NOT NULL, 
	time TEXT NOT NULL, 
	portfolio_id INTEGER, 
	PRIMARY KEY (snapshot_id, range_key, idx), 
	FOREIGN KEY(snapshot_id) REFERENCES performance_snapshot (id) ON DELETE CASCADE
);

CREATE TABLE performance_result_calendar_range (
	snapshot_id TEXT NOT NULL, 
	range_key TEXT NOT NULL, 
	kind TEXT NOT NULL, 
	idx INTEGER NOT NULL, 
	start TEXT NOT NULL, 
	"end" TEXT NOT NULL, 
	PRIMARY KEY (snapshot_id, range_key, kind, idx), 
	FOREIGN KEY(snapshot_id) REFERENCES performance_snapshot (id) ON DELETE CASCADE
);

CREATE TABLE performance_result_daily_cost (
	snapshot_id TEXT NOT NULL, 
	range_key TEXT NOT NULL, 
	day TEXT NOT NULL, 
	value FLOAT, 
	cost FLOAT, 
	loss_bp FLOAT, 
	coverage FLOAT, 
	covered_value FLOAT, 
	covered INTEGER, 
	count INTEGER, 
	fee_covered INTEGER, 
	estimated INTEGER, 
	amount_complete BOOLEAN NOT NULL, 
	present INTEGER NOT NULL, 
	PRIMARY KEY (snapshot_id, range_key, day), 
	FOREIGN KEY(snapshot_id) REFERENCES performance_snapshot (id) ON DELETE CASCADE
);

CREATE TABLE performance_result_daily_fee (
	snapshot_id TEXT NOT NULL, 
	range_key TEXT NOT NULL, 
	day TEXT NOT NULL, 
	currency TEXT NOT NULL, 
	value FLOAT NOT NULL, 
	PRIMARY KEY (snapshot_id, range_key, day, currency), 
	FOREIGN KEY(snapshot_id) REFERENCES performance_snapshot (id) ON DELETE CASCADE
);

CREATE TABLE performance_result_event (
	snapshot_id TEXT NOT NULL, 
	range_key TEXT NOT NULL, 
	idx INTEGER NOT NULL, 
	time TEXT NOT NULL, 
	tag TEXT NOT NULL, 
	text TEXT NOT NULL, 
	execution_id TEXT, 
	PRIMARY KEY (snapshot_id, range_key, idx), 
	FOREIGN KEY(snapshot_id) REFERENCES performance_snapshot (id) ON DELETE CASCADE
);

CREATE TABLE performance_result_fallback (
	snapshot_id TEXT NOT NULL, 
	range_key TEXT NOT NULL, 
	idx INTEGER NOT NULL, 
	start TEXT NOT NULL, 
	"end" TEXT NOT NULL, 
	count INTEGER NOT NULL, 
	PRIMARY KEY (snapshot_id, range_key, idx), 
	FOREIGN KEY(snapshot_id) REFERENCES performance_snapshot (id) ON DELETE CASCADE
);

CREATE TABLE performance_result_point (
	snapshot_id TEXT NOT NULL, 
	range_key TEXT NOT NULL, 
	idx INTEGER NOT NULL, 
	date TEXT NOT NULL, 
	observed_at TEXT NOT NULL, 
	record_id INTEGER, 
	execution_id TEXT, 
	account_return FLOAT, 
	account_equity FLOAT, 
	portfolio_return FLOAT, 
	account_daily_return FLOAT, 
	portfolio_daily_return FLOAT, 
	target_portfolio_return FLOAT, 
	target_portfolio_daily_return FLOAT, 
	difference FLOAT, 
	sizing_fallback BOOLEAN NOT NULL, 
	PRIMARY KEY (snapshot_id, range_key, idx), 
	FOREIGN KEY(snapshot_id) REFERENCES performance_snapshot (id) ON DELETE CASCADE
);

CREATE TABLE performance_result_range (
	snapshot_id TEXT NOT NULL, 
	range_key TEXT NOT NULL, 
	backtest_included BOOLEAN NOT NULL, 
	engine_version TEXT NOT NULL, 
	baseline TEXT, 
	"end" TEXT, 
	record_count INTEGER NOT NULL, 
	observation_count INTEGER NOT NULL, 
	used_record_count INTEGER NOT NULL, 
	target_used_record_count INTEGER NOT NULL, 
	invalid_asset_count INTEGER NOT NULL, 
	sizing_fallback_count INTEGER, 
	calendar_status TEXT NOT NULL, 
	calendar_id TEXT, 
	calendar_label TEXT, 
	event_count INTEGER NOT NULL, 
	PRIMARY KEY (snapshot_id, range_key), 
	FOREIGN KEY(snapshot_id) REFERENCES performance_snapshot (id) ON DELETE CASCADE
);

CREATE TABLE performance_result_skip (
	snapshot_id TEXT NOT NULL, 
	range_key TEXT NOT NULL, 
	kind TEXT NOT NULL, 
	count INTEGER NOT NULL, 
	missing_target INTEGER NOT NULL, 
	missing_ticks INTEGER NOT NULL, 
	first_time TEXT, 
	last_time TEXT, 
	PRIMARY KEY (snapshot_id, range_key, kind), 
	FOREIGN KEY(snapshot_id) REFERENCES performance_snapshot (id) ON DELETE CASCADE
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
	ranges JSON NOT NULL, cost_schema_version INTEGER DEFAULT '0' NOT NULL, result_schema_version INTEGER DEFAULT '0' NOT NULL, backtest_weight_type TEXT, backtest_fee_rate FLOAT, read_view_schema_version INTEGER DEFAULT '0' NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(account_id) REFERENCES account (id) ON DELETE CASCADE
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

CREATE TABLE portfolioaccount (
	account_id INTEGER NOT NULL, 
	portfolio_id INTEGER, 
	created_at TEXT NOT NULL, 
	id INTEGER NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(account_id) REFERENCES account (id) ON DELETE CASCADE, 
	FOREIGN KEY(portfolio_id) REFERENCES portfolio (id) ON DELETE CASCADE
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

CREATE TABLE supplement_group (
	id VARCHAR NOT NULL, 
	account_id INTEGER NOT NULL, 
	fingerprint VARCHAR NOT NULL, 
	base_scheduled_at VARCHAR NOT NULL, 
	expires_at VARCHAR NOT NULL, 
	configured_count INTEGER NOT NULL, 
	effective_count INTEGER NOT NULL, 
	status VARCHAR NOT NULL, 
	end_reason VARCHAR, 
	cancel_requested_at VARCHAR, 
	PRIMARY KEY (id), 
	UNIQUE (account_id, fingerprint, base_scheduled_at), 
	FOREIGN KEY(account_id) REFERENCES account (id) ON DELETE CASCADE
);

CREATE TABLE supplement_step (
	id VARCHAR NOT NULL, 
	group_id TEXT NOT NULL, 
	"index" INTEGER NOT NULL, 
	scheduled_at VARCHAR NOT NULL, 
	status VARCHAR NOT NULL, 
	execution_id VARCHAR, 
	PRIMARY KEY (id), 
	UNIQUE (group_id, "index"), 
	FOREIGN KEY(group_id) REFERENCES supplement_group (id) ON DELETE CASCADE
);

CREATE TABLE system_notification_state (
	id INTEGER NOT NULL, 
	finished_at VARCHAR, 
	mode VARCHAR, 
	event_type VARCHAR, 
	execution_id VARCHAR, 
	account_id INTEGER, 
	ok BOOLEAN, 
	error VARCHAR, 
	PRIMARY KEY (id)
);

CREATE TABLE target_weight_snapshot (
	id INTEGER NOT NULL, 
	portfolio_id INTEGER NOT NULL, 
	account_id INTEGER, 
	source TEXT NOT NULL, 
	execution_id TEXT, 
	calculated_at TEXT NOT NULL, raw_weights_kind TEXT DEFAULT 'null' NOT NULL, normalized_weights_kind TEXT DEFAULT 'null' NOT NULL, weights_metadata JSON DEFAULT '{}' NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(portfolio_id) REFERENCES portfolio (id) ON DELETE CASCADE, 
	FOREIGN KEY(account_id) REFERENCES account (id) ON DELETE SET NULL
);

CREATE TABLE target_weight_snapshot_weight (
	snapshot_id INTEGER NOT NULL, 
	role TEXT NOT NULL, 
	map_key TEXT NOT NULL, 
	value FLOAT, 
	audit_evidence JSON NOT NULL, 
	PRIMARY KEY (snapshot_id, role, map_key), 
	FOREIGN KEY(snapshot_id) REFERENCES target_weight_snapshot (id) ON DELETE CASCADE
)
 WITHOUT ROWID;

CREATE TABLE time_check_settings (
	id INTEGER NOT NULL, 
	version INTEGER NOT NULL, 
	data JSON NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE time_check_state (
	id INTEGER NOT NULL, 
	data JSON NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TRIGGER account_settings_delete AFTER DELETE ON account
        BEGIN
        DELETE FROM account_settings WHERE account_id = OLD.id;
        DELETE FROM account_notification_state WHERE account_id = OLD.id;
        END;

CREATE TRIGGER analysis_account_channel
        AFTER UPDATE OF trade_channel ON account WHEN OLD.trade_channel IS NOT NEW.trade_channel BEGIN
        INSERT INTO account_analysis (account_id, source_version) VALUES (NEW.id, 1)
        ON CONFLICT(account_id) DO UPDATE SET source_version = source_version + 1,
        requested = CASE WHEN failures > 0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
        THEN 1 ELSE requested END,
        failures = 0, retry_at = 0, error = NULL;
        END;

CREATE TRIGGER analysis_account_delete AFTER DELETE ON account
        BEGIN
        DELETE FROM performance_cost_trade WHERE snapshot_id IN (SELECT id FROM performance_snapshot WHERE account_id = OLD.id);
        DELETE FROM performance_cost_execution WHERE snapshot_id IN (SELECT id FROM performance_snapshot WHERE account_id = OLD.id);
        DELETE FROM performance_snapshot WHERE account_id = OLD.id;
        DELETE FROM account_analysis WHERE account_id = OLD.id;
        END;

CREATE TRIGGER analysis_account_settings
        AFTER UPDATE OF backtest ON account_settings
        WHEN json_extract(OLD.backtest, '$.backtest_weight_type') IS NOT json_extract(NEW.backtest, '$.backtest_weight_type')
          OR json_extract(OLD.backtest, '$.backtest_fee_rate') IS NOT json_extract(NEW.backtest, '$.backtest_fee_rate')
        BEGIN
        INSERT INTO account_analysis (account_id, source_version) VALUES (NEW.account_id, 1)
        ON CONFLICT(account_id) DO UPDATE SET source_version = source_version + 1,
        requested = CASE WHEN failures > 0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
        THEN 1 ELSE requested END,
        failures = 0, retry_at = 0, error = NULL;
        END;

CREATE TRIGGER analysis_executerecord_delete AFTER DELETE ON executerecord BEGIN INSERT INTO account_analysis (account_id, source_version)
                SELECT OLD.account_id, 1 WHERE OLD.account_id IS NOT NULL
                AND EXISTS (SELECT 1 FROM account WHERE id = OLD.account_id)
                
                ON CONFLICT(account_id) DO UPDATE SET source_version = source_version + 1,
                requested = CASE WHEN failures > 0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
                failures = 0, retry_at = 0, error = NULL; END;

CREATE TRIGGER analysis_executerecord_insert AFTER INSERT ON executerecord BEGIN INSERT INTO account_analysis (account_id, source_version)
                SELECT NEW.account_id, 1 WHERE NEW.account_id IS NOT NULL
                AND EXISTS (SELECT 1 FROM account WHERE id = NEW.account_id)
                
                ON CONFLICT(account_id) DO UPDATE SET source_version = source_version + 1,
                requested = CASE WHEN failures > 0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
                failures = 0, retry_at = 0, error = NULL; END;

CREATE TRIGGER analysis_executerecord_update AFTER UPDATE ON executerecord BEGIN INSERT INTO account_analysis (account_id, source_version)
                SELECT OLD.account_id, 1 WHERE OLD.account_id IS NOT NULL
                AND EXISTS (SELECT 1 FROM account WHERE id = OLD.account_id)
                
                ON CONFLICT(account_id) DO UPDATE SET source_version = source_version + 1,
                requested = CASE WHEN failures > 0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
                failures = 0, retry_at = 0, error = NULL; INSERT INTO account_analysis (account_id, source_version)
                SELECT NEW.account_id, 1 WHERE NEW.account_id IS NOT NULL
                AND EXISTS (SELECT 1 FROM account WHERE id = NEW.account_id)
                AND NEW.account_id IS NOT OLD.account_id
                ON CONFLICT(account_id) DO UPDATE SET source_version = source_version + 1,
                requested = CASE WHEN failures > 0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
                failures = 0, retry_at = 0, error = NULL; END;

CREATE TRIGGER analysis_facts_snapshot_delete AFTER DELETE ON performance_snapshot BEGIN DELETE FROM performance_cost_transaction_fee WHERE snapshot_id = OLD.id; DELETE FROM performance_cost_transaction WHERE snapshot_id = OLD.id; DELETE FROM performance_cost_attempt WHERE snapshot_id = OLD.id; DELETE FROM performance_cost_position WHERE snapshot_id = OLD.id; DELETE FROM performance_cost_execution_symbol WHERE snapshot_id = OLD.id;  DELETE FROM performance_cost_trade WHERE snapshot_id = OLD.id; DELETE FROM performance_cost_execution WHERE snapshot_id = OLD.id; END;

CREATE TRIGGER analysis_portfolioaccount_delete AFTER DELETE ON portfolioaccount BEGIN INSERT INTO account_analysis (account_id, source_version)
                SELECT OLD.account_id, 1 WHERE OLD.account_id IS NOT NULL
                AND EXISTS (SELECT 1 FROM account WHERE id = OLD.account_id)
                
                ON CONFLICT(account_id) DO UPDATE SET source_version = source_version + 1,
                requested = CASE WHEN failures > 0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
                failures = 0, retry_at = 0, error = NULL; END;

CREATE TRIGGER analysis_portfolioaccount_insert AFTER INSERT ON portfolioaccount BEGIN INSERT INTO account_analysis (account_id, source_version)
                SELECT NEW.account_id, 1 WHERE NEW.account_id IS NOT NULL
                AND EXISTS (SELECT 1 FROM account WHERE id = NEW.account_id)
                
                ON CONFLICT(account_id) DO UPDATE SET source_version = source_version + 1,
                requested = CASE WHEN failures > 0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
                failures = 0, retry_at = 0, error = NULL; END;

CREATE TRIGGER analysis_portfolioaccount_update AFTER UPDATE ON portfolioaccount BEGIN INSERT INTO account_analysis (account_id, source_version)
                SELECT OLD.account_id, 1 WHERE OLD.account_id IS NOT NULL
                AND EXISTS (SELECT 1 FROM account WHERE id = OLD.account_id)
                
                ON CONFLICT(account_id) DO UPDATE SET source_version = source_version + 1,
                requested = CASE WHEN failures > 0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
                failures = 0, retry_at = 0, error = NULL; INSERT INTO account_analysis (account_id, source_version)
                SELECT NEW.account_id, 1 WHERE NEW.account_id IS NOT NULL
                AND EXISTS (SELECT 1 FROM account WHERE id = NEW.account_id)
                AND NEW.account_id IS NOT OLD.account_id
                ON CONFLICT(account_id) DO UPDATE SET source_version = source_version + 1,
                requested = CASE WHEN failures > 0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
                failures = 0, retry_at = 0, error = NULL; END;

CREATE TRIGGER analysis_read_views_snapshot_delete AFTER DELETE ON performance_snapshot BEGIN DELETE FROM performance_execution_fee WHERE snapshot_id=OLD.id;  END;

CREATE TRIGGER analysis_schedule_skip_delete AFTER DELETE ON schedule_skip BEGIN INSERT INTO account_analysis (account_id, source_version)
                SELECT OLD.account_id, 1 WHERE OLD.account_id IS NOT NULL
                AND EXISTS (SELECT 1 FROM account WHERE id = OLD.account_id)
                
                ON CONFLICT(account_id) DO UPDATE SET source_version = source_version + 1,
                requested = CASE WHEN failures > 0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
                failures = 0, retry_at = 0, error = NULL; END;

CREATE TRIGGER analysis_schedule_skip_insert AFTER INSERT ON schedule_skip BEGIN INSERT INTO account_analysis (account_id, source_version)
                SELECT NEW.account_id, 1 WHERE NEW.account_id IS NOT NULL
                AND EXISTS (SELECT 1 FROM account WHERE id = NEW.account_id)
                
                ON CONFLICT(account_id) DO UPDATE SET source_version = source_version + 1,
                requested = CASE WHEN failures > 0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
                failures = 0, retry_at = 0, error = NULL; END;

CREATE TRIGGER analysis_schedule_skip_update AFTER UPDATE ON schedule_skip BEGIN INSERT INTO account_analysis (account_id, source_version)
                SELECT OLD.account_id, 1 WHERE OLD.account_id IS NOT NULL
                AND EXISTS (SELECT 1 FROM account WHERE id = OLD.account_id)
                
                ON CONFLICT(account_id) DO UPDATE SET source_version = source_version + 1,
                requested = CASE WHEN failures > 0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
                failures = 0, retry_at = 0, error = NULL; INSERT INTO account_analysis (account_id, source_version)
                SELECT NEW.account_id, 1 WHERE NEW.account_id IS NOT NULL
                AND EXISTS (SELECT 1 FROM account WHERE id = NEW.account_id)
                AND NEW.account_id IS NOT OLD.account_id
                ON CONFLICT(account_id) DO UPDATE SET source_version = source_version + 1,
                requested = CASE WHEN failures > 0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
                failures = 0, retry_at = 0, error = NULL; END;

CREATE TRIGGER analysis_structured_snapshot_delete AFTER DELETE ON performance_snapshot BEGIN DELETE FROM performance_result_daily_fee WHERE snapshot_id = OLD.id; DELETE FROM performance_result_daily_cost WHERE snapshot_id = OLD.id; DELETE FROM performance_result_event WHERE snapshot_id = OLD.id; DELETE FROM performance_result_calendar_range WHERE snapshot_id = OLD.id; DELETE FROM performance_result_binding WHERE snapshot_id = OLD.id; DELETE FROM performance_result_fallback WHERE snapshot_id = OLD.id; DELETE FROM performance_result_skip WHERE snapshot_id = OLD.id; DELETE FROM performance_result_point WHERE snapshot_id = OLD.id; DELETE FROM performance_result_range WHERE snapshot_id = OLD.id; END;

CREATE TRIGGER analysis_target_weight_snapshot_delete AFTER DELETE ON target_weight_snapshot BEGIN INSERT INTO account_analysis (account_id, source_version)
                SELECT OLD.account_id, 1 WHERE OLD.account_id IS NOT NULL
                AND EXISTS (SELECT 1 FROM account WHERE id = OLD.account_id)
                
                ON CONFLICT(account_id) DO UPDATE SET source_version = source_version + 1,
                requested = CASE WHEN failures > 0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
                failures = 0, retry_at = 0, error = NULL; END;

CREATE TRIGGER analysis_target_weight_snapshot_insert AFTER INSERT ON target_weight_snapshot BEGIN INSERT INTO account_analysis (account_id, source_version)
                SELECT NEW.account_id, 1 WHERE NEW.account_id IS NOT NULL
                AND EXISTS (SELECT 1 FROM account WHERE id = NEW.account_id)
                
                ON CONFLICT(account_id) DO UPDATE SET source_version = source_version + 1,
                requested = CASE WHEN failures > 0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
                failures = 0, retry_at = 0, error = NULL; END;

CREATE TRIGGER analysis_target_weight_snapshot_update AFTER UPDATE ON target_weight_snapshot BEGIN INSERT INTO account_analysis (account_id, source_version)
                SELECT OLD.account_id, 1 WHERE OLD.account_id IS NOT NULL
                AND EXISTS (SELECT 1 FROM account WHERE id = OLD.account_id)
                
                ON CONFLICT(account_id) DO UPDATE SET source_version = source_version + 1,
                requested = CASE WHEN failures > 0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
                failures = 0, retry_at = 0, error = NULL; INSERT INTO account_analysis (account_id, source_version)
                SELECT NEW.account_id, 1 WHERE NEW.account_id IS NOT NULL
                AND EXISTS (SELECT 1 FROM account WHERE id = NEW.account_id)
                AND NEW.account_id IS NOT OLD.account_id
                ON CONFLICT(account_id) DO UPDATE SET source_version = source_version + 1,
                requested = CASE WHEN failures > 0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
                failures = 0, retry_at = 0, error = NULL; END;

CREATE TRIGGER "business_facts_0037_account_asset_snapshot_cleanup_business_asset_snapshot" AFTER DELETE ON "account_asset_snapshot" BEGIN DELETE FROM "business_asset_snapshot" WHERE "account_snapshot_id" IS OLD."id"; END;

CREATE TRIGGER "business_facts_0037_account_asset_snapshot_delete" AFTER DELETE ON "account_asset_snapshot" BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT id FROM account WHERE id IS OLD."account_id" UNION SELECT account_id FROM executerecord WHERE id IS (SELECT parent_0."record_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS OLD."asset_fact_id") UNION SELECT account_id FROM execution_input WHERE id IS (SELECT parent_0."input_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS OLD."asset_fact_id") UNION SELECT account_id FROM account_asset_snapshot WHERE id IS OLD."id" UNION SELECT account_id FROM account_asset_snapshot WHERE id IS (SELECT parent_0."account_snapshot_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS OLD."asset_fact_id") UNION SELECT account_id FROM executerecord WHERE id IN (SELECT record_id FROM business_asset_binding WHERE asset_id IS (SELECT parent_0."asset_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS OLD."asset_fact_id"))) AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_account_asset_snapshot_insert" AFTER INSERT ON "account_asset_snapshot" BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT id FROM account WHERE id IS NEW."account_id" UNION SELECT account_id FROM executerecord WHERE id IS (SELECT parent_0."record_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS NEW."asset_fact_id") UNION SELECT account_id FROM execution_input WHERE id IS (SELECT parent_0."input_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS NEW."asset_fact_id") UNION SELECT account_id FROM account_asset_snapshot WHERE id IS NEW."id" UNION SELECT account_id FROM account_asset_snapshot WHERE id IS (SELECT parent_0."account_snapshot_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS NEW."asset_fact_id") UNION SELECT account_id FROM executerecord WHERE id IN (SELECT record_id FROM business_asset_binding WHERE asset_id IS (SELECT parent_0."asset_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS NEW."asset_fact_id"))) AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_account_asset_snapshot_owner_asset_fact_id_insert" BEFORE INSERT ON "account_asset_snapshot" BEGIN SELECT RAISE(ABORT,'Business fact owner missing') WHERE NEW."asset_fact_id" IS NOT NULL AND NOT EXISTS(SELECT 1 FROM "business_asset_snapshot" WHERE "asset_id" IS NEW."asset_fact_id"); END;

CREATE TRIGGER "business_facts_0037_account_asset_snapshot_owner_asset_fact_id_update" BEFORE UPDATE ON "account_asset_snapshot" BEGIN SELECT RAISE(ABORT,'Business fact owner missing') WHERE NEW."asset_fact_id" IS NOT NULL AND NOT EXISTS(SELECT 1 FROM "business_asset_snapshot" WHERE "asset_id" IS NEW."asset_fact_id"); END;

CREATE TRIGGER "business_facts_0037_account_asset_snapshot_update" AFTER UPDATE ON "account_asset_snapshot" BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT id FROM account WHERE id IS OLD."account_id" UNION SELECT account_id FROM executerecord WHERE id IS (SELECT parent_0."record_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS OLD."asset_fact_id") UNION SELECT account_id FROM execution_input WHERE id IS (SELECT parent_0."input_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS OLD."asset_fact_id") UNION SELECT account_id FROM account_asset_snapshot WHERE id IS OLD."id" UNION SELECT account_id FROM account_asset_snapshot WHERE id IS (SELECT parent_0."account_snapshot_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS OLD."asset_fact_id") UNION SELECT account_id FROM executerecord WHERE id IN (SELECT record_id FROM business_asset_binding WHERE asset_id IS (SELECT parent_0."asset_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS OLD."asset_fact_id")) UNION SELECT id FROM account WHERE id IS NEW."account_id" UNION SELECT account_id FROM executerecord WHERE id IS (SELECT parent_0."record_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS NEW."asset_fact_id") UNION SELECT account_id FROM execution_input WHERE id IS (SELECT parent_0."input_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS NEW."asset_fact_id") UNION SELECT account_id FROM account_asset_snapshot WHERE id IS NEW."id" UNION SELECT account_id FROM account_asset_snapshot WHERE id IS (SELECT parent_0."account_snapshot_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS NEW."asset_fact_id") UNION SELECT account_id FROM executerecord WHERE id IN (SELECT record_id FROM business_asset_binding WHERE asset_id IS (SELECT parent_0."asset_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS NEW."asset_fact_id"))) AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER business_facts_0037_account_inputs_cleanup BEFORE DELETE ON account BEGIN DELETE FROM execution_artifact WHERE execution_id IN (SELECT execution_id FROM executerecord WHERE account_id=OLD.id UNION SELECT COALESCE(owner_execution_id,execution_id) FROM execution_input WHERE account_id=OLD.id UNION SELECT execution_id FROM execution_event WHERE account_id=OLD.id); DELETE FROM execution_event WHERE account_id=OLD.id; DELETE FROM account_asset_snapshot WHERE account_id=OLD.id; DELETE FROM executerecord WHERE account_id=OLD.id; DELETE FROM execution_input WHERE account_id=OLD.id; UPDATE target_weight_snapshot SET account_id=NULL WHERE account_id=OLD.id; END;

CREATE TRIGGER business_facts_0037_asset_binding_owner_insert BEFORE INSERT ON business_asset_binding BEGIN SELECT RAISE(ABORT,'Asset binding owner mismatch') WHERE (SELECT COALESCE(r.account_id,i.account_id,s.account_id)
        FROM business_asset_snapshot AS a
        LEFT JOIN executerecord AS r ON r.id=a.record_id
        LEFT JOIN execution_input AS i ON i.id=a.input_id
        LEFT JOIN account_asset_snapshot AS s ON s.id=a.account_snapshot_id
        WHERE a.asset_id=NEW.asset_id) IS NOT (SELECT account_id FROM executerecord WHERE id=NEW.record_id); END;

CREATE TRIGGER business_facts_0037_asset_binding_owner_update BEFORE UPDATE ON business_asset_binding BEGIN SELECT RAISE(ABORT,'Asset binding owner mismatch') WHERE (SELECT COALESCE(r.account_id,i.account_id,s.account_id)
        FROM business_asset_snapshot AS a
        LEFT JOIN executerecord AS r ON r.id=a.record_id
        LEFT JOIN execution_input AS i ON i.id=a.input_id
        LEFT JOIN account_asset_snapshot AS s ON s.id=a.account_snapshot_id
        WHERE a.asset_id=NEW.asset_id) IS NOT (SELECT account_id FROM executerecord WHERE id=NEW.record_id); END;

CREATE TRIGGER business_facts_0037_asset_owner_update BEFORE UPDATE ON business_asset_snapshot BEGIN SELECT RAISE(ABORT,'Asset identity is immutable') WHERE NEW.asset_id IS NOT OLD.asset_id; SELECT RAISE(ABORT,'Asset owner mismatch') WHERE EXISTS(SELECT 1 FROM business_asset_binding AS b JOIN executerecord AS r ON r.id=b.record_id WHERE b.asset_id=OLD.asset_id AND r.account_id IS NOT (COALESCE((SELECT account_id FROM executerecord WHERE id=NEW.record_id),
        (SELECT account_id FROM execution_input WHERE id=NEW.input_id),
        (SELECT account_id FROM account_asset_snapshot WHERE id=NEW.account_snapshot_id)))) OR EXISTS(SELECT 1 FROM account_asset_snapshot AS s WHERE s.asset_fact_id=OLD.asset_id AND s.account_id IS NOT (COALESCE((SELECT account_id FROM executerecord WHERE id=NEW.record_id),
        (SELECT account_id FROM execution_input WHERE id=NEW.input_id),
        (SELECT account_id FROM account_asset_snapshot WHERE id=NEW.account_snapshot_id)))); END;

CREATE TRIGGER "business_facts_0037_business_asset_binding_delete" AFTER DELETE ON "business_asset_binding" BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT account_id FROM executerecord WHERE id IS OLD."record_id" UNION SELECT account_id FROM executerecord WHERE id IS (SELECT parent_0."record_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS OLD."asset_id") UNION SELECT account_id FROM execution_input WHERE id IS (SELECT parent_0."input_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS OLD."asset_id") UNION SELECT account_id FROM account_asset_snapshot WHERE id IS (SELECT parent_0."account_snapshot_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS OLD."asset_id") UNION SELECT account_id FROM executerecord WHERE id IN (SELECT record_id FROM business_asset_binding WHERE asset_id IS OLD."asset_id") UNION SELECT account_id FROM executerecord WHERE id IN (SELECT record_id FROM business_asset_binding WHERE asset_id IS (SELECT parent_0."asset_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS OLD."asset_id"))) AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_business_asset_binding_insert" AFTER INSERT ON "business_asset_binding" WHEN EXISTS(SELECT 1 FROM executerecord WHERE facts_complete=1 AND id IN (SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL UNION SELECT (SELECT parent_0."record_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS NEW."asset_id") WHERE (SELECT parent_0."record_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS NEW."asset_id") IS NOT NULL UNION SELECT id FROM executerecord WHERE (SELECT parent_0."input_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS NEW."asset_id") IS NOT NULL AND (input_id IS (SELECT parent_0."input_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS NEW."asset_id") OR result_input_id IS (SELECT parent_0."input_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS NEW."asset_id")) UNION SELECT e.id FROM executerecord AS e JOIN account_asset_snapshot AS s ON e.execution_id=s.execution_id AND e.account_id=s.account_id WHERE s.id IS (SELECT parent_0."account_snapshot_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS NEW."asset_id") AND s.execution_id IS NOT NULL UNION SELECT record_id FROM business_asset_binding WHERE asset_id IS NEW."asset_id" UNION SELECT record_id FROM business_asset_binding WHERE asset_id IS (SELECT parent_0."asset_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS NEW."asset_id"))) BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT account_id FROM executerecord WHERE id IS NEW."record_id" UNION SELECT account_id FROM executerecord WHERE id IS (SELECT parent_0."record_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS NEW."asset_id") UNION SELECT account_id FROM execution_input WHERE id IS (SELECT parent_0."input_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS NEW."asset_id") UNION SELECT account_id FROM account_asset_snapshot WHERE id IS (SELECT parent_0."account_snapshot_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS NEW."asset_id") UNION SELECT account_id FROM executerecord WHERE id IN (SELECT record_id FROM business_asset_binding WHERE asset_id IS NEW."asset_id") UNION SELECT account_id FROM executerecord WHERE id IN (SELECT record_id FROM business_asset_binding WHERE asset_id IS (SELECT parent_0."asset_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS NEW."asset_id"))) AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_business_asset_binding_owner_asset_id_insert" BEFORE INSERT ON "business_asset_binding" BEGIN SELECT RAISE(ABORT,'Business fact owner missing') WHERE NEW."asset_id" IS NOT NULL AND NOT EXISTS(SELECT 1 FROM "business_asset_snapshot" WHERE "asset_id" IS NEW."asset_id"); END;

CREATE TRIGGER "business_facts_0037_business_asset_binding_owner_asset_id_update" BEFORE UPDATE ON "business_asset_binding" BEGIN SELECT RAISE(ABORT,'Business fact owner missing') WHERE NEW."asset_id" IS NOT NULL AND NOT EXISTS(SELECT 1 FROM "business_asset_snapshot" WHERE "asset_id" IS NEW."asset_id"); END;

CREATE TRIGGER "business_facts_0037_business_asset_binding_owner_record_id_insert" BEFORE INSERT ON "business_asset_binding" BEGIN SELECT RAISE(ABORT,'Business fact owner missing') WHERE NEW."record_id" IS NOT NULL AND NOT EXISTS(SELECT 1 FROM "executerecord" WHERE "id" IS NEW."record_id"); END;

CREATE TRIGGER "business_facts_0037_business_asset_binding_owner_record_id_update" BEFORE UPDATE ON "business_asset_binding" BEGIN SELECT RAISE(ABORT,'Business fact owner missing') WHERE NEW."record_id" IS NOT NULL AND NOT EXISTS(SELECT 1 FROM "executerecord" WHERE "id" IS NEW."record_id"); END;

CREATE TRIGGER "business_facts_0037_business_asset_binding_summary_delete" BEFORE DELETE ON "business_asset_binding" BEGIN SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') WHERE EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN (SELECT OLD."record_id" WHERE OLD."record_id" IS NOT NULL UNION SELECT (SELECT parent_0."record_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS OLD."asset_id") WHERE (SELECT parent_0."record_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS OLD."asset_id") IS NOT NULL UNION SELECT record_id FROM business_asset_binding WHERE asset_id IS OLD."asset_id" UNION SELECT record_id FROM business_asset_binding WHERE asset_id IS (SELECT parent_0."asset_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS OLD."asset_id")))
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN (SELECT OLD."record_id" WHERE OLD."record_id" IS NOT NULL UNION SELECT (SELECT parent_0."record_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS OLD."asset_id") WHERE (SELECT parent_0."record_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS OLD."asset_id") IS NOT NULL UNION SELECT record_id FROM business_asset_binding WHERE asset_id IS OLD."asset_id" UNION SELECT record_id FROM business_asset_binding WHERE asset_id IS (SELECT parent_0."asset_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS OLD."asset_id"))
        AND (1)); END;

CREATE TRIGGER "business_facts_0037_business_asset_binding_summary_insert" BEFORE INSERT ON "business_asset_binding" BEGIN SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') WHERE EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN (SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL UNION SELECT (SELECT parent_0."record_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS NEW."asset_id") WHERE (SELECT parent_0."record_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS NEW."asset_id") IS NOT NULL UNION SELECT record_id FROM business_asset_binding WHERE asset_id IS NEW."asset_id" UNION SELECT record_id FROM business_asset_binding WHERE asset_id IS (SELECT parent_0."asset_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS NEW."asset_id")))
        AND CASE WHEN typeof(content)='text' AND content NOT GLOB '*business_facts_ref*' AND content NOT GLOB '*\u00*' THEN 0 ELSE (CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN (SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL UNION SELECT (SELECT parent_0."record_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS NEW."asset_id") WHERE (SELECT parent_0."record_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS NEW."asset_id") IS NOT NULL UNION SELECT record_id FROM business_asset_binding WHERE asset_id IS NEW."asset_id" UNION SELECT record_id FROM business_asset_binding WHERE asset_id IS (SELECT parent_0."asset_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS NEW."asset_id"))
        AND (1)) END); END;

CREATE TRIGGER "business_facts_0037_business_asset_binding_summary_update" BEFORE UPDATE ON "business_asset_binding" BEGIN SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') WHERE EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN (SELECT OLD."record_id" WHERE OLD."record_id" IS NOT NULL UNION SELECT (SELECT parent_0."record_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS OLD."asset_id") WHERE (SELECT parent_0."record_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS OLD."asset_id") IS NOT NULL UNION SELECT record_id FROM business_asset_binding WHERE asset_id IS OLD."asset_id" UNION SELECT record_id FROM business_asset_binding WHERE asset_id IS (SELECT parent_0."asset_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS OLD."asset_id") UNION SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL UNION SELECT (SELECT parent_0."record_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS NEW."asset_id") WHERE (SELECT parent_0."record_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS NEW."asset_id") IS NOT NULL UNION SELECT record_id FROM business_asset_binding WHERE asset_id IS NEW."asset_id" UNION SELECT record_id FROM business_asset_binding WHERE asset_id IS (SELECT parent_0."asset_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS NEW."asset_id")))
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN (SELECT OLD."record_id" WHERE OLD."record_id" IS NOT NULL UNION SELECT (SELECT parent_0."record_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS OLD."asset_id") WHERE (SELECT parent_0."record_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS OLD."asset_id") IS NOT NULL UNION SELECT record_id FROM business_asset_binding WHERE asset_id IS OLD."asset_id" UNION SELECT record_id FROM business_asset_binding WHERE asset_id IS (SELECT parent_0."asset_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS OLD."asset_id") UNION SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL UNION SELECT (SELECT parent_0."record_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS NEW."asset_id") WHERE (SELECT parent_0."record_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS NEW."asset_id") IS NOT NULL UNION SELECT record_id FROM business_asset_binding WHERE asset_id IS NEW."asset_id" UNION SELECT record_id FROM business_asset_binding WHERE asset_id IS (SELECT parent_0."asset_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS NEW."asset_id"))
        AND (((1) OR (1)) AND (1))); END;

CREATE TRIGGER "business_facts_0037_business_asset_binding_update" AFTER UPDATE ON "business_asset_binding" BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT account_id FROM executerecord WHERE id IS OLD."record_id" UNION SELECT account_id FROM executerecord WHERE id IS (SELECT parent_0."record_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS OLD."asset_id") UNION SELECT account_id FROM execution_input WHERE id IS (SELECT parent_0."input_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS OLD."asset_id") UNION SELECT account_id FROM account_asset_snapshot WHERE id IS (SELECT parent_0."account_snapshot_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS OLD."asset_id") UNION SELECT account_id FROM executerecord WHERE id IN (SELECT record_id FROM business_asset_binding WHERE asset_id IS OLD."asset_id") UNION SELECT account_id FROM executerecord WHERE id IN (SELECT record_id FROM business_asset_binding WHERE asset_id IS (SELECT parent_0."asset_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS OLD."asset_id")) UNION SELECT account_id FROM executerecord WHERE id IS NEW."record_id" UNION SELECT account_id FROM executerecord WHERE id IS (SELECT parent_0."record_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS NEW."asset_id") UNION SELECT account_id FROM execution_input WHERE id IS (SELECT parent_0."input_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS NEW."asset_id") UNION SELECT account_id FROM account_asset_snapshot WHERE id IS (SELECT parent_0."account_snapshot_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS NEW."asset_id") UNION SELECT account_id FROM executerecord WHERE id IN (SELECT record_id FROM business_asset_binding WHERE asset_id IS NEW."asset_id") UNION SELECT account_id FROM executerecord WHERE id IN (SELECT record_id FROM business_asset_binding WHERE asset_id IS (SELECT parent_0."asset_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS NEW."asset_id"))) AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_business_asset_position_delete" AFTER DELETE ON "business_asset_position" BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT account_id FROM executerecord WHERE id IS (SELECT parent_0."record_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS OLD."asset_id") UNION SELECT account_id FROM execution_input WHERE id IS (SELECT parent_0."input_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS OLD."asset_id") UNION SELECT account_id FROM account_asset_snapshot WHERE id IS (SELECT parent_0."account_snapshot_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS OLD."asset_id") UNION SELECT account_id FROM executerecord WHERE id IN (SELECT record_id FROM business_asset_binding WHERE asset_id IS OLD."asset_id") UNION SELECT account_id FROM executerecord WHERE id IN (SELECT record_id FROM business_asset_binding WHERE asset_id IS (SELECT parent_0."asset_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS OLD."asset_id"))) AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_business_asset_position_insert" AFTER INSERT ON "business_asset_position" WHEN EXISTS(SELECT 1 FROM executerecord WHERE facts_complete=1 AND id IN (SELECT (SELECT parent_0."record_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS NEW."asset_id") WHERE (SELECT parent_0."record_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS NEW."asset_id") IS NOT NULL UNION SELECT id FROM executerecord WHERE (SELECT parent_0."input_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS NEW."asset_id") IS NOT NULL AND (input_id IS (SELECT parent_0."input_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS NEW."asset_id") OR result_input_id IS (SELECT parent_0."input_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS NEW."asset_id")) UNION SELECT e.id FROM executerecord AS e JOIN account_asset_snapshot AS s ON e.execution_id=s.execution_id AND e.account_id=s.account_id WHERE s.id IS (SELECT parent_0."account_snapshot_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS NEW."asset_id") AND s.execution_id IS NOT NULL UNION SELECT record_id FROM business_asset_binding WHERE asset_id IS NEW."asset_id" UNION SELECT record_id FROM business_asset_binding WHERE asset_id IS (SELECT parent_0."asset_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS NEW."asset_id"))) BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT account_id FROM executerecord WHERE id IS (SELECT parent_0."record_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS NEW."asset_id") UNION SELECT account_id FROM execution_input WHERE id IS (SELECT parent_0."input_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS NEW."asset_id") UNION SELECT account_id FROM account_asset_snapshot WHERE id IS (SELECT parent_0."account_snapshot_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS NEW."asset_id") UNION SELECT account_id FROM executerecord WHERE id IN (SELECT record_id FROM business_asset_binding WHERE asset_id IS NEW."asset_id") UNION SELECT account_id FROM executerecord WHERE id IN (SELECT record_id FROM business_asset_binding WHERE asset_id IS (SELECT parent_0."asset_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS NEW."asset_id"))) AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_business_asset_position_owner_asset_id_insert" BEFORE INSERT ON "business_asset_position" BEGIN SELECT RAISE(ABORT,'Business fact owner missing') WHERE NEW."asset_id" IS NOT NULL AND NOT EXISTS(SELECT 1 FROM "business_asset_snapshot" WHERE "asset_id" IS NEW."asset_id"); END;

CREATE TRIGGER "business_facts_0037_business_asset_position_owner_asset_id_update" BEFORE UPDATE ON "business_asset_position" BEGIN SELECT RAISE(ABORT,'Business fact owner missing') WHERE NEW."asset_id" IS NOT NULL AND NOT EXISTS(SELECT 1 FROM "business_asset_snapshot" WHERE "asset_id" IS NEW."asset_id"); END;

CREATE TRIGGER "business_facts_0037_business_asset_position_summary_delete" BEFORE DELETE ON "business_asset_position" BEGIN SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') WHERE EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN (SELECT (SELECT parent_0."record_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS OLD."asset_id") WHERE (SELECT parent_0."record_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS OLD."asset_id") IS NOT NULL UNION SELECT record_id FROM business_asset_binding WHERE asset_id IS OLD."asset_id" UNION SELECT record_id FROM business_asset_binding WHERE asset_id IS (SELECT parent_0."asset_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS OLD."asset_id")))
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN (SELECT (SELECT parent_0."record_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS OLD."asset_id") WHERE (SELECT parent_0."record_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS OLD."asset_id") IS NOT NULL UNION SELECT record_id FROM business_asset_binding WHERE asset_id IS OLD."asset_id" UNION SELECT record_id FROM business_asset_binding WHERE asset_id IS (SELECT parent_0."asset_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS OLD."asset_id"))
        AND (1)); END;

CREATE TRIGGER "business_facts_0037_business_asset_position_summary_insert" BEFORE INSERT ON "business_asset_position" BEGIN SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') WHERE EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN (SELECT (SELECT parent_0."record_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS NEW."asset_id") WHERE (SELECT parent_0."record_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS NEW."asset_id") IS NOT NULL UNION SELECT record_id FROM business_asset_binding WHERE asset_id IS NEW."asset_id" UNION SELECT record_id FROM business_asset_binding WHERE asset_id IS (SELECT parent_0."asset_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS NEW."asset_id")))
        AND CASE WHEN typeof(content)='text' AND content NOT GLOB '*business_facts_ref*' AND content NOT GLOB '*\u00*' THEN 0 ELSE (CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN (SELECT (SELECT parent_0."record_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS NEW."asset_id") WHERE (SELECT parent_0."record_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS NEW."asset_id") IS NOT NULL UNION SELECT record_id FROM business_asset_binding WHERE asset_id IS NEW."asset_id" UNION SELECT record_id FROM business_asset_binding WHERE asset_id IS (SELECT parent_0."asset_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS NEW."asset_id"))
        AND (1)) END); END;

CREATE TRIGGER "business_facts_0037_business_asset_position_summary_update" BEFORE UPDATE ON "business_asset_position" BEGIN SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') WHERE EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN (SELECT (SELECT parent_0."record_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS OLD."asset_id") WHERE (SELECT parent_0."record_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS OLD."asset_id") IS NOT NULL UNION SELECT record_id FROM business_asset_binding WHERE asset_id IS OLD."asset_id" UNION SELECT record_id FROM business_asset_binding WHERE asset_id IS (SELECT parent_0."asset_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS OLD."asset_id") UNION SELECT (SELECT parent_0."record_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS NEW."asset_id") WHERE (SELECT parent_0."record_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS NEW."asset_id") IS NOT NULL UNION SELECT record_id FROM business_asset_binding WHERE asset_id IS NEW."asset_id" UNION SELECT record_id FROM business_asset_binding WHERE asset_id IS (SELECT parent_0."asset_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS NEW."asset_id")))
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN (SELECT (SELECT parent_0."record_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS OLD."asset_id") WHERE (SELECT parent_0."record_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS OLD."asset_id") IS NOT NULL UNION SELECT record_id FROM business_asset_binding WHERE asset_id IS OLD."asset_id" UNION SELECT record_id FROM business_asset_binding WHERE asset_id IS (SELECT parent_0."asset_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS OLD."asset_id") UNION SELECT (SELECT parent_0."record_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS NEW."asset_id") WHERE (SELECT parent_0."record_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS NEW."asset_id") IS NOT NULL UNION SELECT record_id FROM business_asset_binding WHERE asset_id IS NEW."asset_id" UNION SELECT record_id FROM business_asset_binding WHERE asset_id IS (SELECT parent_0."asset_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS NEW."asset_id"))
        AND (((1) OR (1)) AND (1))); END;

CREATE TRIGGER "business_facts_0037_business_asset_position_update" AFTER UPDATE ON "business_asset_position" BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT account_id FROM executerecord WHERE id IS (SELECT parent_0."record_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS OLD."asset_id") UNION SELECT account_id FROM execution_input WHERE id IS (SELECT parent_0."input_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS OLD."asset_id") UNION SELECT account_id FROM account_asset_snapshot WHERE id IS (SELECT parent_0."account_snapshot_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS OLD."asset_id") UNION SELECT account_id FROM executerecord WHERE id IN (SELECT record_id FROM business_asset_binding WHERE asset_id IS OLD."asset_id") UNION SELECT account_id FROM executerecord WHERE id IN (SELECT record_id FROM business_asset_binding WHERE asset_id IS (SELECT parent_0."asset_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS OLD."asset_id")) UNION SELECT account_id FROM executerecord WHERE id IS (SELECT parent_0."record_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS NEW."asset_id") UNION SELECT account_id FROM execution_input WHERE id IS (SELECT parent_0."input_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS NEW."asset_id") UNION SELECT account_id FROM account_asset_snapshot WHERE id IS (SELECT parent_0."account_snapshot_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS NEW."asset_id") UNION SELECT account_id FROM executerecord WHERE id IN (SELECT record_id FROM business_asset_binding WHERE asset_id IS NEW."asset_id") UNION SELECT account_id FROM executerecord WHERE id IN (SELECT record_id FROM business_asset_binding WHERE asset_id IS (SELECT parent_0."asset_id" FROM "business_asset_snapshot" AS parent_0 WHERE parent_0."asset_id" IS NEW."asset_id"))) AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_business_asset_snapshot_cleanup_business_asset_position" AFTER DELETE ON "business_asset_snapshot" BEGIN DELETE FROM "business_asset_position" WHERE "asset_id" IS OLD."asset_id"; END;

CREATE TRIGGER "business_facts_0037_business_asset_snapshot_delete" AFTER DELETE ON "business_asset_snapshot" BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT account_id FROM executerecord WHERE id IS OLD."record_id" UNION SELECT account_id FROM execution_input WHERE id IS OLD."input_id" UNION SELECT account_id FROM account_asset_snapshot WHERE id IS OLD."account_snapshot_id" UNION SELECT account_id FROM executerecord WHERE id IN (SELECT record_id FROM business_asset_binding WHERE asset_id IS OLD."asset_id")) AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_business_asset_snapshot_insert" AFTER INSERT ON "business_asset_snapshot" WHEN (NEW.account_snapshot_id IS NOT NULL OR EXISTS(SELECT 1 FROM executerecord WHERE facts_complete=1 AND id IN (SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL UNION SELECT id FROM executerecord WHERE NEW."input_id" IS NOT NULL AND (input_id IS NEW."input_id" OR result_input_id IS NEW."input_id") UNION SELECT e.id FROM executerecord AS e JOIN account_asset_snapshot AS s ON e.execution_id=s.execution_id AND e.account_id=s.account_id WHERE s.id IS NEW."account_snapshot_id" AND s.execution_id IS NOT NULL UNION SELECT record_id FROM business_asset_binding WHERE asset_id IS NEW."asset_id"))) BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT account_id FROM executerecord WHERE id IS NEW."record_id" UNION SELECT account_id FROM execution_input WHERE id IS NEW."input_id" UNION SELECT account_id FROM account_asset_snapshot WHERE id IS NEW."account_snapshot_id" UNION SELECT account_id FROM executerecord WHERE id IN (SELECT record_id FROM business_asset_binding WHERE asset_id IS NEW."asset_id")) AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_business_asset_snapshot_owner_account_snapshot_id_insert" BEFORE INSERT ON "business_asset_snapshot" BEGIN SELECT RAISE(ABORT,'Business fact owner missing') WHERE NEW."account_snapshot_id" IS NOT NULL AND NOT EXISTS(SELECT 1 FROM "account_asset_snapshot" WHERE "id" IS NEW."account_snapshot_id"); END;

CREATE TRIGGER "business_facts_0037_business_asset_snapshot_owner_account_snapshot_id_update" BEFORE UPDATE ON "business_asset_snapshot" BEGIN SELECT RAISE(ABORT,'Business fact owner missing') WHERE NEW."account_snapshot_id" IS NOT NULL AND NOT EXISTS(SELECT 1 FROM "account_asset_snapshot" WHERE "id" IS NEW."account_snapshot_id"); END;

CREATE TRIGGER "business_facts_0037_business_asset_snapshot_owner_input_id_insert" BEFORE INSERT ON "business_asset_snapshot" BEGIN SELECT RAISE(ABORT,'Business fact owner missing') WHERE NEW."input_id" IS NOT NULL AND NOT EXISTS(SELECT 1 FROM "execution_input" WHERE "id" IS NEW."input_id"); END;

CREATE TRIGGER "business_facts_0037_business_asset_snapshot_owner_input_id_update" BEFORE UPDATE ON "business_asset_snapshot" BEGIN SELECT RAISE(ABORT,'Business fact owner missing') WHERE NEW."input_id" IS NOT NULL AND NOT EXISTS(SELECT 1 FROM "execution_input" WHERE "id" IS NEW."input_id"); END;

CREATE TRIGGER "business_facts_0037_business_asset_snapshot_owner_record_id_insert" BEFORE INSERT ON "business_asset_snapshot" BEGIN SELECT RAISE(ABORT,'Business fact owner missing') WHERE NEW."record_id" IS NOT NULL AND NOT EXISTS(SELECT 1 FROM "executerecord" WHERE "id" IS NEW."record_id"); END;

CREATE TRIGGER "business_facts_0037_business_asset_snapshot_owner_record_id_update" BEFORE UPDATE ON "business_asset_snapshot" BEGIN SELECT RAISE(ABORT,'Business fact owner missing') WHERE NEW."record_id" IS NOT NULL AND NOT EXISTS(SELECT 1 FROM "executerecord" WHERE "id" IS NEW."record_id"); END;

CREATE TRIGGER "business_facts_0037_business_asset_snapshot_summary_delete" BEFORE DELETE ON "business_asset_snapshot" BEGIN SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') WHERE EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN (SELECT OLD."record_id" WHERE OLD."record_id" IS NOT NULL UNION SELECT record_id FROM business_asset_binding WHERE asset_id IS OLD."asset_id"))
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN (SELECT OLD."record_id" WHERE OLD."record_id" IS NOT NULL UNION SELECT record_id FROM business_asset_binding WHERE asset_id IS OLD."asset_id")
        AND (1)); END;

CREATE TRIGGER "business_facts_0037_business_asset_snapshot_summary_insert" BEFORE INSERT ON "business_asset_snapshot" BEGIN SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') WHERE EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN (SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL UNION SELECT record_id FROM business_asset_binding WHERE asset_id IS NEW."asset_id"))
        AND CASE WHEN typeof(content)='text' AND content NOT GLOB '*business_facts_ref*' AND content NOT GLOB '*\u00*' THEN 0 ELSE (CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN (SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL UNION SELECT record_id FROM business_asset_binding WHERE asset_id IS NEW."asset_id")
        AND (1)) END); END;

CREATE TRIGGER "business_facts_0037_business_asset_snapshot_summary_update" BEFORE UPDATE ON "business_asset_snapshot" BEGIN SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') WHERE EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN (SELECT OLD."record_id" WHERE OLD."record_id" IS NOT NULL UNION SELECT record_id FROM business_asset_binding WHERE asset_id IS OLD."asset_id" UNION SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL UNION SELECT record_id FROM business_asset_binding WHERE asset_id IS NEW."asset_id"))
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN (SELECT OLD."record_id" WHERE OLD."record_id" IS NOT NULL UNION SELECT record_id FROM business_asset_binding WHERE asset_id IS OLD."asset_id" UNION SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL UNION SELECT record_id FROM business_asset_binding WHERE asset_id IS NEW."asset_id")
        AND (((1) OR (1)) AND (1))); END;

CREATE TRIGGER "business_facts_0037_business_asset_snapshot_update" AFTER UPDATE ON "business_asset_snapshot" BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT account_id FROM executerecord WHERE id IS OLD."record_id" UNION SELECT account_id FROM execution_input WHERE id IS OLD."input_id" UNION SELECT account_id FROM account_asset_snapshot WHERE id IS OLD."account_snapshot_id" UNION SELECT account_id FROM executerecord WHERE id IN (SELECT record_id FROM business_asset_binding WHERE asset_id IS OLD."asset_id") UNION SELECT account_id FROM executerecord WHERE id IS NEW."record_id" UNION SELECT account_id FROM execution_input WHERE id IS NEW."input_id" UNION SELECT account_id FROM account_asset_snapshot WHERE id IS NEW."account_snapshot_id" UNION SELECT account_id FROM executerecord WHERE id IN (SELECT record_id FROM business_asset_binding WHERE asset_id IS NEW."asset_id")) AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_business_trade_collection_delete" AFTER DELETE ON "business_trade_collection" BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT account_id FROM executerecord WHERE id IS OLD."record_id") AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_business_trade_collection_insert" AFTER INSERT ON "business_trade_collection" WHEN EXISTS(SELECT 1 FROM executerecord WHERE facts_complete=1 AND id IN (SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL)) BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT account_id FROM executerecord WHERE id IS NEW."record_id") AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_business_trade_collection_owner_record_id_insert" BEFORE INSERT ON "business_trade_collection" BEGIN SELECT RAISE(ABORT,'Business fact owner missing') WHERE NEW."record_id" IS NOT NULL AND NOT EXISTS(SELECT 1 FROM "executerecord" WHERE "id" IS NEW."record_id"); END;

CREATE TRIGGER "business_facts_0037_business_trade_collection_owner_record_id_update" BEFORE UPDATE ON "business_trade_collection" BEGIN SELECT RAISE(ABORT,'Business fact owner missing') WHERE NEW."record_id" IS NOT NULL AND NOT EXISTS(SELECT 1 FROM "executerecord" WHERE "id" IS NEW."record_id"); END;

CREATE TRIGGER "business_facts_0037_business_trade_collection_summary_delete" BEFORE DELETE ON "business_trade_collection" BEGIN SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') WHERE EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN (SELECT OLD."record_id" WHERE OLD."record_id" IS NOT NULL))
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN (SELECT OLD."record_id" WHERE OLD."record_id" IS NOT NULL)
        AND (1)); END;

CREATE TRIGGER "business_facts_0037_business_trade_collection_summary_insert" BEFORE INSERT ON "business_trade_collection" BEGIN SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') WHERE EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN (SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL))
        AND CASE WHEN typeof(content)='text' AND content NOT GLOB '*business_facts_ref*' AND content NOT GLOB '*\u00*' THEN 0 ELSE (CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN (SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL)
        AND (1)) END); END;

CREATE TRIGGER "business_facts_0037_business_trade_collection_summary_update" BEFORE UPDATE ON "business_trade_collection" BEGIN SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') WHERE EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN (SELECT OLD."record_id" WHERE OLD."record_id" IS NOT NULL UNION SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL))
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN (SELECT OLD."record_id" WHERE OLD."record_id" IS NOT NULL UNION SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL)
        AND (((1) OR (1)) AND (1))); END;

CREATE TRIGGER "business_facts_0037_business_trade_collection_update" AFTER UPDATE ON "business_trade_collection" BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT account_id FROM executerecord WHERE id IS OLD."record_id" UNION SELECT account_id FROM executerecord WHERE id IS NEW."record_id") AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_business_trade_conversion_delete" AFTER DELETE ON "business_trade_conversion" BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT account_id FROM executerecord WHERE id IS OLD."record_id") AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_business_trade_conversion_insert" AFTER INSERT ON "business_trade_conversion" WHEN EXISTS(SELECT 1 FROM executerecord WHERE facts_complete=1 AND id IN (SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL)) BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT account_id FROM executerecord WHERE id IS NEW."record_id") AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_business_trade_conversion_owner_record_id_insert" BEFORE INSERT ON "business_trade_conversion" BEGIN SELECT RAISE(ABORT,'Business fact owner missing') WHERE NEW."record_id" IS NOT NULL AND NOT EXISTS(SELECT 1 FROM "executerecord" WHERE "id" IS NEW."record_id"); END;

CREATE TRIGGER "business_facts_0037_business_trade_conversion_owner_record_id_update" BEFORE UPDATE ON "business_trade_conversion" BEGIN SELECT RAISE(ABORT,'Business fact owner missing') WHERE NEW."record_id" IS NOT NULL AND NOT EXISTS(SELECT 1 FROM "executerecord" WHERE "id" IS NEW."record_id"); END;

CREATE TRIGGER "business_facts_0037_business_trade_conversion_summary_delete" BEFORE DELETE ON "business_trade_conversion" BEGIN SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') WHERE EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN (SELECT OLD."record_id" WHERE OLD."record_id" IS NOT NULL))
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN (SELECT OLD."record_id" WHERE OLD."record_id" IS NOT NULL)
        AND (1)); END;

CREATE TRIGGER "business_facts_0037_business_trade_conversion_summary_insert" BEFORE INSERT ON "business_trade_conversion" BEGIN SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') WHERE EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN (SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL))
        AND CASE WHEN typeof(content)='text' AND content NOT GLOB '*business_facts_ref*' AND content NOT GLOB '*\u00*' THEN 0 ELSE (CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN (SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL)
        AND (1)) END); END;

CREATE TRIGGER "business_facts_0037_business_trade_conversion_summary_update" BEFORE UPDATE ON "business_trade_conversion" BEGIN SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') WHERE EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN (SELECT OLD."record_id" WHERE OLD."record_id" IS NOT NULL UNION SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL))
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN (SELECT OLD."record_id" WHERE OLD."record_id" IS NOT NULL UNION SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL)
        AND (((1) OR (1)) AND (1))); END;

CREATE TRIGGER "business_facts_0037_business_trade_conversion_update" AFTER UPDATE ON "business_trade_conversion" BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT account_id FROM executerecord WHERE id IS OLD."record_id" UNION SELECT account_id FROM executerecord WHERE id IS NEW."record_id") AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_business_trade_delete" AFTER DELETE ON "business_trade" BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT account_id FROM executerecord WHERE id IS OLD."record_id") AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_business_trade_insert" AFTER INSERT ON "business_trade" WHEN EXISTS(SELECT 1 FROM executerecord WHERE facts_complete=1 AND id IN (SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL)) BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT account_id FROM executerecord WHERE id IS NEW."record_id") AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_business_trade_owner_record_id_insert" BEFORE INSERT ON "business_trade" BEGIN SELECT RAISE(ABORT,'Business fact owner missing') WHERE NEW."record_id" IS NOT NULL AND NOT EXISTS(SELECT 1 FROM "executerecord" WHERE "id" IS NEW."record_id"); END;

CREATE TRIGGER "business_facts_0037_business_trade_owner_record_id_update" BEFORE UPDATE ON "business_trade" BEGIN SELECT RAISE(ABORT,'Business fact owner missing') WHERE NEW."record_id" IS NOT NULL AND NOT EXISTS(SELECT 1 FROM "executerecord" WHERE "id" IS NEW."record_id"); END;

CREATE TRIGGER "business_facts_0037_business_trade_summary_delete" BEFORE DELETE ON "business_trade" BEGIN SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') WHERE EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN (SELECT OLD."record_id" WHERE OLD."record_id" IS NOT NULL))
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN (SELECT OLD."record_id" WHERE OLD."record_id" IS NOT NULL)
        AND (1)); END;

CREATE TRIGGER "business_facts_0037_business_trade_summary_insert" BEFORE INSERT ON "business_trade" BEGIN SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') WHERE EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN (SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL))
        AND CASE WHEN typeof(content)='text' AND content NOT GLOB '*business_facts_ref*' AND content NOT GLOB '*\u00*' THEN 0 ELSE (CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN (SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL)
        AND (1)) END); END;

CREATE TRIGGER "business_facts_0037_business_trade_summary_update" BEFORE UPDATE ON "business_trade" BEGIN SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') WHERE EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN (SELECT OLD."record_id" WHERE OLD."record_id" IS NOT NULL UNION SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL))
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN (SELECT OLD."record_id" WHERE OLD."record_id" IS NOT NULL UNION SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL)
        AND (((1) OR (1)) AND (1))); END;

CREATE TRIGGER "business_facts_0037_business_trade_update" AFTER UPDATE ON "business_trade" BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT account_id FROM executerecord WHERE id IS OLD."record_id" UNION SELECT account_id FROM executerecord WHERE id IS NEW."record_id") AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_executerecord_cleanup_business_asset_binding" AFTER DELETE ON "executerecord" BEGIN DELETE FROM "business_asset_binding" WHERE "record_id" IS OLD."id"; END;

CREATE TRIGGER "business_facts_0037_executerecord_cleanup_business_asset_snapshot" AFTER DELETE ON "executerecord" BEGIN DELETE FROM "business_asset_snapshot" WHERE "record_id" IS OLD."id"; END;

CREATE TRIGGER "business_facts_0037_executerecord_cleanup_business_trade" AFTER DELETE ON "executerecord" BEGIN DELETE FROM "business_trade" WHERE "record_id" IS OLD."id"; END;

CREATE TRIGGER "business_facts_0037_executerecord_cleanup_business_trade_collection" AFTER DELETE ON "executerecord" BEGIN DELETE FROM "business_trade_collection" WHERE "record_id" IS OLD."id"; END;

CREATE TRIGGER "business_facts_0037_executerecord_cleanup_business_trade_conversion" AFTER DELETE ON "executerecord" BEGIN DELETE FROM "business_trade_conversion" WHERE "record_id" IS OLD."id"; END;

CREATE TRIGGER "business_facts_0037_executerecord_cleanup_execution_extension" AFTER DELETE ON "executerecord" BEGIN DELETE FROM "execution_extension" WHERE "record_id" IS OLD."id"; END;

CREATE TRIGGER "business_facts_0037_executerecord_cleanup_execution_order" AFTER DELETE ON "executerecord" BEGIN DELETE FROM "execution_order" WHERE "record_id" IS OLD."id"; END;

CREATE TRIGGER "business_facts_0037_executerecord_cleanup_execution_order_collection" AFTER DELETE ON "executerecord" BEGIN DELETE FROM "execution_order_collection" WHERE "record_id" IS OLD."id"; END;

CREATE TRIGGER "business_facts_0037_executerecord_cleanup_execution_symbol_collection" AFTER DELETE ON "executerecord" BEGIN DELETE FROM "execution_symbol_collection" WHERE "record_id" IS OLD."id"; END;

CREATE TRIGGER "business_facts_0037_executerecord_cleanup_execution_symbol_conversion" AFTER DELETE ON "executerecord" BEGIN DELETE FROM "execution_symbol_conversion" WHERE "record_id" IS OLD."id"; END;

CREATE TRIGGER "business_facts_0037_executerecord_cleanup_execution_symbol_result" AFTER DELETE ON "executerecord" BEGIN DELETE FROM "execution_symbol_result" WHERE "record_id" IS OLD."id"; END;

CREATE TRIGGER "business_facts_0037_executerecord_cleanup_execution_symbol_sizing" AFTER DELETE ON "executerecord" BEGIN DELETE FROM "execution_symbol_sizing" WHERE "record_id" IS OLD."id"; END;

CREATE TRIGGER "business_facts_0037_executerecord_cleanup_execution_symbol_target" AFTER DELETE ON "executerecord" BEGIN DELETE FROM "execution_symbol_target" WHERE "record_id" IS OLD."id"; END;

CREATE TRIGGER "business_facts_0037_executerecord_cleanup_execution_symbol_tick" AFTER DELETE ON "executerecord" BEGIN DELETE FROM "execution_symbol_tick" WHERE "record_id" IS OLD."id"; END;

CREATE TRIGGER "business_facts_0037_executerecord_cleanup_execution_termination_cancel_failure" AFTER DELETE ON "executerecord" BEGIN DELETE FROM "execution_termination_cancel_failure" WHERE "record_id" IS OLD."id"; END;

CREATE TRIGGER "business_facts_0037_executerecord_owner_input_id_insert" BEFORE INSERT ON "executerecord" BEGIN SELECT RAISE(ABORT,'Business fact owner missing') WHERE NEW."input_id" IS NOT NULL AND NOT EXISTS(SELECT 1 FROM "execution_input" WHERE "id" IS NEW."input_id"); END;

CREATE TRIGGER "business_facts_0037_executerecord_owner_input_id_update" BEFORE UPDATE ON "executerecord" BEGIN SELECT RAISE(ABORT,'Business fact owner missing') WHERE NEW."input_id" IS NOT NULL AND NOT EXISTS(SELECT 1 FROM "execution_input" WHERE "id" IS NEW."input_id"); END;

CREATE TRIGGER "business_facts_0037_executerecord_owner_result_input_id_insert" BEFORE INSERT ON "executerecord" BEGIN SELECT RAISE(ABORT,'Business fact owner missing') WHERE NEW."result_input_id" IS NOT NULL AND NOT EXISTS(SELECT 1 FROM "execution_input" WHERE "id" IS NEW."result_input_id"); END;

CREATE TRIGGER "business_facts_0037_executerecord_owner_result_input_id_update" BEFORE UPDATE ON "executerecord" BEGIN SELECT RAISE(ABORT,'Business fact owner missing') WHERE NEW."result_input_id" IS NOT NULL AND NOT EXISTS(SELECT 1 FROM "execution_input" WHERE "id" IS NEW."result_input_id"); END;

CREATE TRIGGER "business_facts_0037_executerecord_summary_delete" BEFORE DELETE ON "executerecord" BEGIN SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') WHERE EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN (SELECT OLD."id" WHERE OLD."id" IS NOT NULL))
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN (SELECT OLD."id" WHERE OLD."id" IS NOT NULL)
        AND (1)); END;

CREATE TRIGGER "business_facts_0037_executerecord_summary_insert" BEFORE INSERT ON "executerecord" BEGIN SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') WHERE EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN (SELECT NEW."id" WHERE NEW."id" IS NOT NULL))
        AND CASE WHEN typeof(content)='text' AND content NOT GLOB '*business_facts_ref*' AND content NOT GLOB '*\u00*' THEN 0 ELSE (CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN (SELECT NEW."id" WHERE NEW."id" IS NOT NULL)
        AND (1)) END); END;

CREATE TRIGGER "business_facts_0037_executerecord_summary_update" BEFORE UPDATE ON "executerecord" BEGIN SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') WHERE EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN (SELECT OLD."id" WHERE OLD."id" IS NOT NULL UNION SELECT NEW."id" WHERE NEW."id" IS NOT NULL))
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN (SELECT OLD."id" WHERE OLD."id" IS NOT NULL UNION SELECT NEW."id" WHERE NEW."id" IS NOT NULL)
        AND (((1) OR (1)) AND (OLD."id" IS NOT NEW."id" OR OLD."account_id" IS NOT NEW."account_id" OR OLD."execution_id" IS NOT NEW."execution_id" OR OLD."input_id" IS NOT NEW."input_id" OR OLD."result_input_id" IS NOT NEW."result_input_id"))); END;

CREATE TRIGGER "business_facts_0037_execution_extension_delete" AFTER DELETE ON "execution_extension" BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT account_id FROM executerecord WHERE id IS OLD."record_id") AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_execution_extension_insert" AFTER INSERT ON "execution_extension" WHEN EXISTS(SELECT 1 FROM executerecord WHERE facts_complete=1 AND id IN (SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL)) BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT account_id FROM executerecord WHERE id IS NEW."record_id") AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_execution_extension_owner_record_id_insert" BEFORE INSERT ON "execution_extension" BEGIN SELECT RAISE(ABORT,'Business fact owner missing') WHERE NEW."record_id" IS NOT NULL AND NOT EXISTS(SELECT 1 FROM "executerecord" WHERE "id" IS NEW."record_id"); END;

CREATE TRIGGER "business_facts_0037_execution_extension_owner_record_id_update" BEFORE UPDATE ON "execution_extension" BEGIN SELECT RAISE(ABORT,'Business fact owner missing') WHERE NEW."record_id" IS NOT NULL AND NOT EXISTS(SELECT 1 FROM "executerecord" WHERE "id" IS NEW."record_id"); END;

CREATE TRIGGER "business_facts_0037_execution_extension_summary_delete" BEFORE DELETE ON "execution_extension" BEGIN SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') WHERE EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN (SELECT OLD."record_id" WHERE OLD."record_id" IS NOT NULL))
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN (SELECT OLD."record_id" WHERE OLD."record_id" IS NOT NULL)
        AND (1)); END;

CREATE TRIGGER "business_facts_0037_execution_extension_summary_insert" BEFORE INSERT ON "execution_extension" BEGIN SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') WHERE EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN (SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL))
        AND CASE WHEN typeof(content)='text' AND content NOT GLOB '*business_facts_ref*' AND content NOT GLOB '*\u00*' THEN 0 ELSE (CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN (SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL)
        AND (1)) END); END;

CREATE TRIGGER "business_facts_0037_execution_extension_summary_update" BEFORE UPDATE ON "execution_extension" BEGIN SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') WHERE EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN (SELECT OLD."record_id" WHERE OLD."record_id" IS NOT NULL UNION SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL))
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN (SELECT OLD."record_id" WHERE OLD."record_id" IS NOT NULL UNION SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL)
        AND (((1) OR (1)) AND (OLD."root_kind" IS NOT NEW."root_kind" OR OLD."root_value" IS NOT NEW."root_value"))); END;

CREATE TRIGGER "business_facts_0037_execution_extension_update" AFTER UPDATE ON "execution_extension" BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT account_id FROM executerecord WHERE id IS OLD."record_id" UNION SELECT account_id FROM executerecord WHERE id IS NEW."record_id") AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_execution_input_cleanup_business_asset_snapshot" AFTER DELETE ON "execution_input" BEGIN DELETE FROM "business_asset_snapshot" WHERE "input_id" IS OLD."id"; END;

CREATE TRIGGER "business_facts_0037_execution_input_cleanup_execution_input_plan" AFTER DELETE ON "execution_input" BEGIN DELETE FROM "execution_input_plan" WHERE "input_id" IS OLD."id"; END;

CREATE TRIGGER "business_facts_0037_execution_input_cleanup_execution_input_symbol" AFTER DELETE ON "execution_input" BEGIN DELETE FROM "execution_input_symbol" WHERE "input_id" IS OLD."id"; END;

CREATE TRIGGER "business_facts_0037_execution_input_cleanup_execution_input_target" AFTER DELETE ON "execution_input" BEGIN DELETE FROM "execution_input_target" WHERE "input_id" IS OLD."id"; END;

CREATE TRIGGER "business_facts_0037_execution_input_delete" AFTER DELETE ON "execution_input" BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT id FROM account WHERE id IS OLD."account_id" UNION SELECT account_id FROM execution_input WHERE id IS OLD."id") AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_execution_input_insert" AFTER INSERT ON "execution_input" WHEN EXISTS(SELECT 1 FROM executerecord WHERE facts_complete=1 AND id IN (SELECT id FROM executerecord WHERE NEW."id" IS NOT NULL AND (input_id IS NEW."id" OR result_input_id IS NEW."id"))) BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT id FROM account WHERE id IS NEW."account_id" UNION SELECT account_id FROM execution_input WHERE id IS NEW."id") AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_execution_input_plan_delete" AFTER DELETE ON "execution_input_plan" BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT account_id FROM execution_input WHERE id IS OLD."input_id") AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_execution_input_plan_insert" AFTER INSERT ON "execution_input_plan" WHEN EXISTS(SELECT 1 FROM executerecord WHERE facts_complete=1 AND id IN (SELECT id FROM executerecord WHERE NEW."input_id" IS NOT NULL AND (input_id IS NEW."input_id" OR result_input_id IS NEW."input_id"))) BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT account_id FROM execution_input WHERE id IS NEW."input_id") AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_execution_input_plan_owner_input_id_insert" BEFORE INSERT ON "execution_input_plan" BEGIN SELECT RAISE(ABORT,'Business fact owner missing') WHERE NEW."input_id" IS NOT NULL AND NOT EXISTS(SELECT 1 FROM "execution_input" WHERE "id" IS NEW."input_id"); END;

CREATE TRIGGER "business_facts_0037_execution_input_plan_owner_input_id_update" BEFORE UPDATE ON "execution_input_plan" BEGIN SELECT RAISE(ABORT,'Business fact owner missing') WHERE NEW."input_id" IS NOT NULL AND NOT EXISTS(SELECT 1 FROM "execution_input" WHERE "id" IS NEW."input_id"); END;

CREATE TRIGGER "business_facts_0037_execution_input_plan_summary_delete" BEFORE DELETE ON "execution_input_plan" BEGIN SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') WHERE EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN (SELECT NULL WHERE 0))
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN (SELECT NULL WHERE 0)
        AND (1)); END;

CREATE TRIGGER "business_facts_0037_execution_input_plan_summary_insert" BEFORE INSERT ON "execution_input_plan" BEGIN SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') WHERE EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN (SELECT NULL WHERE 0))
        AND CASE WHEN typeof(content)='text' AND content NOT GLOB '*business_facts_ref*' AND content NOT GLOB '*\u00*' THEN 0 ELSE (CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN (SELECT NULL WHERE 0)
        AND (1)) END); END;

CREATE TRIGGER "business_facts_0037_execution_input_plan_summary_update" BEFORE UPDATE ON "execution_input_plan" BEGIN SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') WHERE EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN (SELECT NULL WHERE 0 UNION SELECT NULL WHERE 0))
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN (SELECT NULL WHERE 0 UNION SELECT NULL WHERE 0)
        AND (((1) OR (1)) AND (1))); END;

CREATE TRIGGER "business_facts_0037_execution_input_plan_update" AFTER UPDATE ON "execution_input_plan" BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT account_id FROM execution_input WHERE id IS OLD."input_id" UNION SELECT account_id FROM execution_input WHERE id IS NEW."input_id") AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_execution_input_summary_delete" BEFORE DELETE ON "execution_input" BEGIN SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') WHERE EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN (SELECT NULL WHERE 0))
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN (SELECT NULL WHERE 0)
        AND (1)); END;

CREATE TRIGGER "business_facts_0037_execution_input_summary_insert" BEFORE INSERT ON "execution_input" BEGIN SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') WHERE EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN (SELECT NULL WHERE 0))
        AND CASE WHEN typeof(content)='text' AND content NOT GLOB '*business_facts_ref*' AND content NOT GLOB '*\u00*' THEN 0 ELSE (CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN (SELECT NULL WHERE 0)
        AND (1)) END); END;

CREATE TRIGGER "business_facts_0037_execution_input_summary_update" BEFORE UPDATE ON "execution_input" BEGIN SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') WHERE EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN (SELECT NULL WHERE 0 UNION SELECT NULL WHERE 0))
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN (SELECT NULL WHERE 0 UNION SELECT NULL WHERE 0)
        AND (((1) OR (1)) AND (1))); END;

CREATE TRIGGER "business_facts_0037_execution_input_symbol_delete" AFTER DELETE ON "execution_input_symbol" BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT account_id FROM execution_input WHERE id IS OLD."input_id") AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_execution_input_symbol_insert" AFTER INSERT ON "execution_input_symbol" WHEN EXISTS(SELECT 1 FROM executerecord WHERE facts_complete=1 AND id IN (SELECT id FROM executerecord WHERE NEW."input_id" IS NOT NULL AND (input_id IS NEW."input_id" OR result_input_id IS NEW."input_id"))) BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT account_id FROM execution_input WHERE id IS NEW."input_id") AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_execution_input_symbol_owner_input_id_insert" BEFORE INSERT ON "execution_input_symbol" BEGIN SELECT RAISE(ABORT,'Business fact owner missing') WHERE NEW."input_id" IS NOT NULL AND NOT EXISTS(SELECT 1 FROM "execution_input" WHERE "id" IS NEW."input_id"); END;

CREATE TRIGGER "business_facts_0037_execution_input_symbol_owner_input_id_update" BEFORE UPDATE ON "execution_input_symbol" BEGIN SELECT RAISE(ABORT,'Business fact owner missing') WHERE NEW."input_id" IS NOT NULL AND NOT EXISTS(SELECT 1 FROM "execution_input" WHERE "id" IS NEW."input_id"); END;

CREATE TRIGGER "business_facts_0037_execution_input_symbol_summary_delete" BEFORE DELETE ON "execution_input_symbol" BEGIN SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') WHERE EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN (SELECT NULL WHERE 0))
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN (SELECT NULL WHERE 0)
        AND (1)); END;

CREATE TRIGGER "business_facts_0037_execution_input_symbol_summary_insert" BEFORE INSERT ON "execution_input_symbol" BEGIN SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') WHERE EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN (SELECT NULL WHERE 0))
        AND CASE WHEN typeof(content)='text' AND content NOT GLOB '*business_facts_ref*' AND content NOT GLOB '*\u00*' THEN 0 ELSE (CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN (SELECT NULL WHERE 0)
        AND (1)) END); END;

CREATE TRIGGER "business_facts_0037_execution_input_symbol_summary_update" BEFORE UPDATE ON "execution_input_symbol" BEGIN SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') WHERE EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN (SELECT NULL WHERE 0 UNION SELECT NULL WHERE 0))
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN (SELECT NULL WHERE 0 UNION SELECT NULL WHERE 0)
        AND (((1) OR (1)) AND (1))); END;

CREATE TRIGGER "business_facts_0037_execution_input_symbol_update" AFTER UPDATE ON "execution_input_symbol" BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT account_id FROM execution_input WHERE id IS OLD."input_id" UNION SELECT account_id FROM execution_input WHERE id IS NEW."input_id") AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_execution_input_target_delete" AFTER DELETE ON "execution_input_target" BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT account_id FROM execution_input WHERE id IS OLD."input_id") AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_execution_input_target_insert" AFTER INSERT ON "execution_input_target" WHEN EXISTS(SELECT 1 FROM executerecord WHERE facts_complete=1 AND id IN (SELECT id FROM executerecord WHERE NEW."input_id" IS NOT NULL AND (input_id IS NEW."input_id" OR result_input_id IS NEW."input_id"))) BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT account_id FROM execution_input WHERE id IS NEW."input_id") AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_execution_input_target_owner_input_id_insert" BEFORE INSERT ON "execution_input_target" BEGIN SELECT RAISE(ABORT,'Business fact owner missing') WHERE NEW."input_id" IS NOT NULL AND NOT EXISTS(SELECT 1 FROM "execution_input" WHERE "id" IS NEW."input_id"); END;

CREATE TRIGGER "business_facts_0037_execution_input_target_owner_input_id_update" BEFORE UPDATE ON "execution_input_target" BEGIN SELECT RAISE(ABORT,'Business fact owner missing') WHERE NEW."input_id" IS NOT NULL AND NOT EXISTS(SELECT 1 FROM "execution_input" WHERE "id" IS NEW."input_id"); END;

CREATE TRIGGER "business_facts_0037_execution_input_target_summary_delete" BEFORE DELETE ON "execution_input_target" BEGIN SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') WHERE EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN (SELECT NULL WHERE 0))
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN (SELECT NULL WHERE 0)
        AND (1)); END;

CREATE TRIGGER "business_facts_0037_execution_input_target_summary_insert" BEFORE INSERT ON "execution_input_target" BEGIN SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') WHERE EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN (SELECT NULL WHERE 0))
        AND CASE WHEN typeof(content)='text' AND content NOT GLOB '*business_facts_ref*' AND content NOT GLOB '*\u00*' THEN 0 ELSE (CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN (SELECT NULL WHERE 0)
        AND (1)) END); END;

CREATE TRIGGER "business_facts_0037_execution_input_target_summary_update" BEFORE UPDATE ON "execution_input_target" BEGIN SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') WHERE EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN (SELECT NULL WHERE 0 UNION SELECT NULL WHERE 0))
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN (SELECT NULL WHERE 0 UNION SELECT NULL WHERE 0)
        AND (((1) OR (1)) AND (1))); END;

CREATE TRIGGER "business_facts_0037_execution_input_target_update" AFTER UPDATE ON "execution_input_target" BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT account_id FROM execution_input WHERE id IS OLD."input_id" UNION SELECT account_id FROM execution_input WHERE id IS NEW."input_id") AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_execution_input_update" AFTER UPDATE ON "execution_input" BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT id FROM account WHERE id IS OLD."account_id" UNION SELECT account_id FROM execution_input WHERE id IS OLD."id" UNION SELECT id FROM account WHERE id IS NEW."account_id" UNION SELECT account_id FROM execution_input WHERE id IS NEW."id") AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_execution_order_collection_cleanup_execution_order" AFTER DELETE ON "execution_order_collection" BEGIN DELETE FROM "execution_order" WHERE "path" IS OLD."path" AND "record_id" IS OLD."record_id"; END;

CREATE TRIGGER "business_facts_0037_execution_order_collection_delete" AFTER DELETE ON "execution_order_collection" BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT account_id FROM executerecord WHERE id IS OLD."record_id") AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_execution_order_collection_insert" AFTER INSERT ON "execution_order_collection" WHEN EXISTS(SELECT 1 FROM executerecord WHERE facts_complete=1 AND id IN (SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL)) BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT account_id FROM executerecord WHERE id IS NEW."record_id") AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_execution_order_collection_owner_record_id_insert" BEFORE INSERT ON "execution_order_collection" BEGIN SELECT RAISE(ABORT,'Business fact owner missing') WHERE NEW."record_id" IS NOT NULL AND NOT EXISTS(SELECT 1 FROM "executerecord" WHERE "id" IS NEW."record_id"); END;

CREATE TRIGGER "business_facts_0037_execution_order_collection_owner_record_id_update" BEFORE UPDATE ON "execution_order_collection" BEGIN SELECT RAISE(ABORT,'Business fact owner missing') WHERE NEW."record_id" IS NOT NULL AND NOT EXISTS(SELECT 1 FROM "executerecord" WHERE "id" IS NEW."record_id"); END;

CREATE TRIGGER "business_facts_0037_execution_order_collection_summary_delete" BEFORE DELETE ON "execution_order_collection" BEGIN SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') WHERE EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN (SELECT OLD."record_id" WHERE OLD."record_id" IS NOT NULL))
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN (SELECT OLD."record_id" WHERE OLD."record_id" IS NOT NULL)
        AND (1)); END;

CREATE TRIGGER "business_facts_0037_execution_order_collection_summary_insert" BEFORE INSERT ON "execution_order_collection" BEGIN SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') WHERE EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN (SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL))
        AND CASE WHEN typeof(content)='text' AND content NOT GLOB '*business_facts_ref*' AND content NOT GLOB '*\u00*' THEN 0 ELSE (CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN (SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL)
        AND (1)) END); END;

CREATE TRIGGER "business_facts_0037_execution_order_collection_summary_update" BEFORE UPDATE ON "execution_order_collection" BEGIN SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') WHERE EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN (SELECT OLD."record_id" WHERE OLD."record_id" IS NOT NULL UNION SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL))
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN (SELECT OLD."record_id" WHERE OLD."record_id" IS NOT NULL UNION SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL)
        AND (((1) OR (1)) AND (1))); END;

CREATE TRIGGER "business_facts_0037_execution_order_collection_update" AFTER UPDATE ON "execution_order_collection" BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT account_id FROM executerecord WHERE id IS OLD."record_id" UNION SELECT account_id FROM executerecord WHERE id IS NEW."record_id") AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_execution_order_delete" AFTER DELETE ON "execution_order" BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT account_id FROM executerecord WHERE id IS OLD."record_id" UNION SELECT account_id FROM executerecord WHERE id IS (SELECT parent_0."record_id" FROM "execution_order_collection" AS parent_0 WHERE parent_0."record_id" IS OLD."record_id" AND parent_0."path" IS OLD."path")) AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_execution_order_insert" AFTER INSERT ON "execution_order" WHEN EXISTS(SELECT 1 FROM executerecord WHERE facts_complete=1 AND id IN (SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL UNION SELECT (SELECT parent_0."record_id" FROM "execution_order_collection" AS parent_0 WHERE parent_0."record_id" IS NEW."record_id" AND parent_0."path" IS NEW."path") WHERE (SELECT parent_0."record_id" FROM "execution_order_collection" AS parent_0 WHERE parent_0."record_id" IS NEW."record_id" AND parent_0."path" IS NEW."path") IS NOT NULL)) BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT account_id FROM executerecord WHERE id IS NEW."record_id" UNION SELECT account_id FROM executerecord WHERE id IS (SELECT parent_0."record_id" FROM "execution_order_collection" AS parent_0 WHERE parent_0."record_id" IS NEW."record_id" AND parent_0."path" IS NEW."path")) AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_execution_order_owner_record_id_insert" BEFORE INSERT ON "execution_order" BEGIN SELECT RAISE(ABORT,'Business fact owner missing') WHERE NEW."record_id" IS NOT NULL AND NOT EXISTS(SELECT 1 FROM "executerecord" WHERE "id" IS NEW."record_id"); END;

CREATE TRIGGER "business_facts_0037_execution_order_owner_record_id_path_insert" BEFORE INSERT ON "execution_order" BEGIN SELECT RAISE(ABORT,'Business fact owner missing') WHERE NEW."record_id" IS NOT NULL AND NEW."path" IS NOT NULL AND NOT EXISTS(SELECT 1 FROM "execution_order_collection" WHERE "record_id" IS NEW."record_id" AND "path" IS NEW."path"); END;

CREATE TRIGGER "business_facts_0037_execution_order_owner_record_id_path_update" BEFORE UPDATE ON "execution_order" BEGIN SELECT RAISE(ABORT,'Business fact owner missing') WHERE NEW."record_id" IS NOT NULL AND NEW."path" IS NOT NULL AND NOT EXISTS(SELECT 1 FROM "execution_order_collection" WHERE "record_id" IS NEW."record_id" AND "path" IS NEW."path"); END;

CREATE TRIGGER "business_facts_0037_execution_order_owner_record_id_update" BEFORE UPDATE ON "execution_order" BEGIN SELECT RAISE(ABORT,'Business fact owner missing') WHERE NEW."record_id" IS NOT NULL AND NOT EXISTS(SELECT 1 FROM "executerecord" WHERE "id" IS NEW."record_id"); END;

CREATE TRIGGER "business_facts_0037_execution_order_summary_delete" BEFORE DELETE ON "execution_order" BEGIN SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') WHERE EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN (SELECT OLD."record_id" WHERE OLD."record_id" IS NOT NULL UNION SELECT (SELECT parent_0."record_id" FROM "execution_order_collection" AS parent_0 WHERE parent_0."record_id" IS OLD."record_id" AND parent_0."path" IS OLD."path") WHERE (SELECT parent_0."record_id" FROM "execution_order_collection" AS parent_0 WHERE parent_0."record_id" IS OLD."record_id" AND parent_0."path" IS OLD."path") IS NOT NULL))
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN (SELECT OLD."record_id" WHERE OLD."record_id" IS NOT NULL UNION SELECT (SELECT parent_0."record_id" FROM "execution_order_collection" AS parent_0 WHERE parent_0."record_id" IS OLD."record_id" AND parent_0."path" IS OLD."path") WHERE (SELECT parent_0."record_id" FROM "execution_order_collection" AS parent_0 WHERE parent_0."record_id" IS OLD."record_id" AND parent_0."path" IS OLD."path") IS NOT NULL)
        AND (1)); END;

CREATE TRIGGER "business_facts_0037_execution_order_summary_insert" BEFORE INSERT ON "execution_order" BEGIN SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') WHERE EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN (SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL UNION SELECT (SELECT parent_0."record_id" FROM "execution_order_collection" AS parent_0 WHERE parent_0."record_id" IS NEW."record_id" AND parent_0."path" IS NEW."path") WHERE (SELECT parent_0."record_id" FROM "execution_order_collection" AS parent_0 WHERE parent_0."record_id" IS NEW."record_id" AND parent_0."path" IS NEW."path") IS NOT NULL))
        AND CASE WHEN typeof(content)='text' AND content NOT GLOB '*business_facts_ref*' AND content NOT GLOB '*\u00*' THEN 0 ELSE (CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN (SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL UNION SELECT (SELECT parent_0."record_id" FROM "execution_order_collection" AS parent_0 WHERE parent_0."record_id" IS NEW."record_id" AND parent_0."path" IS NEW."path") WHERE (SELECT parent_0."record_id" FROM "execution_order_collection" AS parent_0 WHERE parent_0."record_id" IS NEW."record_id" AND parent_0."path" IS NEW."path") IS NOT NULL)
        AND (1)) END); END;

CREATE TRIGGER "business_facts_0037_execution_order_summary_update" BEFORE UPDATE ON "execution_order" BEGIN SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') WHERE EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN (SELECT OLD."record_id" WHERE OLD."record_id" IS NOT NULL UNION SELECT (SELECT parent_0."record_id" FROM "execution_order_collection" AS parent_0 WHERE parent_0."record_id" IS OLD."record_id" AND parent_0."path" IS OLD."path") WHERE (SELECT parent_0."record_id" FROM "execution_order_collection" AS parent_0 WHERE parent_0."record_id" IS OLD."record_id" AND parent_0."path" IS OLD."path") IS NOT NULL UNION SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL UNION SELECT (SELECT parent_0."record_id" FROM "execution_order_collection" AS parent_0 WHERE parent_0."record_id" IS NEW."record_id" AND parent_0."path" IS NEW."path") WHERE (SELECT parent_0."record_id" FROM "execution_order_collection" AS parent_0 WHERE parent_0."record_id" IS NEW."record_id" AND parent_0."path" IS NEW."path") IS NOT NULL))
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN (SELECT OLD."record_id" WHERE OLD."record_id" IS NOT NULL UNION SELECT (SELECT parent_0."record_id" FROM "execution_order_collection" AS parent_0 WHERE parent_0."record_id" IS OLD."record_id" AND parent_0."path" IS OLD."path") WHERE (SELECT parent_0."record_id" FROM "execution_order_collection" AS parent_0 WHERE parent_0."record_id" IS OLD."record_id" AND parent_0."path" IS OLD."path") IS NOT NULL UNION SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL UNION SELECT (SELECT parent_0."record_id" FROM "execution_order_collection" AS parent_0 WHERE parent_0."record_id" IS NEW."record_id" AND parent_0."path" IS NEW."path") WHERE (SELECT parent_0."record_id" FROM "execution_order_collection" AS parent_0 WHERE parent_0."record_id" IS NEW."record_id" AND parent_0."path" IS NEW."path") IS NOT NULL)
        AND (((1) OR (1)) AND (1))); END;

CREATE TRIGGER "business_facts_0037_execution_order_update" AFTER UPDATE ON "execution_order" BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT account_id FROM executerecord WHERE id IS OLD."record_id" UNION SELECT account_id FROM executerecord WHERE id IS (SELECT parent_0."record_id" FROM "execution_order_collection" AS parent_0 WHERE parent_0."record_id" IS OLD."record_id" AND parent_0."path" IS OLD."path") UNION SELECT account_id FROM executerecord WHERE id IS NEW."record_id" UNION SELECT account_id FROM executerecord WHERE id IS (SELECT parent_0."record_id" FROM "execution_order_collection" AS parent_0 WHERE parent_0."record_id" IS NEW."record_id" AND parent_0."path" IS NEW."path")) AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_execution_symbol_collection_delete" AFTER DELETE ON "execution_symbol_collection" BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT account_id FROM executerecord WHERE id IS OLD."record_id") AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_execution_symbol_collection_insert" AFTER INSERT ON "execution_symbol_collection" WHEN EXISTS(SELECT 1 FROM executerecord WHERE facts_complete=1 AND id IN (SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL)) BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT account_id FROM executerecord WHERE id IS NEW."record_id") AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_execution_symbol_collection_owner_record_id_insert" BEFORE INSERT ON "execution_symbol_collection" BEGIN SELECT RAISE(ABORT,'Business fact owner missing') WHERE NEW."record_id" IS NOT NULL AND NOT EXISTS(SELECT 1 FROM "executerecord" WHERE "id" IS NEW."record_id"); END;

CREATE TRIGGER "business_facts_0037_execution_symbol_collection_owner_record_id_update" BEFORE UPDATE ON "execution_symbol_collection" BEGIN SELECT RAISE(ABORT,'Business fact owner missing') WHERE NEW."record_id" IS NOT NULL AND NOT EXISTS(SELECT 1 FROM "executerecord" WHERE "id" IS NEW."record_id"); END;

CREATE TRIGGER "business_facts_0037_execution_symbol_collection_summary_delete" BEFORE DELETE ON "execution_symbol_collection" BEGIN SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') WHERE EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN (SELECT OLD."record_id" WHERE OLD."record_id" IS NOT NULL))
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN (SELECT OLD."record_id" WHERE OLD."record_id" IS NOT NULL)
        AND (1)); END;

CREATE TRIGGER "business_facts_0037_execution_symbol_collection_summary_insert" BEFORE INSERT ON "execution_symbol_collection" BEGIN SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') WHERE EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN (SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL))
        AND CASE WHEN typeof(content)='text' AND content NOT GLOB '*business_facts_ref*' AND content NOT GLOB '*\u00*' THEN 0 ELSE (CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN (SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL)
        AND (1)) END); END;

CREATE TRIGGER "business_facts_0037_execution_symbol_collection_summary_update" BEFORE UPDATE ON "execution_symbol_collection" BEGIN SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') WHERE EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN (SELECT OLD."record_id" WHERE OLD."record_id" IS NOT NULL UNION SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL))
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN (SELECT OLD."record_id" WHERE OLD."record_id" IS NOT NULL UNION SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL)
        AND (((1) OR (1)) AND (1))); END;

CREATE TRIGGER "business_facts_0037_execution_symbol_collection_update" AFTER UPDATE ON "execution_symbol_collection" BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT account_id FROM executerecord WHERE id IS OLD."record_id" UNION SELECT account_id FROM executerecord WHERE id IS NEW."record_id") AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_execution_symbol_conversion_delete" AFTER DELETE ON "execution_symbol_conversion" BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT account_id FROM executerecord WHERE id IS OLD."record_id") AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_execution_symbol_conversion_insert" AFTER INSERT ON "execution_symbol_conversion" WHEN EXISTS(SELECT 1 FROM executerecord WHERE facts_complete=1 AND id IN (SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL)) BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT account_id FROM executerecord WHERE id IS NEW."record_id") AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_execution_symbol_conversion_owner_record_id_insert" BEFORE INSERT ON "execution_symbol_conversion" BEGIN SELECT RAISE(ABORT,'Business fact owner missing') WHERE NEW."record_id" IS NOT NULL AND NOT EXISTS(SELECT 1 FROM "executerecord" WHERE "id" IS NEW."record_id"); END;

CREATE TRIGGER "business_facts_0037_execution_symbol_conversion_owner_record_id_update" BEFORE UPDATE ON "execution_symbol_conversion" BEGIN SELECT RAISE(ABORT,'Business fact owner missing') WHERE NEW."record_id" IS NOT NULL AND NOT EXISTS(SELECT 1 FROM "executerecord" WHERE "id" IS NEW."record_id"); END;

CREATE TRIGGER "business_facts_0037_execution_symbol_conversion_summary_delete" BEFORE DELETE ON "execution_symbol_conversion" BEGIN SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') WHERE EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN (SELECT OLD."record_id" WHERE OLD."record_id" IS NOT NULL))
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN (SELECT OLD."record_id" WHERE OLD."record_id" IS NOT NULL)
        AND (1)); END;

CREATE TRIGGER "business_facts_0037_execution_symbol_conversion_summary_insert" BEFORE INSERT ON "execution_symbol_conversion" BEGIN SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') WHERE EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN (SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL))
        AND CASE WHEN typeof(content)='text' AND content NOT GLOB '*business_facts_ref*' AND content NOT GLOB '*\u00*' THEN 0 ELSE (CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN (SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL)
        AND (1)) END); END;

CREATE TRIGGER "business_facts_0037_execution_symbol_conversion_summary_update" BEFORE UPDATE ON "execution_symbol_conversion" BEGIN SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') WHERE EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN (SELECT OLD."record_id" WHERE OLD."record_id" IS NOT NULL UNION SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL))
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN (SELECT OLD."record_id" WHERE OLD."record_id" IS NOT NULL UNION SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL)
        AND (((1) OR (1)) AND (1))); END;

CREATE TRIGGER "business_facts_0037_execution_symbol_conversion_update" AFTER UPDATE ON "execution_symbol_conversion" BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT account_id FROM executerecord WHERE id IS OLD."record_id" UNION SELECT account_id FROM executerecord WHERE id IS NEW."record_id") AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_execution_symbol_result_delete" AFTER DELETE ON "execution_symbol_result" BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT account_id FROM executerecord WHERE id IS OLD."record_id") AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_execution_symbol_result_insert" AFTER INSERT ON "execution_symbol_result" WHEN EXISTS(SELECT 1 FROM executerecord WHERE facts_complete=1 AND id IN (SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL)) BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT account_id FROM executerecord WHERE id IS NEW."record_id") AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_execution_symbol_result_owner_record_id_insert" BEFORE INSERT ON "execution_symbol_result" BEGIN SELECT RAISE(ABORT,'Business fact owner missing') WHERE NEW."record_id" IS NOT NULL AND NOT EXISTS(SELECT 1 FROM "executerecord" WHERE "id" IS NEW."record_id"); END;

CREATE TRIGGER "business_facts_0037_execution_symbol_result_owner_record_id_update" BEFORE UPDATE ON "execution_symbol_result" BEGIN SELECT RAISE(ABORT,'Business fact owner missing') WHERE NEW."record_id" IS NOT NULL AND NOT EXISTS(SELECT 1 FROM "executerecord" WHERE "id" IS NEW."record_id"); END;

CREATE TRIGGER "business_facts_0037_execution_symbol_result_summary_delete" BEFORE DELETE ON "execution_symbol_result" BEGIN SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') WHERE EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN (SELECT OLD."record_id" WHERE OLD."record_id" IS NOT NULL))
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN (SELECT OLD."record_id" WHERE OLD."record_id" IS NOT NULL)
        AND (1)); END;

CREATE TRIGGER "business_facts_0037_execution_symbol_result_summary_insert" BEFORE INSERT ON "execution_symbol_result" BEGIN SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') WHERE EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN (SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL))
        AND CASE WHEN typeof(content)='text' AND content NOT GLOB '*business_facts_ref*' AND content NOT GLOB '*\u00*' THEN 0 ELSE (CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN (SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL)
        AND (1)) END); END;

CREATE TRIGGER "business_facts_0037_execution_symbol_result_summary_update" BEFORE UPDATE ON "execution_symbol_result" BEGIN SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') WHERE EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN (SELECT OLD."record_id" WHERE OLD."record_id" IS NOT NULL UNION SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL))
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN (SELECT OLD."record_id" WHERE OLD."record_id" IS NOT NULL UNION SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL)
        AND (((1) OR (1)) AND (OLD."record_id" IS NOT NEW."record_id" OR OLD."path" IS NOT NEW."path" OR OLD."ordinal" IS NOT NEW."ordinal" OR OLD."map_key" IS NOT NEW."map_key" OR OLD."node_kind" IS NOT NEW."node_kind" OR OLD."invalid_value" IS NOT NEW."invalid_value" OR OLD."audit_evidence" IS NOT NEW."audit_evidence" OR OLD."memory" IS NOT NEW."memory"))); END;

CREATE TRIGGER "business_facts_0037_execution_symbol_result_update" AFTER UPDATE ON "execution_symbol_result" BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT account_id FROM executerecord WHERE id IS OLD."record_id" UNION SELECT account_id FROM executerecord WHERE id IS NEW."record_id") AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_execution_symbol_sizing_delete" AFTER DELETE ON "execution_symbol_sizing" BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT account_id FROM executerecord WHERE id IS OLD."record_id") AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_execution_symbol_sizing_insert" AFTER INSERT ON "execution_symbol_sizing" WHEN EXISTS(SELECT 1 FROM executerecord WHERE facts_complete=1 AND id IN (SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL)) BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT account_id FROM executerecord WHERE id IS NEW."record_id") AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_execution_symbol_sizing_owner_record_id_insert" BEFORE INSERT ON "execution_symbol_sizing" BEGIN SELECT RAISE(ABORT,'Business fact owner missing') WHERE NEW."record_id" IS NOT NULL AND NOT EXISTS(SELECT 1 FROM "executerecord" WHERE "id" IS NEW."record_id"); END;

CREATE TRIGGER "business_facts_0037_execution_symbol_sizing_owner_record_id_update" BEFORE UPDATE ON "execution_symbol_sizing" BEGIN SELECT RAISE(ABORT,'Business fact owner missing') WHERE NEW."record_id" IS NOT NULL AND NOT EXISTS(SELECT 1 FROM "executerecord" WHERE "id" IS NEW."record_id"); END;

CREATE TRIGGER "business_facts_0037_execution_symbol_sizing_summary_delete" BEFORE DELETE ON "execution_symbol_sizing" BEGIN SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') WHERE EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN (SELECT OLD."record_id" WHERE OLD."record_id" IS NOT NULL))
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN (SELECT OLD."record_id" WHERE OLD."record_id" IS NOT NULL)
        AND (1)); END;

CREATE TRIGGER "business_facts_0037_execution_symbol_sizing_summary_insert" BEFORE INSERT ON "execution_symbol_sizing" BEGIN SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') WHERE EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN (SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL))
        AND CASE WHEN typeof(content)='text' AND content NOT GLOB '*business_facts_ref*' AND content NOT GLOB '*\u00*' THEN 0 ELSE (CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN (SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL)
        AND (1)) END); END;

CREATE TRIGGER "business_facts_0037_execution_symbol_sizing_summary_update" BEFORE UPDATE ON "execution_symbol_sizing" BEGIN SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') WHERE EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN (SELECT OLD."record_id" WHERE OLD."record_id" IS NOT NULL UNION SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL))
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN (SELECT OLD."record_id" WHERE OLD."record_id" IS NOT NULL UNION SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL)
        AND (((1) OR (1)) AND (1))); END;

CREATE TRIGGER "business_facts_0037_execution_symbol_sizing_update" AFTER UPDATE ON "execution_symbol_sizing" BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT account_id FROM executerecord WHERE id IS OLD."record_id" UNION SELECT account_id FROM executerecord WHERE id IS NEW."record_id") AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_execution_symbol_target_delete" AFTER DELETE ON "execution_symbol_target" BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT account_id FROM executerecord WHERE id IS OLD."record_id") AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_execution_symbol_target_insert" AFTER INSERT ON "execution_symbol_target" WHEN EXISTS(SELECT 1 FROM executerecord WHERE facts_complete=1 AND id IN (SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL)) BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT account_id FROM executerecord WHERE id IS NEW."record_id") AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_execution_symbol_target_owner_record_id_insert" BEFORE INSERT ON "execution_symbol_target" BEGIN SELECT RAISE(ABORT,'Business fact owner missing') WHERE NEW."record_id" IS NOT NULL AND NOT EXISTS(SELECT 1 FROM "executerecord" WHERE "id" IS NEW."record_id"); END;

CREATE TRIGGER "business_facts_0037_execution_symbol_target_owner_record_id_update" BEFORE UPDATE ON "execution_symbol_target" BEGIN SELECT RAISE(ABORT,'Business fact owner missing') WHERE NEW."record_id" IS NOT NULL AND NOT EXISTS(SELECT 1 FROM "executerecord" WHERE "id" IS NEW."record_id"); END;

CREATE TRIGGER "business_facts_0037_execution_symbol_target_summary_delete" BEFORE DELETE ON "execution_symbol_target" BEGIN SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') WHERE EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN (SELECT OLD."record_id" WHERE OLD."record_id" IS NOT NULL))
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN (SELECT OLD."record_id" WHERE OLD."record_id" IS NOT NULL)
        AND (1)); END;

CREATE TRIGGER "business_facts_0037_execution_symbol_target_summary_insert" BEFORE INSERT ON "execution_symbol_target" BEGIN SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') WHERE EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN (SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL))
        AND CASE WHEN typeof(content)='text' AND content NOT GLOB '*business_facts_ref*' AND content NOT GLOB '*\u00*' THEN 0 ELSE (CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN (SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL)
        AND (1)) END); END;

CREATE TRIGGER "business_facts_0037_execution_symbol_target_summary_update" BEFORE UPDATE ON "execution_symbol_target" BEGIN SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') WHERE EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN (SELECT OLD."record_id" WHERE OLD."record_id" IS NOT NULL UNION SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL))
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN (SELECT OLD."record_id" WHERE OLD."record_id" IS NOT NULL UNION SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL)
        AND (((1) OR (1)) AND (1))); END;

CREATE TRIGGER "business_facts_0037_execution_symbol_target_update" AFTER UPDATE ON "execution_symbol_target" BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT account_id FROM executerecord WHERE id IS OLD."record_id" UNION SELECT account_id FROM executerecord WHERE id IS NEW."record_id") AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_execution_symbol_tick_delete" AFTER DELETE ON "execution_symbol_tick" BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT account_id FROM executerecord WHERE id IS OLD."record_id") AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_execution_symbol_tick_insert" AFTER INSERT ON "execution_symbol_tick" WHEN EXISTS(SELECT 1 FROM executerecord WHERE facts_complete=1 AND id IN (SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL)) BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT account_id FROM executerecord WHERE id IS NEW."record_id") AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_execution_symbol_tick_owner_record_id_insert" BEFORE INSERT ON "execution_symbol_tick" BEGIN SELECT RAISE(ABORT,'Business fact owner missing') WHERE NEW."record_id" IS NOT NULL AND NOT EXISTS(SELECT 1 FROM "executerecord" WHERE "id" IS NEW."record_id"); END;

CREATE TRIGGER "business_facts_0037_execution_symbol_tick_owner_record_id_update" BEFORE UPDATE ON "execution_symbol_tick" BEGIN SELECT RAISE(ABORT,'Business fact owner missing') WHERE NEW."record_id" IS NOT NULL AND NOT EXISTS(SELECT 1 FROM "executerecord" WHERE "id" IS NEW."record_id"); END;

CREATE TRIGGER "business_facts_0037_execution_symbol_tick_summary_delete" BEFORE DELETE ON "execution_symbol_tick" BEGIN SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') WHERE EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN (SELECT OLD."record_id" WHERE OLD."record_id" IS NOT NULL))
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN (SELECT OLD."record_id" WHERE OLD."record_id" IS NOT NULL)
        AND (1)); END;

CREATE TRIGGER "business_facts_0037_execution_symbol_tick_summary_insert" BEFORE INSERT ON "execution_symbol_tick" BEGIN SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') WHERE EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN (SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL))
        AND CASE WHEN typeof(content)='text' AND content NOT GLOB '*business_facts_ref*' AND content NOT GLOB '*\u00*' THEN 0 ELSE (CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN (SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL)
        AND (1)) END); END;

CREATE TRIGGER "business_facts_0037_execution_symbol_tick_summary_update" BEFORE UPDATE ON "execution_symbol_tick" BEGIN SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') WHERE EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN (SELECT OLD."record_id" WHERE OLD."record_id" IS NOT NULL UNION SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL))
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN (SELECT OLD."record_id" WHERE OLD."record_id" IS NOT NULL UNION SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL)
        AND (((1) OR (1)) AND (1))); END;

CREATE TRIGGER "business_facts_0037_execution_symbol_tick_update" AFTER UPDATE ON "execution_symbol_tick" BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT account_id FROM executerecord WHERE id IS OLD."record_id" UNION SELECT account_id FROM executerecord WHERE id IS NEW."record_id") AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_execution_termination_cancel_failure_delete" AFTER DELETE ON "execution_termination_cancel_failure" BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT account_id FROM executerecord WHERE id IS OLD."record_id") AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_execution_termination_cancel_failure_insert" AFTER INSERT ON "execution_termination_cancel_failure" WHEN EXISTS(SELECT 1 FROM executerecord WHERE facts_complete=1 AND id IN (SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL)) BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT account_id FROM executerecord WHERE id IS NEW."record_id") AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_execution_termination_cancel_failure_owner_record_id_insert" BEFORE INSERT ON "execution_termination_cancel_failure" BEGIN SELECT RAISE(ABORT,'Business fact owner missing') WHERE NEW."record_id" IS NOT NULL AND NOT EXISTS(SELECT 1 FROM "executerecord" WHERE "id" IS NEW."record_id"); END;

CREATE TRIGGER "business_facts_0037_execution_termination_cancel_failure_owner_record_id_update" BEFORE UPDATE ON "execution_termination_cancel_failure" BEGIN SELECT RAISE(ABORT,'Business fact owner missing') WHERE NEW."record_id" IS NOT NULL AND NOT EXISTS(SELECT 1 FROM "executerecord" WHERE "id" IS NEW."record_id"); END;

CREATE TRIGGER "business_facts_0037_execution_termination_cancel_failure_summary_delete" BEFORE DELETE ON "execution_termination_cancel_failure" BEGIN SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') WHERE EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN (SELECT OLD."record_id" WHERE OLD."record_id" IS NOT NULL))
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN (SELECT OLD."record_id" WHERE OLD."record_id" IS NOT NULL)
        AND (1)); END;

CREATE TRIGGER "business_facts_0037_execution_termination_cancel_failure_summary_insert" BEFORE INSERT ON "execution_termination_cancel_failure" BEGIN SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') WHERE EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN (SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL))
        AND CASE WHEN typeof(content)='text' AND content NOT GLOB '*business_facts_ref*' AND content NOT GLOB '*\u00*' THEN 0 ELSE (CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN (SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL)
        AND (1)) END); END;

CREATE TRIGGER "business_facts_0037_execution_termination_cancel_failure_summary_update" BEFORE UPDATE ON "execution_termination_cancel_failure" BEGIN SELECT RAISE(ABORT,'Execution summary must be materialized before changing its result') WHERE EXISTS(SELECT 1 FROM execution_artifact WHERE
        execution_id IN (SELECT execution_id FROM executerecord WHERE id IN (SELECT OLD."record_id" WHERE OLD."record_id" IS NOT NULL UNION SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL))
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.view') END='result'
        AND CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.record_id') END IN (SELECT OLD."record_id" WHERE OLD."record_id" IS NOT NULL UNION SELECT NEW."record_id" WHERE NEW."record_id" IS NOT NULL)
        AND (((1) OR (1)) AND (1))); END;

CREATE TRIGGER "business_facts_0037_execution_termination_cancel_failure_update" AFTER UPDATE ON "execution_termination_cancel_failure" BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT account_id FROM executerecord WHERE id IS OLD."record_id" UNION SELECT account_id FROM executerecord WHERE id IS NEW."record_id") AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER business_facts_0037_input_delete_guard BEFORE DELETE ON execution_input BEGIN SELECT RAISE(ABORT,'Execution input is still referenced') WHERE EXISTS(SELECT 1 FROM executerecord WHERE (input_id=OLD.id OR result_input_id=OLD.id) AND EXISTS(SELECT 1 FROM account WHERE id=executerecord.account_id)); END;

CREATE TRIGGER business_facts_0037_input_owner_update BEFORE UPDATE OF account_id,id ON execution_input BEGIN SELECT RAISE(ABORT,'Execution input owner mismatch') WHERE NEW.account_id IS NOT OLD.account_id OR NEW.id IS NOT OLD.id; END;

CREATE TRIGGER business_facts_0037_notification_outbox_delete BEFORE DELETE ON notification_event BEGIN SELECT RAISE(ABORT,'Frozen notification context still references this outbox') WHERE (1) AND EXISTS(SELECT 1 FROM execution_extension WHERE
        CASE WHEN json_valid(extra) THEN json_extract(extra,'$.server_notification_context."$axile_notification_context"') END=1
        AND CASE WHEN json_valid(extra) THEN json_extract(extra,'$.server_notification_context.event_id') END=OLD.id); END;

CREATE TRIGGER business_facts_0037_notification_outbox_insert BEFORE INSERT ON notification_event BEGIN SELECT RAISE(ABORT,'Frozen notification context still references this outbox') WHERE EXISTS(SELECT 1 FROM execution_extension WHERE
        CASE WHEN json_valid(extra) THEN json_extract(extra,'$.server_notification_context."$axile_notification_context"') END=1
        AND CASE WHEN json_valid(extra) THEN json_extract(extra,'$.server_notification_context.event_id') END=NEW.id) AND NOT EXISTS(SELECT 1 FROM notification_event WHERE id=NEW.id AND account_id IS NEW.account_id AND event_type IS NEW.event_type AND context IS NEW.context); END;

CREATE TRIGGER business_facts_0037_notification_outbox_update BEFORE UPDATE OF id,account_id,event_type,context ON notification_event BEGIN SELECT RAISE(ABORT,'Frozen notification context still references this outbox') WHERE (OLD."id" IS NOT NEW."id" OR OLD."account_id" IS NOT NEW."account_id" OR OLD."event_type" IS NOT NEW."event_type" OR OLD."context" IS NOT NEW."context") AND EXISTS(SELECT 1 FROM execution_extension WHERE
        CASE WHEN json_valid(extra) THEN json_extract(extra,'$.server_notification_context."$axile_notification_context"') END=1
        AND CASE WHEN json_valid(extra) THEN json_extract(extra,'$.server_notification_context.event_id') END=OLD.id); END;

CREATE TRIGGER business_facts_0037_record_cleanup AFTER DELETE ON executerecord BEGIN DELETE FROM "execution_extension" WHERE record_id=OLD.id; DELETE FROM "business_trade_conversion" WHERE record_id=OLD.id; DELETE FROM "business_trade" WHERE record_id=OLD.id; DELETE FROM "business_trade_collection" WHERE record_id=OLD.id; DELETE FROM "execution_order" WHERE record_id=OLD.id; DELETE FROM "execution_order_collection" WHERE record_id=OLD.id; DELETE FROM "business_asset_binding" WHERE record_id=OLD.id; DELETE FROM "business_asset_snapshot" WHERE record_id=OLD.id; DELETE FROM "execution_symbol_target" WHERE record_id=OLD.id; DELETE FROM "execution_symbol_tick" WHERE record_id=OLD.id; DELETE FROM "execution_symbol_conversion" WHERE record_id=OLD.id; DELETE FROM "execution_symbol_sizing" WHERE record_id=OLD.id; DELETE FROM "execution_symbol_result" WHERE record_id=OLD.id; DELETE FROM "execution_symbol_collection" WHERE record_id=OLD.id; DELETE FROM "execution_termination_cancel_failure" WHERE record_id=OLD.id;  DELETE FROM execution_input
                WHERE id IN (OLD.input_id,OLD.result_input_id)
                AND NOT EXISTS(SELECT 1 FROM executerecord WHERE input_id=execution_input.id OR result_input_id=execution_input.id)
                AND NOT EXISTS(SELECT 1 FROM execution_artifact WHERE CASE WHEN json_valid(content)
                    THEN json_extract(content,'$.business_facts_ref.input_id') END=execution_input.id)
                AND NOT EXISTS(SELECT 1 FROM execution_artifact WHERE CASE WHEN json_valid(content)
                    THEN json_extract(content,'$.business_facts_ref.asset_id') END IN
                    (SELECT asset_id FROM business_asset_snapshot WHERE input_id=execution_input.id))
                AND NOT EXISTS(SELECT 1 FROM execution_event WHERE CASE WHEN json_valid(details)
                    THEN json_extract(details,'$.decision.business_facts_ref.input_id') END=execution_input.id)
                AND NOT EXISTS(SELECT 1 FROM account_asset_snapshot WHERE asset_fact_id IN
                    (SELECT asset_id FROM business_asset_snapshot WHERE input_id=execution_input.id)); END;

CREATE TRIGGER business_facts_0037_record_input_insert BEFORE INSERT ON executerecord BEGIN SELECT RAISE(ABORT,'Execution input owner mismatch') WHERE NEW.input_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM execution_input WHERE id=NEW.input_id AND account_id=NEW.account_id); SELECT RAISE(ABORT,'Execution input owner mismatch') WHERE NEW.result_input_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM execution_input WHERE id=NEW.result_input_id AND account_id=NEW.account_id); END;

CREATE TRIGGER business_facts_0037_record_input_update BEFORE UPDATE ON executerecord BEGIN SELECT RAISE(ABORT,'Execution input owner mismatch') WHERE NEW.input_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM execution_input WHERE id=NEW.input_id AND account_id=NEW.account_id); SELECT RAISE(ABORT,'Execution input owner mismatch') WHERE NEW.result_input_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM execution_input WHERE id=NEW.result_input_id AND account_id=NEW.account_id); END;

CREATE TRIGGER business_facts_0037_record_retain_assets BEFORE DELETE ON executerecord WHEN EXISTS(SELECT 1 FROM account WHERE id=OLD.account_id) BEGIN SELECT RAISE(ABORT,'Referenced asset requires a durable input owner') WHERE OLD.input_id IS NULL AND EXISTS(SELECT 1 FROM business_asset_snapshot AS a WHERE a.record_id=OLD.id AND (EXISTS(SELECT 1 FROM account_asset_snapshot WHERE asset_fact_id=a.asset_id)
        OR EXISTS(SELECT 1 FROM execution_artifact WHERE CASE WHEN json_valid(content)
            THEN json_extract(content,'$.business_facts_ref.asset_id') END=a.asset_id))); UPDATE business_asset_snapshot SET record_id=NULL,input_id=OLD.input_id WHERE record_id=OLD.id AND (EXISTS(SELECT 1 FROM account_asset_snapshot WHERE asset_fact_id=business_asset_snapshot.asset_id)
        OR EXISTS(SELECT 1 FROM execution_artifact WHERE CASE WHEN json_valid(content)
            THEN json_extract(content,'$.business_facts_ref.asset_id') END=business_asset_snapshot.asset_id)); END;

CREATE TRIGGER business_facts_0037_snapshot_asset_owner_insert BEFORE INSERT ON account_asset_snapshot WHEN NEW.asset_fact_id IS NOT NULL BEGIN SELECT RAISE(ABORT,'Snapshot asset owner mismatch') WHERE (SELECT COALESCE(r.account_id,i.account_id,s.account_id)
        FROM business_asset_snapshot AS a
        LEFT JOIN executerecord AS r ON r.id=a.record_id
        LEFT JOIN execution_input AS i ON i.id=a.input_id
        LEFT JOIN account_asset_snapshot AS s ON s.id=a.account_snapshot_id
        WHERE a.asset_id=NEW.asset_fact_id) IS NOT NEW.account_id; END;

CREATE TRIGGER business_facts_0037_snapshot_asset_owner_update BEFORE UPDATE ON account_asset_snapshot WHEN NEW.asset_fact_id IS NOT NULL BEGIN SELECT RAISE(ABORT,'Snapshot asset owner mismatch') WHERE (SELECT COALESCE(r.account_id,i.account_id,s.account_id)
        FROM business_asset_snapshot AS a
        LEFT JOIN executerecord AS r ON r.id=a.record_id
        LEFT JOIN execution_input AS i ON i.id=a.input_id
        LEFT JOIN account_asset_snapshot AS s ON s.id=a.account_snapshot_id
        WHERE a.asset_id=NEW.asset_fact_id) IS NOT NEW.account_id; END;

CREATE TRIGGER "business_facts_0037_target_weight_snapshot_cleanup_target_weight_snapshot_weight" AFTER DELETE ON "target_weight_snapshot" BEGIN DELETE FROM "target_weight_snapshot_weight" WHERE "snapshot_id" IS OLD."id"; END;

CREATE TRIGGER "business_facts_0037_target_weight_snapshot_delete" AFTER DELETE ON "target_weight_snapshot" BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT id FROM account WHERE id IS OLD."account_id") AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_target_weight_snapshot_insert" AFTER INSERT ON "target_weight_snapshot" BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT id FROM account WHERE id IS NEW."account_id") AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_target_weight_snapshot_update" AFTER UPDATE ON "target_weight_snapshot" BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT id FROM account WHERE id IS OLD."account_id" UNION SELECT id FROM account WHERE id IS NEW."account_id") AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_target_weight_snapshot_weight_delete" AFTER DELETE ON "target_weight_snapshot_weight" BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT id FROM account WHERE id IS (SELECT parent_0."account_id" FROM "target_weight_snapshot" AS parent_0 WHERE parent_0."id" IS OLD."snapshot_id")) AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_target_weight_snapshot_weight_insert" AFTER INSERT ON "target_weight_snapshot_weight" WHEN EXISTS(SELECT 1 FROM executerecord WHERE facts_complete=1 AND id IN (SELECT id FROM executerecord WHERE execution_id IS (SELECT parent_0."execution_id" FROM "target_weight_snapshot" AS parent_0 WHERE parent_0."id" IS NEW."snapshot_id") AND (SELECT parent_0."execution_id" FROM "target_weight_snapshot" AS parent_0 WHERE parent_0."id" IS NEW."snapshot_id") IS NOT NULL)) BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT id FROM account WHERE id IS (SELECT parent_0."account_id" FROM "target_weight_snapshot" AS parent_0 WHERE parent_0."id" IS NEW."snapshot_id")) AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;

CREATE TRIGGER "business_facts_0037_target_weight_snapshot_weight_owner_snapshot_id_insert" BEFORE INSERT ON "target_weight_snapshot_weight" BEGIN SELECT RAISE(ABORT,'Business fact owner missing') WHERE NEW."snapshot_id" IS NOT NULL AND NOT EXISTS(SELECT 1 FROM "target_weight_snapshot" WHERE "id" IS NEW."snapshot_id"); END;

CREATE TRIGGER "business_facts_0037_target_weight_snapshot_weight_owner_snapshot_id_update" BEFORE UPDATE ON "target_weight_snapshot_weight" BEGIN SELECT RAISE(ABORT,'Business fact owner missing') WHERE NEW."snapshot_id" IS NOT NULL AND NOT EXISTS(SELECT 1 FROM "target_weight_snapshot" WHERE "id" IS NEW."snapshot_id"); END;

CREATE TRIGGER "business_facts_0037_target_weight_snapshot_weight_update" AFTER UPDATE ON "target_weight_snapshot_weight" BEGIN          INSERT INTO account_analysis(account_id,source_version)
            SELECT id,1 FROM account WHERE id IN (SELECT id FROM account WHERE id IS (SELECT parent_0."account_id" FROM "target_weight_snapshot" AS parent_0 WHERE parent_0."id" IS OLD."snapshot_id") UNION SELECT id FROM account WHERE id IS (SELECT parent_0."account_id" FROM "target_weight_snapshot" AS parent_0 WHERE parent_0."id" IS NEW."snapshot_id")) AND 1
            ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,
            requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
            failures=0,retry_at=0,error=NULL; END;
