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

CREATE INDEX ix_supplement_step_execution_id ON supplement_step (execution_id);
