CREATE TABLE order_submission (
	submission_uid TEXT NOT NULL, 
	account_id INTEGER NOT NULL, 
	occurred_at_ms INTEGER NOT NULL, 
	revision INTEGER NOT NULL, 
	data JSON NOT NULL, 
	PRIMARY KEY (submission_uid), 
	FOREIGN KEY(account_id) REFERENCES account (id) ON DELETE CASCADE
);

CREATE INDEX ix_order_submission_account_time ON order_submission (account_id, occurred_at_ms);
