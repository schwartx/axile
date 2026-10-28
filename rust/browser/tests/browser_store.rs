use axile_browser::BrowserStore;
use rusqlite::Connection;
use serde_json::{json, Value};
use std::{
    collections::BTreeMap,
    time::{SystemTime, UNIX_EPOCH},
};

struct Fixture {
    directory: std::path::PathBuf,
    source: Value,
    records: BTreeMap<i64, Vec<Value>>,
    expected: Value,
}
impl Fixture {
    fn new() -> Self {
        let expected: Value = serde_json::from_str(include_str!(
            "../../../tests/fixtures/browser_analysis.json"
        ))
        .unwrap();
        let mut records = BTreeMap::<i64, Vec<Value>>::new();
        for page in expected["pages"].as_array().unwrap() {
            for part in page["parts"].as_array().unwrap() {
                records
                    .entry(part["record_id"].as_str().unwrap().parse().unwrap())
                    .or_default()
                    .push(part.clone());
            }
        }
        let directory = std::env::temp_dir().join(format!(
            "axile-incremental-{}-{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir(&directory).unwrap();
        Self {
            directory,
            source: expected["state"].clone(),
            records,
            expected,
        }
    }
    fn open(&self, name: &str) -> BrowserStore {
        BrowserStore::new(
            self.directory.join(name).to_str().unwrap().into(),
            1,
            "fixture-build".into(),
        )
        .unwrap()
    }
    fn state(&self, seq: i64) -> Value {
        let mut s = self.source.clone();
        s["cursor"] = json!(seq.to_string());
        s["source_version"] = json!(seq);
        s
    }
    fn sync(&self, db: &mut BrowserStore, seq: i64, ids: &[i64]) {
        let job = db.begin(self.state(seq)).unwrap();
        let parts: Vec<_> = ids
            .iter()
            .flat_map(|id| self.records[id].clone())
            .map(|mut p| {
                p["seq"] = json!(seq.to_string());
                p
            })
            .collect();
        db.page(json!({"schema_version":2,"source_id":self.source["source_id"],"from":job["cursor"],"until":seq.to_string(),"parts":parts,"cursor":{"seq":seq.to_string(),"table":22,"after":null},"complete":true})).unwrap();
        assert_eq!(job["phase"], "sync");
    }
    fn publish(&self, db: &mut BrowserStore) -> Value {
        for _ in 0..500 {
            let m = db.calculate("2026-04-02T00:00:00Z").unwrap();
            if m["status"] == "ready" {
                return m;
            }
        }
        panic!("calculation did not converge");
    }
    fn chart(&self, db: &BrowserStore, range: &str) -> Value {
        let m = db.metadata(&Value::Null).unwrap();
        clean(
            db.query(&json!({"command":"chart","snapshot_id":m["snapshot_id"],"range":range}))
                .unwrap(),
        )
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.directory);
    }
}
fn clean(mut v: Value) -> Value {
    for k in ["snapshot_id", "snapshot_version", "computed_at"] {
        v.as_object_mut().unwrap().remove(k);
    }
    v
}

#[test]
fn native_parity_and_three_persistent_tables() {
    let f = Fixture::new();
    let mut db = f.open("parity");
    f.sync(&mut db, 100, &[0, 1, 2, 3]);
    let m = f.publish(&mut db);
    for r in ["30", "90", "all"] {
        assert_eq!(
            f.chart(&db, r),
            clean(f.expected["charts"][r].clone()),
            "range {r}"
        );
    }
    let mut detail = db
        .query(&json!({"command":"execution-detail","snapshot_id":m["snapshot_id"],"record_id":1}))
        .unwrap();
    detail["snapshot_id"] = json!("native-fixture");
    assert_eq!(detail, f.expected["detail"]);
    let costs=db.query(&json!({"command":"costs","snapshot_id":m["snapshot_id"],"range":"all","dimension":"trade","limit":1,"cursor_scope":"fixture"})).unwrap();
    assert_eq!(costs["summary"], f.expected["costs"]["summary"]);
    assert_eq!(costs["data"], f.expected["costs"]["data"]);
    drop(db);
    let c = Connection::open(f.directory.join("parity-incremental.sqlite3")).unwrap();
    let names: Vec<String> = c
        .prepare("SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name")
        .unwrap()
        .query_map([], |r| r.get(0))
        .unwrap()
        .map(Result::unwrap)
        .collect();
    assert_eq!(names, vec!["facts", "results", "state"]);
}

#[test]
fn append_resume_correction_delete_and_settings_match_fresh_replay() {
    let mut f = Fixture::new();
    let mut db = f.open("append");
    f.sync(&mut db, 10, &[0, 1, 2]);
    let old = f.publish(&mut db);
    f.sync(&mut db, 20, &[3]);
    assert_eq!(
        db.query(&json!({"command":"chart","snapshot_id":old["snapshot_id"],"range":"all"}))
            .unwrap()["snapshot_id"],
        old["snapshot_id"]
    );
    db.calculate("partial").unwrap();
    drop(db);
    let mut db = f.open("append");
    let cursor = db.job().unwrap();
    assert_eq!(db.begin(f.state(20)).unwrap(), cursor);
    f.publish(&mut db);
    let mut fresh = f.open("fresh");
    f.sync(&mut fresh, 20, &[0, 1, 2, 3]);
    f.publish(&mut fresh);
    for r in ["30", "90", "all"] {
        assert_eq!(f.chart(&db, r), f.chart(&fresh, r), "append {r}");
    }
    // A historical value is replaced; the old published result remains readable until completion.
    for p in f.records.get_mut(&2).unwrap() {
        if p["table"] == "business_asset_snapshot" {
            p["rows"][0][4] = json!(75.0);
        }
    }
    f.sync(&mut db, 30, &[2]);
    f.publish(&mut db);
    let mut corrected = f.open("corrected");
    f.sync(&mut corrected, 30, &[0, 1, 2, 3]);
    f.publish(&mut corrected);
    for r in ["30", "90", "all"] {
        assert_eq!(f.chart(&db, r), f.chart(&corrected, r), "correction {r}");
    }
    f.records.insert(
        3,
        vec![json!({"seq":"40","record_id":"3","table":null,"deleted":true})],
    );
    f.sync(&mut db, 40, &[3]);
    f.publish(&mut db);
    let mut deleted = f.open("deleted");
    f.sync(&mut deleted, 40, &[0, 1, 2]);
    f.publish(&mut deleted);
    for r in ["30", "90", "all"] {
        assert_eq!(f.chart(&db, r), f.chart(&deleted, r), "delete {r}");
    }
    f.source["settings"]["backtest_fee_rate"] = json!(0.003);
    f.sync(&mut db, 50, &[]);
    f.publish(&mut db);
    let mut settings = f.open("settings");
    f.sync(&mut settings, 50, &[0, 1, 2]);
    f.publish(&mut settings);
    for r in ["30", "90", "all"] {
        assert_eq!(f.chart(&db, r), f.chart(&settings, r), "settings {r}");
    }
}

#[test]
fn partial_execution_survives_reopen_and_invalid_page_rolls_back() {
    let f = Fixture::new();
    let mut db = f.open("partial");
    let job = db.begin(f.state(10)).unwrap();
    let mut parts = f.records[&1].clone();
    for part in &mut parts {
        part["seq"] = json!("10");
    }
    let end = parts.pop().unwrap();
    db.page(json!({"schema_version":2,"source_id":f.source["source_id"],"from":job["cursor"],"until":"10","parts":parts,"cursor":{"seq":"10","table":21,"after":null},"complete":false})).unwrap();
    drop(db);
    let mut db = f.open("partial");
    let before = db.job().unwrap();
    assert!(db.page(json!({"schema_version":2,"source_id":f.source["source_id"],"from":before["cursor"],"until":"10","parts":[{"seq":"10","record_id":"1","table":"executerecord","rows":[[1]]}],"cursor":{"seq":"10","table":22,"after":null},"complete":true})).unwrap_err().contains("columns"));
    assert_eq!(db.job().unwrap(), before);
    db.page(json!({"schema_version":2,"source_id":f.source["source_id"],"from":before["cursor"],"until":"10","parts":[end],"cursor":{"seq":"10","table":22,"after":null},"complete":true})).unwrap();
    f.publish(&mut db);
    let mut fresh = f.open("whole");
    f.sync(&mut fresh, 10, &[1]);
    f.publish(&mut fresh);
    assert_eq!(f.chart(&db, "all"), f.chart(&fresh, "all"));
}

#[test]
fn same_timestamp_open_day_and_repeated_fill_resume_match_full_replay() {
    let mut f = Fixture::new();
    let mut db = f.open("days");
    f.sync(&mut db, 10, &[0, 1, 2, 3]);
    f.publish(&mut db);
    let c = Connection::open(f.directory.join("days-incremental.sqlite3")).unwrap();
    let cost_version = || {
        c.query_row("SELECT version FROM results WHERE kind='performance_result_daily_cost' AND range_key='all' AND json_extract(body,'$.day')='2026-01-01'",[],|r|r.get::<_,i64>(0)).unwrap()
    };
    let original_cost_version = cost_version();
    let mut duplicate = f.records[&3].clone();
    for part in &mut duplicate {
        part["record_id"] = json!("4");
        if let Some(rows) = part["rows"].as_array_mut() {
            for row in rows {
                if row[0] == 3 {
                    row[0] = json!(4);
                }
            }
        }
    }
    f.records.insert(4, duplicate);
    for (seq, time) in [
        (20, "2026-04-01T09:00:00.000006+08:00"),
        (30, "2026-04-01T10:00:00+08:00"),
        (40, "2026-04-02T09:00:00+08:00"),
    ] {
        for part in f.records.get_mut(&4).unwrap() {
            if part["table"] == "executerecord" {
                part["rows"][0][3] = json!(time);
                part["rows"][0][2] = json!("duplicate-fill");
            }
        }
        f.sync(&mut db, seq, &[4]);
        f.publish(&mut db);
        let mut fresh = f.open(&format!("full-{seq}"));
        f.sync(&mut fresh, seq, &[0, 1, 2, 3, 4]);
        f.publish(&mut fresh);
        for range in ["30", "90", "all"] {
            assert_eq!(
                f.chart(&db, range),
                f.chart(&fresh, range),
                "{time} {range}"
            );
        }
        assert_eq!(
            cost_version(),
            original_cost_version,
            "closed historical cost day was rewritten"
        );
    }
    let work: String = c
        .query_row("SELECT value FROM state WHERE key='range:all'", [], |r| {
            r.get(0)
        })
        .unwrap();
    let work: Value = serde_json::from_str(&work).unwrap();
    assert!(
        !work["before"]["after"].is_null(),
        "continuation must retain the closed history"
    );
}

#[test]
fn append_continues_after_the_last_execution_without_replaying_the_open_day() {
    let mut f = Fixture::new();
    for record in 4..=76 {
        let mut parts = f.records[&3].clone();
        for part in &mut parts {
            part["record_id"] = json!(record.to_string());
            if let Some(rows) = part["rows"].as_array_mut() {
                for row in rows {
                    if row[0] == 3 {
                        row[0] = json!(record);
                    }
                }
            }
            if part["table"] == "executerecord" {
                let minute = record - 4;
                part["rows"][0][3] = json!(format!(
                    "2026-04-01T{:02}:{:02}:00+08:00",
                    10 + minute / 60,
                    minute % 60
                ));
            }
        }
        f.records.insert(record, parts);
    }
    let mut db = f.open("bounded-append");
    f.sync(&mut db, 100, &(0..76).collect::<Vec<_>>());
    f.publish(&mut db);
    f.sync(&mut db, 101, &[76]);
    db.calculate("append").unwrap();
    let job = db.job().unwrap();
    assert_eq!(
        job["range_index"], 1,
        "a single append must finish one range in one bounded step"
    );
    assert!(job["work"].is_null());
    f.publish(&mut db);
    let mut fresh = f.open("bounded-append-fresh");
    f.sync(&mut fresh, 101, &(0..=76).collect::<Vec<_>>());
    f.publish(&mut fresh);
    for range in ["30", "90", "all"] {
        assert_eq!(f.chart(&db, range), f.chart(&fresh, range));
    }
}

#[test]
fn empty_account_and_source_replacement_do_not_reuse_old_results() {
    let mut f = Fixture::new();
    let mut db = f.open("identity");
    f.sync(&mut db, 10, &[0]);
    f.publish(&mut db);
    assert!(f.chart(&db, "all")["tables"]["points"]["rows"]
        .as_array()
        .unwrap()
        .is_empty());
    f.sync(&mut db, 20, &[1, 2, 3]);
    let previous = f.publish(&mut db);
    db.begin(f.state(30)).unwrap();
    f.source["source_id"] = json!("restored-database");
    let job = db.begin(f.state(1)).unwrap();
    assert_eq!(job["cursor"]["seq"], "0");
    assert!(db
        .query(&json!({"command":"chart","snapshot_id":previous["snapshot_id"],"range":"all"}))
        .is_err());
    f.sync(&mut db, 1, &[0]);
    f.publish(&mut db);
    assert!(f.chart(&db, "all")["tables"]["points"]["rows"]
        .as_array()
        .unwrap()
        .is_empty());
}

#[test]
fn index_migration_preserves_cached_results() {
    let f = Fixture::new();
    let db = f.open("migration");
    drop(db);
    let path = f.directory.join("migration-incremental.sqlite3");
    let c = Connection::open(&path).unwrap();
    c.execute_batch("DROP INDEX results_version;DROP INDEX results_trade_day;DROP INDEX results_event_time;PRAGMA user_version=1;INSERT INTO state VALUES('migration-marker','42');INSERT INTO results VALUES('performance_result_point','all','point',1,1,'2026-04-01','{}')").unwrap();
    drop(c);
    drop(f.open("migration"));
    let c = Connection::open(path).unwrap();
    assert_eq!(
        c.query_row("PRAGMA user_version", [], |r| r.get::<_, i64>(0))
            .unwrap(),
        2
    );
    assert_eq!(
        c.query_row(
            "SELECT value FROM state WHERE key='migration-marker'",
            [],
            |r| r.get::<_, String>(0)
        )
        .unwrap(),
        "42"
    );
    assert_eq!(
        c.query_row("SELECT count(*) FROM results", [], |r| r.get::<_, i64>(0))
            .unwrap(),
        1
    );
    for (query, index) in [
        ("SELECT time,key FROM results WHERE kind='source_event' AND body IS NOT NULL AND time>='2026-04-01' ORDER BY time DESC LIMIT 100", "results_event_time"),
        ("DELETE FROM results WHERE (kind,range_key,key) IN (SELECT kind,range_key,key FROM results WHERE version=2) AND version<2", "results_version"),
        ("SELECT * FROM results WHERE version=2", "results_version"),
        ("SELECT * FROM results WHERE kind='performance_cost_trade' AND json_extract(body,'$.day')='2026-04-01'", "results_trade_day"),
        ("SELECT * FROM results WHERE kind GLOB 'performance_result_*' AND range_key='all'", "sqlite_autoindex_results_1"),
    ] {
        let plan = c.prepare(&format!("EXPLAIN QUERY PLAN {query}")).unwrap().query_map([], |r| r.get::<_, String>(3)).unwrap().map(Result::unwrap).collect::<Vec<_>>().join("\n");
        assert!(plan.contains("SEARCH") && plan.contains(index), "{plan}");
    }
}
