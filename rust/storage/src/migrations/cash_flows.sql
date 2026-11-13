CREATE TABLE account_cash_flow (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    direction TEXT NOT NULL CHECK(direction IN ('deposit','withdraw')),
    amount REAL NOT NULL CHECK(amount > 0 AND amount < 1e100),
    occurred_at_us INTEGER NOT NULL,
    source TEXT NOT NULL CHECK(source IN ('manual','channel')),
    external_id TEXT,
    status TEXT NOT NULL CHECK(status IN ('active','pending','void')),
    note TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 1,
    created_at_us INTEGER NOT NULL,
    updated_at_us INTEGER NOT NULL,
    UNIQUE(account_id,external_id),
    CHECK((source='manual' AND external_id IS NULL) OR (source='channel' AND length(external_id)>0))
);
CREATE INDEX ix_account_cash_flow_time ON account_cash_flow(account_id,occurred_at_us);
CREATE TABLE account_cash_flow_revision (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cash_flow_id INTEGER NOT NULL REFERENCES account_cash_flow(id) ON DELETE CASCADE,
    before_json TEXT NOT NULL,
    changed_at_us INTEGER NOT NULL
);
