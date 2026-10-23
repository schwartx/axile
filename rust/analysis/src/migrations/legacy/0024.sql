CREATE TABLE account_control_session (
	id INTEGER NOT NULL, 
	instance_id VARCHAR NOT NULL, 
	account_id INTEGER NOT NULL, 
	execution_id VARCHAR NOT NULL, 
	timezone VARCHAR NOT NULL, 
	started_at_ms INTEGER NOT NULL, 
	finished_at_ms INTEGER, 
	status VARCHAR NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (instance_id), 
	FOREIGN KEY(account_id) REFERENCES account (id) ON DELETE CASCADE
);

CREATE INDEX ix_account_control_session_account_id ON account_control_session (account_id);
