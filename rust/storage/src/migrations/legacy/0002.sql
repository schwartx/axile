CREATE TABLE trading_calendar (
	calendar_id TEXT NOT NULL, 
	cal_date DATE NOT NULL, 
	is_open BOOLEAN NOT NULL, 
	updated_at TEXT NOT NULL, 
	PRIMARY KEY (calendar_id, cal_date)
);

CREATE TABLE trading_calendar_override (
	calendar_id TEXT NOT NULL, 
	cal_date DATE NOT NULL, 
	is_open BOOLEAN NOT NULL, 
	updated_at TEXT NOT NULL, 
	PRIMARY KEY (calendar_id, cal_date)
);

CREATE TABLE trading_calendar_config (
	calendar_id TEXT NOT NULL, 
	refresh_kind TEXT NOT NULL, 
	function_code TEXT NOT NULL, 
	last_sync_at TEXT, 
	updated_at TEXT NOT NULL, 
	PRIMARY KEY (calendar_id)
);
