CREATE TABLE _futures_close_policy_backup (
	account_id INTEGER NOT NULL, 
	previous_execution JSON NOT NULL, 
	migrated_execution JSON NOT NULL, 
	PRIMARY KEY (account_id)
);
