CREATE TABLE time_check_settings (
	id INTEGER NOT NULL, 
	version INTEGER NOT NULL, 
	data JSON NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE time_check_state (
	id INTEGER NOT NULL, 
	data JSON NOT NULL, 
	PRIMARY KEY (id)
);
