//! Stream structured SQLite observations through independent range ledgers.
//!
//! Selection remains in SQLite, while only one observation, current holdings,
//! and daily output survive a scan. Historical JSON is never read here.

use std::collections::{BTreeMap, BTreeSet};

use chrono::{Duration, NaiveDateTime};
use rusqlite::{Connection, OptionalExtension};
#[cfg(test)]
use rusqlite::params;
use serde::Serialize;
use serde_json::{json, Value};

use crate::replay::{Observation, ReplayClose, ReplayLedger};
use crate::result_reader::Point;

const ENGINE_VERSION: &str = "equity-ledger-1";

fn database_error(error: rusqlite::Error) -> String {
    error.to_string()
}

fn parse_time(value: &str) -> Result<NaiveDateTime, String> {
    NaiveDateTime::parse_from_str(value, "%Y-%m-%dT%H:%M:%S%.f")
        .map_err(|error| format!("Invalid observation time {value:?}: {error}"))
}

fn canonical_time(value: NaiveDateTime) -> String {
    value.format("%Y-%m-%dT%H:%M:%S%.6f").to_string()
}

fn display_time(value: &str) -> Result<String, String> {
    Ok(crate::calendar::display_time(parse_time(value)?))
}

#[derive(Clone)]
struct Sample {
    time: String,
    id: i64,
    execution_id: Option<String>,
    asset: Option<f64>,
}

impl Sample {
    fn from_observation(item: &Observation) -> Self {
        Self {
            time: item.time.clone(),
            id: item.id,
            execution_id: item.execution_id.clone(),
            asset: item.asset,
        }
    }
}

struct DaySamples {
    last: Sample,
    participant: Option<Sample>,
}

#[derive(Default, Serialize)]
pub(crate) struct Skips {
    pub(crate) count: usize,
    pub(crate) missing_target: usize,
    pub(crate) missing_ticks: usize,
    pub(crate) first_time: Option<String>,
    pub(crate) last_time: Option<String>,
}

/// Tracks the same carried target as build_wbt_input, without materializing bars.
struct Participants {
    previous_symbols: BTreeSet<String>,
    used: usize,
    start: Option<String>,
    skips: Skips,
    ledger: Option<ReplayLedger>,
}

impl Participants {
    fn new(equity: Option<f64>, fee: f64, target_weight: bool) -> Result<Self, String> {
        Ok(Self {
            previous_symbols: BTreeSet::new(),
            used: 0,
            start: None,
            skips: Skips::default(),
            ledger: equity
                .map(|amount| ReplayLedger::new(amount, fee, target_weight))
                .transpose()?,
        })
    }

    fn push(&mut self, item: &Observation, target_weight: bool) -> Result<bool, String> {
        let target = if target_weight {
            item.target_weight.as_ref()
        } else {
            item.target.as_ref()
        };
        let missing = target.is_some_and(|weights| {
            self.previous_symbols
                .iter()
                .any(|symbol| !item.prices.contains_key(symbol))
                || weights
                    .iter()
                    .any(|(symbol, weight)| *weight != 0.0 && !item.prices.contains_key(symbol))
        });
        if target.is_none() || missing {
            self.skips.count += 1;
            if target.is_none() {
                self.skips.missing_target += 1;
            } else {
                self.skips.missing_ticks += 1;
            }
            self.skips
                .first_time
                .get_or_insert_with(|| item.time.clone());
            self.skips.last_time = Some(item.time.clone());
            return Ok(false);
        }
        self.previous_symbols = target
            .expect("target checked above")
            .iter()
            .filter_map(|(symbol, weight)| (*weight != 0.0).then_some(symbol.clone()))
            .collect();
        self.used += 1;
        self.start.get_or_insert_with(|| item.time.clone());
        if let Some(ledger) = self.ledger.as_mut() {
            ledger.push(item)?;
        }
        Ok(true)
    }
}

#[derive(Serialize)]
pub(crate) struct FallbackRange {
    pub(crate) start: String,
    pub(crate) end: String,
    pub(crate) count: usize,
}

#[derive(Serialize)]
pub(crate) struct Range {
    #[serde(flatten)]
    pub(crate) header: Value,
    pub(crate) points: Vec<Point>,
    pub(crate) sizing_fallback_ranges: Vec<FallbackRange>,
    pub(crate) skips: Option<Skips>,
    pub(crate) target_skips: Option<Skips>,
}

struct RangeState {
    key: &'static str,
    start: String,
    count: usize,
    invalid_assets: usize,
    end: Option<String>,
    baseline: Option<Sample>,
    days: BTreeMap<String, DaySamples>,
    executable: Participants,
    target: Participants,
    fallback_days: BTreeSet<String>,
    fallback_count: usize,
    fallback_ranges: Vec<FallbackRange>,
    in_fallback: bool,
}

struct RangeBaseline {
    key: &'static str,
    cutoff: String,
    candidate: Option<(String, f64)>,
    fallback_equity: Option<f64>,
}

impl RangeBaseline {
    fn push(&mut self, item: &Observation) {
        let Some(asset) = item.asset.filter(|asset| *asset > 0.0) else { return };
        if item.time >= self.cutoff && self.fallback_equity.is_none() {
            self.fallback_equity = Some(asset);
        }
        let eligible = item.target.as_ref().is_some_and(|target| {
            target.iter().all(|(symbol, weight)| *weight == 0.0 || item.prices.contains_key(symbol))
        });
        if eligible && (item.time <= self.cutoff || self.candidate.is_none()) {
            self.candidate = Some((item.time.clone(), asset));
        }
    }
}

impl RangeState {
    fn push(
        &mut self,
        item: &Observation,
        time: &str,
        include_backtest: bool,
    ) -> Result<(), String> {
        if time < self.start.as_str() {
            return Ok(());
        }
        self.count += 1;
        self.invalid_assets += usize::from(item.asset.is_none());
        self.end = Some(item.time.clone());
        if self.baseline.is_none() && item.asset.is_some_and(|asset| asset > 0.0) {
            self.baseline = Some(Sample::from_observation(item));
        }
        let participant = self.executable.push(item, false)?;
        if include_backtest {
            self.target.push(item, true)?;
            if participant && item.sizing_fallback {
                self.fallback_count += 1;
                self.fallback_days.insert(item.time[..10].to_owned());
                if self.in_fallback {
                    let current = self
                        .fallback_ranges
                        .last_mut()
                        .expect("open fallback range");
                    current.end = item.time.clone();
                    current.count += 1;
                } else {
                    self.fallback_ranges.push(FallbackRange {
                        start: item.time.clone(),
                        end: item.time.clone(),
                        count: 1,
                    });
                }
                self.in_fallback = true;
            } else if participant {
                // Skipped observations do not split participating fallback ranges.
                self.in_fallback = false;
            }
        }
        if self.baseline.is_some() {
            let sample = Sample::from_observation(item);
            let day = item.time[..10].to_owned();
            let samples = self.days.entry(day).or_insert_with(|| DaySamples {
                last: sample.clone(),
                participant: None,
            });
            samples.last = sample.clone();
            if participant {
                samples.participant = Some(sample);
            }
        }
        Ok(())
    }

    fn finish(self, settings: &Value, record_count: i64, include_backtest: bool) -> Range {
        let executable_start = self.executable.start.clone();
        let target_start = self.target.start.clone();
        let executable_daily = self.executable.ledger.map(ReplayLedger::finish);
        let target_daily = self.target.ledger.map(ReplayLedger::finish);
        let points = merge_daily(
            self.baseline.as_ref(),
            &self.days,
            executable_daily.as_ref(),
            executable_start.as_deref(),
            target_daily.as_ref(),
            target_start.as_deref(),
            &self.fallback_days,
        );
        let baseline = points.first().map(|point| point.date.clone());
        let header = json!({
            "settings": settings,
            "backtest_included": include_backtest,
            "engine_version": ENGINE_VERSION,
            "range": self.key,
            "baseline": baseline,
            "end": self.end,
            "record_count": record_count,
            "observation_count": self.count,
            "used_record_count": if include_backtest { self.executable.used } else { 0 },
            "target_used_record_count": if include_backtest { self.target.used } else { 0 },
            "invalid_asset_count": self.invalid_assets,
            "sizing_fallback_count": if include_backtest && self.count > 0 { Some(self.fallback_count) } else { None },
            "bindings": [],
            "executions": [],
            "calendar": {"status":"unavailable", "calendar_id":null, "label":null,
                "closed_ranges":[], "unavailable_ranges":[]}
        });
        Range {
            header, points, sizing_fallback_ranges: self.fallback_ranges,
            skips: (include_backtest && self.count > 0).then_some(self.executable.skips),
            target_skips: (include_backtest && self.count > 0).then_some(self.target.skips),
        }
    }
}

fn replay_close(
    daily: Option<&BTreeMap<String, ReplayClose>>,
    day: &str,
    start: Option<&str>,
    previous: f64,
) -> Option<ReplayClose> {
    let (daily, start) = (daily?, start?);
    if day < &start[..10] {
        return None;
    }
    Some(*daily.get(day).unwrap_or(&ReplayClose {
        cumulative_return: previous,
        daily_return: Some(0.0),
    }))
}

#[allow(clippy::too_many_arguments)]
fn merge_daily(
    baseline: Option<&Sample>,
    days: &BTreeMap<String, DaySamples>,
    daily: Option<&BTreeMap<String, ReplayClose>>,
    start: Option<&str>,
    target_daily: Option<&BTreeMap<String, ReplayClose>>,
    target_start: Option<&str>,
    fallback_days: &BTreeSet<String>,
) -> Vec<Point> {
    let Some(base) = baseline else {
        return Vec::new();
    };
    let base_asset = base.asset.expect("positive baseline");
    let from_backtest = daily.is_some() && start.is_some_and(|when| when <= base.time.as_str());
    let from_target =
        target_daily.is_some() && target_start.is_some_and(|when| when <= base.time.as_str());
    let mut points = vec![Point {
        date: base.time.clone(), observed_at: base.time.clone(), record_id: Some(base.id),
        execution_id: base.execution_id.clone(), account_return: Some(0.0), account_equity: Some(base_asset),
        portfolio_return: from_backtest.then_some(0.0), account_daily_return: None, portfolio_daily_return: None,
        target_portfolio_return: from_target.then_some(0.0), target_portfolio_daily_return: None,
        sizing_fallback: false, difference: from_backtest.then_some(0.0),
    }];
    let mut previous_asset = Some(base_asset);
    let (mut portfolio, mut target_portfolio) = (0.0, 0.0);
    for (day, samples) in days {
        let last = samples.participant.as_ref().unwrap_or(&samples.last);
        let account = last.asset.map(|amount| amount / base_asset - 1.0);
        let account_daily = last.asset.and_then(|amount| {
            previous_asset
                .filter(|previous| *previous > 0.0)
                .map(|previous| amount / previous - 1.0)
        });
        previous_asset = last.asset;
        let close = replay_close(daily, day, start, portfolio);
        let target_close = replay_close(target_daily, day, target_start, target_portfolio);
        if let Some(close) = close {
            portfolio = close.cumulative_return;
        }
        if let Some(close) = target_close {
            target_portfolio = close.cumulative_return;
        }
        points.push(Point {
            date: day.clone(), observed_at: last.time.clone(), record_id: Some(last.id),
            execution_id: last.execution_id.clone(), account_return: account, account_equity: last.asset,
            portfolio_return: close.map(|value| value.cumulative_return), account_daily_return: account_daily,
            portfolio_daily_return: close.and_then(|value| value.daily_return),
            target_portfolio_return: target_close.map(|value| value.cumulative_return),
            target_portfolio_daily_return: target_close.and_then(|value| value.daily_return),
            sizing_fallback: fallback_days.contains(day),
            difference: account.and_then(|account| close.map(|value| account-value.cumulative_return)),
        });
    }
    points
}

/// Group a joined cursor into one owned observation, releasing it after use.
fn scan(
    connection: &Connection,
    account_id: i64,
    start: &str,
    mut consume: impl FnMut(&Observation) -> Result<(), String>,
) -> Result<(), String> {
    crate::fact_observation::scan(connection, account_id, start, |mut item| {
        item.time = display_time(&item.time)?;
        consume(&item)
    })
}

fn check_source_version(
    connection: &Connection,
    account_id: i64,
    expected: SourceVersionCheck,
) -> Result<(), String> {
    let SourceVersionCheck::Version(expected) = expected else {
        return Ok(());
    };
    let actual: Option<i64> = connection
        .query_row(
            "SELECT source_version FROM account_analysis WHERE account_id=?1",
            [account_id],
            |row| row.get(0),
        )
        .optional()
        .map_err(database_error)?;
    if actual != expected {
        return Err(
            "SOURCE_VERSION_CHANGED: analysis source changed before the read snapshot".to_owned(),
        );
    }
    Ok(())
}

/// Verify the replay snapshot is complete without reading raw JSON payloads.
fn check_observation_facts(connection: &Connection, account_id: i64) -> Result<(), String> {
    crate::fact_integrity::validate(connection, account_id)
}

/// 读路径性能计算的领域入参:账户、可选区间与回测语义。
pub struct PerformanceRequest {
    pub account_id: i64,
    pub range: Option<&'static str>,
    pub include_backtest: bool,
    pub include_metadata: bool,
    pub require_observation_facts: bool,
    pub expected_source_version: SourceVersionCheck,
    pub backtest_fee_rate: f64,
}

/// 源版本守卫的三态:跳过、要求行不存在、要求版本相等。
#[derive(Clone, Copy)]
pub enum SourceVersionCheck {
    Skip,
    Version(Option<i64>),
}

pub fn performance(connection: &Connection, request: &PerformanceRequest) -> Result<Value, String> {
    let mut ranges = calculate(connection, request)?;
    if request.range.is_some() {
        return serde_json::to_value(ranges.pop()).map_err(|error| error.to_string());
    }
    ranges.into_iter().map(|range| {
        Ok((range.header["range"].as_str().expect("calculated range").to_owned(),
            serde_json::to_value(range).map_err(|error| error.to_string())?))
    }).collect::<Result<serde_json::Map<_, _>, String>>().map(Value::Object)
}

pub(crate) fn calculate(connection: &Connection, request: &PerformanceRequest) -> Result<Vec<Range>, String> {
    let account_id = request.account_id;
    check_source_version(connection, account_id, request.expected_source_version)?;
    if request.require_observation_facts {
        check_observation_facts(connection, account_id)?;
    }
    let include_metadata = request.include_metadata;
    let fee = request.backtest_fee_rate;
    if !fee.is_finite() || !(0.0..1.0).contains(&fee) {
        return Err("backtest_fee_rate must be finite and in [0,1)".to_owned());
    }
    let settings = json!({"backtest_weight_type":"cs", "backtest_fee_rate":fee});
    let include_backtest = request.include_backtest;
    let record_count: i64 = connection
        .query_row(
            "SELECT count(*) FROM executerecord WHERE account_id=?1 AND history_visible=1 AND result_staged=0",
            [account_id],
            |row| row.get(0),
        )
        .map_err(database_error)?;
    let (first, last): (Option<String>, Option<String>) = connection.query_row(
        "SELECT min(axile_time(created_at)),max(axile_time(created_at)) FROM executerecord WHERE account_id=?1 AND history_visible=1 AND result_staged=0",
        [account_id], |row| Ok((row.get(0)?,row.get(1)?)),
    ).map_err(database_error)?;
    let range_keys: &[&str] = match request.range {
        None => &["30", "90", "all"],
        Some("30") => &["30"],
        Some("90") => &["90"],
        Some("all") => &["all"],
        Some(_) => return Err("range must be 30, 90 or all".to_owned()),
    };
    let mut baselines = Vec::new();
    for &key in range_keys {
        let cutoff = if let (Some(first), Some(last)) = (&first, &last) {
            if key == "all" {
                first.clone()
            } else {
                canonical_time(
                    parse_time(last)? - Duration::days(key.parse::<i64>().expect("range days")),
                )
            }
        } else {
            String::new()
        };
        baselines.push(RangeBaseline {key,cutoff,candidate:None,fallback_equity:None});
    }
    crate::fact_observation::scan(connection, account_id, "", |item| {
        for baseline in &mut baselines { baseline.push(&item); }
        Ok(())
    })?;
    let mut states = Vec::new();
    for baseline in baselines {
        let (start, initial_equity) = match baseline.candidate {
            Some((time, asset)) => (time, Some(asset)),
            None => (baseline.cutoff, baseline.fallback_equity),
        };
        let executable = Participants::new(
            if include_backtest {
                initial_equity
            } else {
                None
            },
            fee,
            false,
        )?;
        let target = Participants::new(
            if include_backtest {
                initial_equity
            } else {
                None
            },
            fee,
            true,
        )?;
        states.push(RangeState {
            key: baseline.key,
            start,
            count: 0,
            invalid_assets: 0,
            end: None,
            baseline: None,
            days: BTreeMap::new(),
            executable,
            target,
            fallback_days: BTreeSet::new(),
            fallback_count: 0,
            fallback_ranges: Vec::new(),
            in_fallback: false,
        });
    }
    if let Some(start) = states.iter().map(|state| state.start.clone()).min() {
        scan(connection, account_id, &start, |item| {
            let time = canonical_time(parse_time(&item.time)?);
            for state in &mut states {
                state.push(item, &time, include_backtest)?;
            }
            Ok(())
        })?;
    }
    states.into_iter().map(|state| {
        let mut range = state.finish(&settings, record_count, include_backtest);
        if include_metadata {
            crate::calendar::enrich(connection, account_id, &mut range.header)?;
        }
        Ok(range)
    }).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture() -> Connection {
        let connection = Connection::open_in_memory().unwrap();
        crate::calendar::register_time_function(&connection).unwrap();
        connection.execute_batch(
            "CREATE TABLE executerecord(id INTEGER PRIMARY KEY,account_id INTEGER,execution_id TEXT,created_at TEXT,execution_kind TEXT,input_id TEXT,result_input_id TEXT,facts_complete INTEGER,history_visible INTEGER DEFAULT 1,result_staged INTEGER DEFAULT 0);
             CREATE TABLE execution_input(id TEXT,account_id INTEGER,curr_target_state TEXT);
             CREATE TABLE execution_input_target(input_id TEXT,role TEXT,map_key TEXT,value REAL);
             CREATE TABLE target_weight_snapshot(id INTEGER,account_id INTEGER,execution_id TEXT,normalized_weights_kind TEXT);
             CREATE TABLE target_weight_snapshot_weight(snapshot_id INTEGER,role TEXT,map_key TEXT,value REAL);
             CREATE TABLE business_asset_binding(record_id INTEGER,path TEXT,asset_id TEXT);
             CREATE TABLE business_asset_snapshot(asset_id TEXT,total_asset REAL,observation_valid INTEGER);
             CREATE TABLE execution_symbol_result(record_id INTEGER,path TEXT,map_key TEXT,ordinal INTEGER,tick_recorded INTEGER,sizing_recorded INTEGER);
             CREATE TABLE execution_symbol_tick(record_id INTEGER,path TEXT,map_key TEXT,ordinal INTEGER,bid_price REAL,ask_price REAL,last_price REAL,book_valid INTEGER);
             CREATE TABLE execution_symbol_sizing(record_id INTEGER,path TEXT,sizing_mode TEXT,status TEXT,target_quantity REAL,equity REAL,unit_notional REAL,reference_price REAL,unit_multiplier REAL,conversion_recorded INTEGER);
             CREATE TABLE execution_symbol_conversion(record_id INTEGER,path TEXT,rate REAL);
             CREATE TABLE execution_symbol_collection(record_id INTEGER,path TEXT,node_kind TEXT,member_count INTEGER);
             CREATE TABLE business_trade_collection(record_id INTEGER,path TEXT,state TEXT,member_count INTEGER);
             CREATE TABLE business_trade(record_id INTEGER,collection_path TEXT);",
        ).unwrap();
        connection
    }

    fn insert(
        connection: &Connection,
        id: i64,
        time: &str,
        asset: Option<f64>,
        target: bool,
        price: Option<f64>,
    ) {
        connection
            .execute(
                "INSERT INTO executerecord(id,account_id,execution_id,created_at,execution_kind,input_id,result_input_id,facts_complete) VALUES(?1,1,?2,?3,NULL,?4,NULL,1)",
                params![id, format!("execution-{id}"), time, id.to_string()],
            )
            .unwrap();
        connection
            .execute(
                "INSERT INTO execution_input VALUES(?1,1,?2)",
                params![id.to_string(), if target { "value" } else { "absent" }],
            )
            .unwrap();
        if target {
            connection
                .execute(
                    "INSERT INTO execution_input_target VALUES(?1,'curr_target','A',1)",
                    [id.to_string()],
                )
                .unwrap();
        }
        connection
            .execute(
                "INSERT INTO business_asset_binding VALUES(?1,'/account_assets',?2)",
                params![id, id.to_string()],
            )
            .unwrap();
        connection
            .execute(
                "INSERT INTO business_asset_snapshot VALUES(?1,?2,1)",
                params![id.to_string(), asset],
            )
            .unwrap();
        if price.is_some() {
            connection.execute("INSERT INTO execution_symbol_tick VALUES(?1,'/first_ticks/A','A',0,NULL,NULL,?2,1)",params![id,price]).unwrap();
        }
    }

    fn calculate(connection: &Connection, include: bool) -> Value {
        performance(
            connection,
            &PerformanceRequest {
                account_id: 1,
                range: None,
                include_backtest: include,
                include_metadata: false,
                require_observation_facts: false,
                expected_source_version: SourceVersionCheck::Skip,
                backtest_fee_rate: 0.0,
            },
        )
        .unwrap()
    }

    #[test]
    fn single_requested_range_matches_batched_result() {
        let connection = fixture();
        insert(
            &connection,
            1,
            "2026-01-01T10:00:00",
            Some(100.0),
            true,
            Some(10.0),
        );
        insert(
            &connection,
            2,
            "2026-02-15T10:00:00",
            Some(120.0),
            true,
            Some(12.0),
        );
        insert(
            &connection,
            3,
            "2026-02-15T10:00:00.000003",
            Some(121.0),
            true,
            Some(12.1),
        );
        for include_backtest in [false, true] {
            let batched = calculate(&connection, include_backtest);
            for key in ["30", "90", "all"] {
                let single = performance(
                    &connection,
                    &PerformanceRequest {
                        account_id: 1,
                        range: Some(key),
                        include_backtest,
                        include_metadata: false,
                        require_observation_facts: false,
                        expected_source_version: SourceVersionCheck::Skip,
                        backtest_fee_rate: 0.0,
                    },
                )
                .unwrap();
                assert_eq!(single, batched[key]);
            }
        }
        let invalid = performance(
            &connection,
            &PerformanceRequest {
                account_id: 1,
                range: Some("7"),
                include_backtest: true,
                include_metadata: false,
                require_observation_facts: false,
                expected_source_version: SourceVersionCheck::Skip,
                backtest_fee_rate: 0.0,
            },
        );
        assert!(invalid.is_err());
    }

    #[test]
    fn expected_source_version_preserves_missing_row_and_rejects_changes() {
        let connection = fixture();
        connection.execute_batch(
            "CREATE TABLE account_analysis (account_id INTEGER PRIMARY KEY,source_version INTEGER NOT NULL);"
        ).unwrap();
        let mut request = PerformanceRequest {
            account_id: 1,
            range: None,
            include_backtest: true,
            include_metadata: false,
            require_observation_facts: false,
            expected_source_version: SourceVersionCheck::Version(None),
            backtest_fee_rate: 0.0,
        };
        assert!(performance(&connection, &request).is_ok());
        connection
            .execute("INSERT INTO account_analysis VALUES (1,4)", [])
            .unwrap();
        assert!(performance(&connection, &request)
            .unwrap_err()
            .starts_with("SOURCE_VERSION_CHANGED:"));
        request.expected_source_version = SourceVersionCheck::Version(Some(4));
        assert!(performance(&connection, &request).is_ok());
        request.expected_source_version = SourceVersionCheck::Version(Some(3));
        assert!(performance(&connection, &request)
            .unwrap_err()
            .starts_with("SOURCE_VERSION_CHANGED:"));
    }

    #[test]
    fn observation_readiness_rejects_missing_stale_and_orphaned_rows() {
        let connection = fixture();
        insert(
            &connection,
            1,
            "2026-01-01T09:00:00",
            Some(100.0),
            true,
            Some(10.0),
        );
        let request = &PerformanceRequest {
            account_id: 1,
            range: None,
            include_backtest: true,
            include_metadata: false,
            require_observation_facts: true,
            expected_source_version: SourceVersionCheck::Skip,
            backtest_fee_rate: 0.0,
        };
        assert!(performance(&connection, &request).is_ok());
        connection
            .execute("UPDATE executerecord SET facts_complete=0", [])
            .unwrap();
        assert!(performance(&connection, request)
            .unwrap_err()
            .starts_with("SOURCE_FACTS_INCOMPLETE:"));
        connection
            .execute("UPDATE executerecord SET facts_complete=1", [])
            .unwrap();
        connection
            .execute("UPDATE execution_input SET account_id=2", [])
            .unwrap();
        assert!(performance(&connection, request)
            .unwrap_err()
            .starts_with("SOURCE_FACTS_INCOMPLETE:"));
        connection
            .execute("UPDATE execution_input SET account_id=1", [])
            .unwrap();
        connection
            .execute(
                "INSERT INTO execution_symbol_collection VALUES(1,'/symbol_results','object',1)",
                [],
            )
            .unwrap();
        assert!(performance(&connection, request)
            .unwrap_err()
            .starts_with("SOURCE_FACTS_INCOMPLETE:"));
    }

    #[test]
    fn metadata_flag_enriches_bindings_and_channel_calendar() {
        let connection = fixture();
        insert(
            &connection,
            1,
            "2026-01-01T09:00:00",
            Some(100.0),
            true,
            Some(10.0),
        );
        connection.execute_batch(
            "CREATE TABLE account (id INTEGER PRIMARY KEY,trade_channel TEXT NOT NULL);
             INSERT INTO account VALUES (1,'ctp');
             CREATE TABLE portfolioaccount (id INTEGER PRIMARY KEY,account_id INTEGER,portfolio_id INTEGER,created_at TEXT);
             INSERT INTO portfolioaccount VALUES (1,1,3,'2026-01-01T01:00:00Z');
             CREATE TABLE analysis_calendar_day (channel TEXT,day TEXT,status TEXT,calendar_id TEXT,label TEXT,PRIMARY KEY(channel,day));
             INSERT INTO analysis_calendar_day VALUES ('ctp','2026-01-01','available_closed','china','中国');"
        ).unwrap();
        let request = &PerformanceRequest {
            account_id: 1,
            range: Some("all"),
            include_backtest: true,
            include_metadata: true,
            require_observation_facts: false,
            expected_source_version: SourceVersionCheck::Skip,
            backtest_fee_rate: 0.0,
        };
        let actual = performance(&connection, &request).unwrap();
        assert_eq!(actual["calendar"]["status"], "available");
        assert_eq!(
            actual["calendar"]["closed_ranges"],
            json!([{"start":"2026-01-01","end":"2026-01-01"}])
        );
        assert_eq!(
            actual["bindings"],
            json!([{"time":"2026-01-01T09:00:00","portfolio_id":3}])
        );
        assert_eq!(
            calculate(&connection, true)["all"]["calendar"]["status"],
            "unavailable"
        );
    }

    #[test]
    fn empty_range_preserves_optional_defaults() {
        let connection = fixture();
        let result = calculate(&connection, true);
        for key in ["30", "90", "all"] {
            assert_eq!(result[key]["record_count"], 0);
            assert!(result[key]["baseline"].is_null());
            assert!(result[key]["skips"].is_null());
            assert!(result[key]["sizing_fallback_count"].is_null());
            assert_eq!(result[key]["points"], json!([]));
        }
    }

    #[test]
    fn duplicate_timestamp_replaces_previous_before_selecting_baseline() {
        let connection = fixture();
        insert(
            &connection,
            1,
            "2026-01-01T10:00:00",
            Some(100.0),
            true,
            Some(10.0),
        );
        insert(
            &connection,
            2,
            "2026-01-01T10:00:00",
            None,
            true,
            Some(10.0),
        );
        insert(
            &connection,
            3,
            "2026-01-02T10:00:00",
            Some(110.0),
            true,
            Some(11.0),
        );
        let result = calculate(&connection, true);
        assert_eq!(result["all"]["record_count"], 3);
        assert_eq!(result["all"]["observation_count"], 1);
        assert_eq!(result["all"]["baseline"], "2026-01-02T10:00:00");
        assert_eq!(result["all"]["points"][0]["record_id"], 3);
    }

    #[test]
    fn canonical_time_deduplicates_aliases_and_keeps_the_last_record() {
        let connection = fixture();
        insert(&connection, 1, "2026-01-01T01:00:00.000006Z", Some(100.0), true, Some(10.0));
        insert(&connection, 2, "2026-01-01 09:00:00.000006+08:00", Some(110.0), true, Some(11.0));
        insert(&connection, 3, "2026-01-01T09:00:00.000007", Some(120.0), true, Some(12.0));
        let result = calculate(&connection, true);
        assert_eq!(result["all"]["record_count"], 3);
        assert_eq!(result["all"]["observation_count"], 2);
        assert_eq!(result["all"]["points"][0]["record_id"], 2);
        assert_eq!(result["all"]["points"][0]["account_equity"], 110.0);
        assert_eq!(result["all"]["points"][1]["record_id"], 3);
    }

    #[test]
    fn recent_range_rebases_at_last_valid_baseline_before_cutoff() {
        let connection = fixture();
        insert(
            &connection,
            1,
            "2026-01-01T10:00:00",
            Some(100.0),
            true,
            Some(10.0),
        );
        insert(
            &connection,
            2,
            "2026-01-10T10:00:00",
            Some(120.0),
            true,
            Some(12.0),
        );
        insert(
            &connection,
            3,
            "2026-01-15T10:00:00",
            Some(125.0),
            true,
            None,
        );
        insert(
            &connection,
            4,
            "2026-02-15T10:00:00",
            Some(130.0),
            true,
            Some(13.0),
        );
        let result = calculate(&connection, true);
        assert_eq!(result["30"]["baseline"], "2026-01-10T10:00:00");
        assert_eq!(result["30"]["observation_count"], 3);
        assert_eq!(result["30"]["skips"]["missing_ticks"], 1);
        assert_eq!(result["all"]["baseline"], "2026-01-01T10:00:00");
    }

    #[test]
    fn participating_sample_wins_over_later_intraday_heartbeat() {
        let connection = fixture();
        insert(
            &connection,
            1,
            "2026-01-01T10:00:00",
            Some(100.0),
            true,
            Some(10.0),
        );
        insert(
            &connection,
            2,
            "2026-01-02T10:00:00",
            Some(110.0),
            true,
            Some(11.0),
        );
        insert(
            &connection,
            3,
            "2026-01-02T18:00:00",
            Some(150.0),
            false,
            None,
        );
        let full = calculate(&connection, true);
        let quick = calculate(&connection, false);
        let last = full["all"]["points"].as_array().unwrap().last().unwrap();
        assert_eq!(last["record_id"], 2);
        assert_eq!(last["account_equity"], 110.0);
        assert_eq!(full["all"]["end"], "2026-01-02T18:00:00");
        assert_eq!(quick["all"]["points"][2]["record_id"], 2);
        assert!(quick["all"]["points"][2]["portfolio_return"].is_null());
        assert!(quick["all"]["skips"].is_null());
    }

    #[test]
    fn missing_asset_does_not_fabricate_daily_equity() {
        let connection = fixture();
        insert(
            &connection,
            1,
            "2026-01-01T10:00:00",
            Some(100.0),
            true,
            Some(10.0),
        );
        insert(
            &connection,
            2,
            "2026-01-02T10:00:00",
            None,
            true,
            Some(11.0),
        );
        insert(
            &connection,
            3,
            "2026-01-03T10:00:00",
            Some(120.0),
            true,
            Some(12.0),
        );
        let result = calculate(&connection, true);
        assert!(result["all"]["points"][2]["account_return"].is_null());
        assert!(result["all"]["points"][3]["account_daily_return"].is_null());
        assert_eq!(result["all"]["invalid_asset_count"], 1);
        let cumulative = result["all"]["points"][3]["account_return"]
            .as_f64()
            .unwrap();
        assert!((cumulative - 0.2).abs() < 1e-12);
    }

    #[test]
    fn no_common_baseline_still_displays_account_curve() {
        let connection = fixture();
        insert(
            &connection,
            1,
            "2026-01-01T10:00:00.000001",
            Some(100.0),
            false,
            None,
        );
        insert(
            &connection,
            2,
            "2026-01-02T10:00:00",
            Some(110.0),
            false,
            None,
        );
        let result = calculate(&connection, true);
        assert_eq!(result["all"]["observation_count"], 2);
        assert_eq!(result["all"]["baseline"], "2026-01-01T10:00:00.000001");
        assert_eq!(result["all"]["skips"]["missing_target"], 2);
        assert_eq!(result["all"]["points"][2]["account_equity"], 110.0);
        assert!(result["all"]["points"][2]["portfolio_return"].is_null());
    }
}
