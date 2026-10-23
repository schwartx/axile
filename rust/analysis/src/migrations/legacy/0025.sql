CREATE TABLE account_connection_state (
	account_id INTEGER NOT NULL, 
	config_key TEXT NOT NULL, 
	instance_id TEXT NOT NULL, 
	sequence INTEGER NOT NULL, 
	status TEXT NOT NULL, 
	observed_at TEXT, 
	message TEXT, 
	error JSON, 
	PRIMARY KEY (account_id), 
	FOREIGN KEY(account_id) REFERENCES account (id) ON DELETE CASCADE
);
