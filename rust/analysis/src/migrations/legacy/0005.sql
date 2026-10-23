CREATE TABLE IF NOT EXISTS target_weight_snapshot (
	id INTEGER NOT NULL, 
	portfolio_id INTEGER NOT NULL, 
	account_id INTEGER, 
	raw_weights JSON, 
	normalized_weights JSON, 
	source TEXT NOT NULL, 
	execution_id TEXT, 
	calculated_at TEXT NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(portfolio_id) REFERENCES portfolio (id) ON DELETE CASCADE, 
	FOREIGN KEY(account_id) REFERENCES account (id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS ix_target_weight_snapshot_portfolio_id_id ON target_weight_snapshot (portfolio_id, id);
CREATE INDEX IF NOT EXISTS ix_target_weight_snapshot_account_id_id ON target_weight_snapshot (account_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS ix_target_weight_snapshot_execution_id ON target_weight_snapshot (execution_id);
