CREATE TABLE performance_contribution(
        snapshot_id TEXT NOT NULL REFERENCES performance_snapshot(id) ON DELETE CASCADE,
        range_key TEXT NOT NULL,idx INTEGER NOT NULL,symbol TEXT NOT NULL,
        product TEXT NOT NULL,currency TEXT,gross_pnl REAL,fee REAL NOT NULL,
        fee_missing INTEGER NOT NULL,issues INTEGER NOT NULL,trades INTEGER NOT NULL,
        PRIMARY KEY(snapshot_id,range_key,idx,symbol));
