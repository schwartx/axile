-- Final public 0034 analysis coordination and calendar facts.
CREATE TABLE account_analysis (
	account_id INTEGER NOT NULL,
	source_version INTEGER DEFAULT '1' NOT NULL,
	requested BOOLEAN DEFAULT '0' NOT NULL,
	running_version INTEGER,
	failures INTEGER DEFAULT '0' NOT NULL,
	retry_at_us INTEGER DEFAULT '0' NOT NULL,
	error TEXT,
	running_token TEXT, dirty_record INTEGER,
	PRIMARY KEY (account_id),
	FOREIGN KEY(account_id) REFERENCES account (id) ON DELETE CASCADE
);

CREATE TABLE analysis_calendar_day(channel TEXT NOT NULL,day TEXT NOT NULL,status TEXT NOT NULL,calendar_id TEXT,label TEXT,PRIMARY KEY(channel,day));
