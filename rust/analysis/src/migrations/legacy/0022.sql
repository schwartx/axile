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
