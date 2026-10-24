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

CREATE INDEX ix_account_runtime_sync_attempt_account ON accountruntimesyncattempt (account_id);
