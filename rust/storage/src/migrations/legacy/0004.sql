CREATE TABLE account_asset_snapshot (
	id INTEGER NOT NULL, 
	account_id INTEGER NOT NULL, 
	assets JSON NOT NULL, 
	source TEXT NOT NULL, 
	execution_id TEXT, 
	created_at TEXT NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(account_id) REFERENCES account (id) ON DELETE CASCADE
);

CREATE INDEX ix_account_asset_snapshot_account_id_id ON account_asset_snapshot (account_id, id);

CREATE INDEX ix_account_asset_snapshot_account_created ON account_asset_snapshot (account_id, created_at);

CREATE UNIQUE INDEX ix_account_asset_snapshot_execution_id ON account_asset_snapshot (execution_id);
