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

CREATE INDEX ix_submission_warning_account_time ON order_submission_warning (account_id, occurred_at_ms);
