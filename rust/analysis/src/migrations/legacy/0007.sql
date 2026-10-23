CREATE TABLE IF NOT EXISTS execution_intent (
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

CREATE UNIQUE INDEX ix_execution_intent_execution_id ON execution_intent (execution_id);

CREATE INDEX ix_execution_intent_account_id_created_at ON execution_intent (account_id, created_at);

CREATE UNIQUE INDEX IF NOT EXISTS uq_intent_account_queued ON execution_intent (account_id) WHERE status = 'QUEUED';
CREATE UNIQUE INDEX IF NOT EXISTS uq_intent_account_running ON execution_intent (account_id) WHERE status IN ('RUNNING', 'TERMINATING');
