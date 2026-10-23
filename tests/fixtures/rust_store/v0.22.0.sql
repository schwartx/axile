-- Generated from Axile v0.22.0 (public head 0033), empty SQLite.
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

CREATE INDEX ix_execution_intent_account_id_created_at ON execution_intent (account_id, created_at);

CREATE UNIQUE INDEX ix_execution_intent_execution_id ON execution_intent (execution_id);

CREATE INDEX ix_order_submission_account_time ON order_submission (account_id, occurred_at_ms);

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
	engine_version TEXT, 
	PRIMARY KEY (account_id), 
	FOREIGN KEY(account_id) REFERENCES account (id) ON DELETE CASCADE
);

CREATE TABLE account_asset_snapshot (
	id INTEGER NOT NULL, 
	account_id INTEGER NOT NULL, 
	assets JSON NOT NULL, 
	source TEXT NOT NULL, 
	execution_id TEXT, 
	created_at TEXT NOT NULL, 
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
	raw_weights JSON, 
	normalized_weights JSON, 
	source TEXT NOT NULL, 
	execution_id TEXT, 
	calculated_at TEXT NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(portfolio_id) REFERENCES portfolio (id) ON DELETE CASCADE, 
	FOREIGN KEY(account_id) REFERENCES account (id) ON DELETE SET NULL
);

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
