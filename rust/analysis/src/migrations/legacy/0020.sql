CREATE TABLE account_settings (account_id INTEGER NOT NULL, connection JSON NOT NULL, execution JSON NOT NULL, schedule JSON NOT NULL, notification JSON NOT NULL, backtest JSON NOT NULL, PRIMARY KEY (account_id), FOREIGN KEY(account_id) REFERENCES account (id) ON DELETE CASCADE);

CREATE TABLE account_notification_state (
	account_id INTEGER NOT NULL, 
	last_success_at TEXT NOT NULL, 
	execution_id TEXT, 
	PRIMARY KEY (account_id), 
	FOREIGN KEY(account_id) REFERENCES account (id) ON DELETE CASCADE
);
