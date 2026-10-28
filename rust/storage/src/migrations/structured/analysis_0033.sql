-- Frozen v0.23.1 cache structure and invalidation triggers for downgrade to 0033.
CREATE TABLE account_analysis (
	account_id INTEGER NOT NULL,
	source_version INTEGER DEFAULT '1' NOT NULL,
	published_version INTEGER DEFAULT '0' NOT NULL,
	current_snapshot TEXT,
	previous_snapshot TEXT,
	requested BOOLEAN DEFAULT '0' NOT NULL,
	running_version INTEGER,
	failures INTEGER DEFAULT '0' NOT NULL,
	retry_at FLOAT DEFAULT '0' NOT NULL,
	error TEXT,
	logic_version TEXT,
	engine_version TEXT,
	PRIMARY KEY (account_id),
	FOREIGN KEY(account_id) REFERENCES account (id) ON DELETE CASCADE
);

CREATE TABLE performance_cost_execution (
	snapshot_id TEXT NOT NULL,
	record_id INTEGER NOT NULL,
	time FLOAT NOT NULL,
	success BOOLEAN NOT NULL,
	noop BOOLEAN NOT NULL,
	payload JSON NOT NULL,
	PRIMARY KEY (snapshot_id, record_id),
	FOREIGN KEY(snapshot_id) REFERENCES performance_snapshot (id) ON DELETE CASCADE
);

CREATE TABLE performance_cost_trade (
	snapshot_id TEXT NOT NULL,
	id INTEGER NOT NULL,
	record_id INTEGER NOT NULL,
	symbol TEXT NOT NULL,
	time FLOAT NOT NULL,
	day TEXT NOT NULL,
	value FLOAT,
	cost FLOAT,
	loss_bp FLOAT,
	fee FLOAT,
	fee_currency TEXT,
	estimated BOOLEAN NOT NULL,
	payload JSON NOT NULL,
	PRIMARY KEY (snapshot_id, id),
	FOREIGN KEY(snapshot_id) REFERENCES performance_snapshot (id) ON DELETE CASCADE
);

CREATE TABLE performance_snapshot (
	id TEXT NOT NULL,
	account_id INTEGER NOT NULL,
	source_version INTEGER NOT NULL,
	logic_version TEXT NOT NULL,
	engine_version TEXT NOT NULL,
	computed_at TEXT NOT NULL,
	data_until TEXT,
	settings JSON NOT NULL,
	ranges JSON NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(account_id) REFERENCES account (id) ON DELETE CASCADE
);
CREATE INDEX ix_performance_execution_time ON performance_cost_execution (snapshot_id, time);

CREATE INDEX ix_performance_snapshot_account_id ON performance_snapshot (account_id);

CREATE INDEX ix_performance_trade_record ON performance_cost_trade (snapshot_id, record_id, time);

CREATE INDEX ix_performance_trade_symbol ON performance_cost_trade (snapshot_id, symbol, time);

CREATE TRIGGER analysis_account_delete AFTER DELETE ON account
        BEGIN
        DELETE FROM performance_cost_trade WHERE snapshot_id IN (SELECT id FROM performance_snapshot WHERE account_id = OLD.id);
        DELETE FROM performance_cost_execution WHERE snapshot_id IN (SELECT id FROM performance_snapshot WHERE account_id = OLD.id);
        DELETE FROM performance_snapshot WHERE account_id = OLD.id;
        DELETE FROM account_analysis WHERE account_id = OLD.id;
        END;

CREATE TRIGGER analysis_account_settings
        AFTER UPDATE OF backtest ON account_settings
        WHEN json_extract(OLD.backtest, '$.backtest_weight_type') IS NOT json_extract(NEW.backtest, '$.backtest_weight_type')
          OR json_extract(OLD.backtest, '$.backtest_fee_rate') IS NOT json_extract(NEW.backtest, '$.backtest_fee_rate')
        BEGIN
        INSERT INTO account_analysis (account_id, source_version) VALUES (NEW.account_id, 1)
        ON CONFLICT(account_id) DO UPDATE SET source_version = source_version + 1,
        requested = CASE WHEN failures > 0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
        THEN 1 ELSE requested END,
        failures = 0, retry_at = 0, error = NULL;
        END;

CREATE TRIGGER analysis_executerecord_delete AFTER DELETE ON executerecord BEGIN INSERT INTO account_analysis (account_id, source_version)
                SELECT OLD.account_id, 1 WHERE OLD.account_id IS NOT NULL
                AND EXISTS (SELECT 1 FROM account WHERE id = OLD.account_id)

                ON CONFLICT(account_id) DO UPDATE SET source_version = source_version + 1,
                requested = CASE WHEN failures > 0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
                failures = 0, retry_at = 0, error = NULL; END;

CREATE TRIGGER analysis_executerecord_insert AFTER INSERT ON executerecord BEGIN INSERT INTO account_analysis (account_id, source_version)
                SELECT NEW.account_id, 1 WHERE NEW.account_id IS NOT NULL
                AND EXISTS (SELECT 1 FROM account WHERE id = NEW.account_id)

                ON CONFLICT(account_id) DO UPDATE SET source_version = source_version + 1,
                requested = CASE WHEN failures > 0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
                failures = 0, retry_at = 0, error = NULL; END;

CREATE TRIGGER analysis_executerecord_update AFTER UPDATE ON executerecord BEGIN INSERT INTO account_analysis (account_id, source_version)
                SELECT OLD.account_id, 1 WHERE OLD.account_id IS NOT NULL
                AND EXISTS (SELECT 1 FROM account WHERE id = OLD.account_id)

                ON CONFLICT(account_id) DO UPDATE SET source_version = source_version + 1,
                requested = CASE WHEN failures > 0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
                failures = 0, retry_at = 0, error = NULL; INSERT INTO account_analysis (account_id, source_version)
                SELECT NEW.account_id, 1 WHERE NEW.account_id IS NOT NULL
                AND EXISTS (SELECT 1 FROM account WHERE id = NEW.account_id)
                AND NEW.account_id IS NOT OLD.account_id
                ON CONFLICT(account_id) DO UPDATE SET source_version = source_version + 1,
                requested = CASE WHEN failures > 0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
                failures = 0, retry_at = 0, error = NULL; END;

CREATE TRIGGER analysis_portfolioaccount_delete AFTER DELETE ON portfolioaccount BEGIN INSERT INTO account_analysis (account_id, source_version)
                SELECT OLD.account_id, 1 WHERE OLD.account_id IS NOT NULL
                AND EXISTS (SELECT 1 FROM account WHERE id = OLD.account_id)

                ON CONFLICT(account_id) DO UPDATE SET source_version = source_version + 1,
                requested = CASE WHEN failures > 0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
                failures = 0, retry_at = 0, error = NULL; END;

CREATE TRIGGER analysis_portfolioaccount_insert AFTER INSERT ON portfolioaccount BEGIN INSERT INTO account_analysis (account_id, source_version)
                SELECT NEW.account_id, 1 WHERE NEW.account_id IS NOT NULL
                AND EXISTS (SELECT 1 FROM account WHERE id = NEW.account_id)

                ON CONFLICT(account_id) DO UPDATE SET source_version = source_version + 1,
                requested = CASE WHEN failures > 0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
                failures = 0, retry_at = 0, error = NULL; END;

CREATE TRIGGER analysis_portfolioaccount_update AFTER UPDATE ON portfolioaccount BEGIN INSERT INTO account_analysis (account_id, source_version)
                SELECT OLD.account_id, 1 WHERE OLD.account_id IS NOT NULL
                AND EXISTS (SELECT 1 FROM account WHERE id = OLD.account_id)

                ON CONFLICT(account_id) DO UPDATE SET source_version = source_version + 1,
                requested = CASE WHEN failures > 0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
                failures = 0, retry_at = 0, error = NULL; INSERT INTO account_analysis (account_id, source_version)
                SELECT NEW.account_id, 1 WHERE NEW.account_id IS NOT NULL
                AND EXISTS (SELECT 1 FROM account WHERE id = NEW.account_id)
                AND NEW.account_id IS NOT OLD.account_id
                ON CONFLICT(account_id) DO UPDATE SET source_version = source_version + 1,
                requested = CASE WHEN failures > 0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
                failures = 0, retry_at = 0, error = NULL; END;

CREATE TRIGGER analysis_schedule_skip_delete AFTER DELETE ON schedule_skip BEGIN INSERT INTO account_analysis (account_id, source_version)
                SELECT OLD.account_id, 1 WHERE OLD.account_id IS NOT NULL
                AND EXISTS (SELECT 1 FROM account WHERE id = OLD.account_id)

                ON CONFLICT(account_id) DO UPDATE SET source_version = source_version + 1,
                requested = CASE WHEN failures > 0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
                failures = 0, retry_at = 0, error = NULL; END;

CREATE TRIGGER analysis_schedule_skip_insert AFTER INSERT ON schedule_skip BEGIN INSERT INTO account_analysis (account_id, source_version)
                SELECT NEW.account_id, 1 WHERE NEW.account_id IS NOT NULL
                AND EXISTS (SELECT 1 FROM account WHERE id = NEW.account_id)

                ON CONFLICT(account_id) DO UPDATE SET source_version = source_version + 1,
                requested = CASE WHEN failures > 0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
                failures = 0, retry_at = 0, error = NULL; END;

CREATE TRIGGER analysis_schedule_skip_update AFTER UPDATE ON schedule_skip BEGIN INSERT INTO account_analysis (account_id, source_version)
                SELECT OLD.account_id, 1 WHERE OLD.account_id IS NOT NULL
                AND EXISTS (SELECT 1 FROM account WHERE id = OLD.account_id)

                ON CONFLICT(account_id) DO UPDATE SET source_version = source_version + 1,
                requested = CASE WHEN failures > 0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
                failures = 0, retry_at = 0, error = NULL; INSERT INTO account_analysis (account_id, source_version)
                SELECT NEW.account_id, 1 WHERE NEW.account_id IS NOT NULL
                AND EXISTS (SELECT 1 FROM account WHERE id = NEW.account_id)
                AND NEW.account_id IS NOT OLD.account_id
                ON CONFLICT(account_id) DO UPDATE SET source_version = source_version + 1,
                requested = CASE WHEN failures > 0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
                failures = 0, retry_at = 0, error = NULL; END;

CREATE TRIGGER analysis_target_weight_snapshot_delete AFTER DELETE ON target_weight_snapshot BEGIN INSERT INTO account_analysis (account_id, source_version)
                SELECT OLD.account_id, 1 WHERE OLD.account_id IS NOT NULL
                AND EXISTS (SELECT 1 FROM account WHERE id = OLD.account_id)

                ON CONFLICT(account_id) DO UPDATE SET source_version = source_version + 1,
                requested = CASE WHEN failures > 0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
                failures = 0, retry_at = 0, error = NULL; END;

CREATE TRIGGER analysis_target_weight_snapshot_insert AFTER INSERT ON target_weight_snapshot BEGIN INSERT INTO account_analysis (account_id, source_version)
                SELECT NEW.account_id, 1 WHERE NEW.account_id IS NOT NULL
                AND EXISTS (SELECT 1 FROM account WHERE id = NEW.account_id)

                ON CONFLICT(account_id) DO UPDATE SET source_version = source_version + 1,
                requested = CASE WHEN failures > 0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
                failures = 0, retry_at = 0, error = NULL; END;

CREATE TRIGGER analysis_target_weight_snapshot_update AFTER UPDATE ON target_weight_snapshot BEGIN INSERT INTO account_analysis (account_id, source_version)
                SELECT OLD.account_id, 1 WHERE OLD.account_id IS NOT NULL
                AND EXISTS (SELECT 1 FROM account WHERE id = OLD.account_id)

                ON CONFLICT(account_id) DO UPDATE SET source_version = source_version + 1,
                requested = CASE WHEN failures > 0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
                failures = 0, retry_at = 0, error = NULL; INSERT INTO account_analysis (account_id, source_version)
                SELECT NEW.account_id, 1 WHERE NEW.account_id IS NOT NULL
                AND EXISTS (SELECT 1 FROM account WHERE id = NEW.account_id)
                AND NEW.account_id IS NOT OLD.account_id
                ON CONFLICT(account_id) DO UPDATE SET source_version = source_version + 1,
                requested = CASE WHEN failures > 0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL
                THEN 1 ELSE requested END,
                failures = 0, retry_at = 0, error = NULL; END;
