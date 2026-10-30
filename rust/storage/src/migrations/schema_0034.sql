-- Final public 0034 base schema; business facts and incremental results use their shared DDL.
CREATE TABLE "account" (
	name TEXT NOT NULL,
	market TEXT NOT NULL,
	trade_channel TEXT NOT NULL,
	remark TEXT,
	brokerage TEXT NOT NULL,
	portfolio_id INTEGER,
	id INTEGER NOT NULL,
	updated_at TEXT NOT NULL,
	created_at TEXT NOT NULL, copied_from_account_id INTEGER, copied_from_account_name TEXT,
	PRIMARY KEY (id AUTOINCREMENT),
	FOREIGN KEY(portfolio_id) REFERENCES portfolio (id) ON DELETE SET NULL
);

CREATE TABLE account_analysis (
	account_id INTEGER NOT NULL,
	source_version INTEGER DEFAULT '1' NOT NULL,
	requested BOOLEAN DEFAULT '0' NOT NULL,
	running_version INTEGER,
	failures INTEGER DEFAULT '0' NOT NULL,
	retry_at_us INTEGER DEFAULT '0' NOT NULL,
	error TEXT,
	running_token TEXT, dirty_record INTEGER,
	PRIMARY KEY (account_id),
	FOREIGN KEY(account_id) REFERENCES account (id) ON DELETE CASCADE
);

CREATE TABLE account_asset_snapshot (
	id INTEGER NOT NULL,
	account_id INTEGER NOT NULL,
	source TEXT NOT NULL,
	execution_id TEXT,
	created_at TEXT NOT NULL, "asset_fact_id" TEXT REFERENCES business_asset_snapshot(asset_id) ON DELETE SET NULL,
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

CREATE TABLE "account_notification_state"(account_id INTEGER NOT NULL PRIMARY KEY REFERENCES account(id) ON DELETE CASCADE,last_success_at TEXT,execution_id TEXT,last_attempt_at TEXT,last_attempt_execution_id TEXT,last_attempt_ok BOOLEAN,last_attempt_error TEXT, last_attempt_event_type VARCHAR);

CREATE TABLE account_settings (account_id INTEGER NOT NULL, connection JSON NOT NULL, execution JSON NOT NULL, schedule JSON NOT NULL, notification JSON NOT NULL, backtest JSON NOT NULL, PRIMARY KEY (account_id), FOREIGN KEY(account_id) REFERENCES account (id) ON DELETE CASCADE);

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

CREATE TABLE "alembic_version" (version_num VARCHAR(32) NOT NULL, CONSTRAINT "alembic_version_pkc" PRIMARY KEY (version_num));

CREATE TABLE analysis_calendar_day(channel TEXT NOT NULL,day TEXT NOT NULL,status TEXT NOT NULL,calendar_id TEXT,label TEXT,PRIMARY KEY(channel,day));

CREATE TABLE executerecord (
	execution_id TEXT,
	is_success INTEGER NOT NULL,
	id INTEGER NOT NULL,
	account_id INTEGER NOT NULL,
	created_at TEXT NOT NULL, "input_id" TEXT REFERENCES execution_input(id), "result_input_id" TEXT REFERENCES execution_input(id), "status" TEXT, "symbol" TEXT, "technical_detail" TEXT, "task_status" TEXT, "error" TEXT, "outcome" TEXT, "outcome_reason" TEXT, "reason_code" TEXT, "msg" TEXT, "execution_kind" TEXT, "channel_type" TEXT, "execution_time" FLOAT, "success" BOOLEAN, "display_status" TEXT, "display_error" TEXT, "display_outcome" TEXT, "display_outcome_reason" TEXT, "termination_reason" TEXT, "termination_mode" TEXT, "termination_trigger" TEXT, "termination_requested_at" TEXT, "termination_acked_at" TEXT, "termination_finished_at" TEXT, "termination_cancel_attempted" BOOLEAN, "termination_forced" BOOLEAN, "termination_cancel_unconfirmed" BOOLEAN, "result_input_kind" TEXT NOT NULL DEFAULT 'absent', "result_input_value" JSON, "audit_evidence" JSON NOT NULL DEFAULT '{}', "termination_evidence" JSON NOT NULL DEFAULT '{}', "termination_kind" TEXT NOT NULL DEFAULT 'absent', "termination_cancel_ids_kind" TEXT NOT NULL DEFAULT 'absent', "termination_cancel_count" INTEGER, "facts_complete" BOOLEAN NOT NULL DEFAULT 0, result_staged INTEGER NOT NULL DEFAULT 0,
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
	calculated_at TEXT NOT NULL, "raw_weights_kind" TEXT DEFAULT 'null' NOT NULL, "normalized_weights_kind" TEXT DEFAULT 'null' NOT NULL, "weights_metadata" JSON DEFAULT '{}' NOT NULL,
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

CREATE INDEX ix_control_session_account_status_finished ON account_control_session(account_id,status,finished_at_ms);

CREATE INDEX ix_executerecord_account_id_created_at ON executerecord (account_id, created_at);

CREATE INDEX ix_executerecord_account_id_id ON executerecord (account_id, id);

CREATE INDEX ix_executerecord_account_id_success_id ON executerecord (account_id, is_success, id);

CREATE INDEX ix_executerecord_account_time_id ON executerecord(account_id,axile_time(created_at) DESC,id DESC);

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

CREATE INDEX ix_portfolioaccount_account_id_id ON portfolioaccount(account_id,id);

CREATE INDEX ix_schedule_skip_account_time_id ON schedule_skip(account_id,axile_time(triggered_at) DESC,id DESC);

CREATE INDEX ix_schedule_skip_account_triggered ON schedule_skip (account_id, triggered_at);

CREATE INDEX ix_submission_warning_account_time ON order_submission_warning (account_id, occurred_at_ms);

CREATE INDEX ix_supplement_step_execution_id ON supplement_step (execution_id);

CREATE INDEX ix_target_weight_snapshot_account_id_id ON target_weight_snapshot (account_id, id);

CREATE UNIQUE INDEX ix_target_weight_snapshot_execution_id ON target_weight_snapshot (execution_id);

CREATE INDEX ix_target_weight_snapshot_portfolio_id_id ON target_weight_snapshot (portfolio_id, id);

CREATE UNIQUE INDEX uq_intent_account_queued ON execution_intent (account_id) WHERE status = 'QUEUED';

CREATE UNIQUE INDEX uq_intent_account_running ON execution_intent (account_id) WHERE status IN ('RUNNING', 'TERMINATING');

CREATE TRIGGER account_settings_delete AFTER DELETE ON account BEGIN DELETE FROM account_settings WHERE account_id=OLD.id; DELETE FROM account_notification_state WHERE account_id=OLD.id; END;
