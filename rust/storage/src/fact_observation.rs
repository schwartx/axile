//! Derive one replay observation from canonical business facts.

use crate::{fact_sizing::SizingReader, fact_ticks::TickReader, replay::Observation};
use rusqlite::{params, Connection, OptionalExtension, Statement};
use std::collections::BTreeMap;

pub(crate) struct ObservationReader<'a> {
    header: Statement<'a>,
    target: Statement<'a>,
    snapshot: Statement<'a>,
    snapshot_weights: Statement<'a>,
    assets: Statement<'a>,
    ticks: TickReader<'a>,
    sizing: SizingReader<'a>,
}

impl<'a> ObservationReader<'a> {
    pub(crate) fn new(connection: &'a Connection) -> rusqlite::Result<Self> {
        Ok(Self {
            header: connection.prepare("SELECT e.execution_id,e.created_at,e.execution_kind,i.id,i.curr_target_state FROM executerecord e LEFT JOIN execution_input i ON i.id=e.input_id AND i.account_id=e.account_id WHERE e.id=?1 AND e.account_id=?2")?,
            target: connection.prepare("SELECT map_key,value FROM execution_input_target WHERE input_id=?1 AND role='curr_target' ORDER BY map_key")?,
            snapshot: connection.prepare("SELECT id,normalized_weights_kind FROM target_weight_snapshot WHERE account_id=?1 AND execution_id=?2 ORDER BY id DESC LIMIT 1")?,
            snapshot_weights: connection.prepare("SELECT map_key,value FROM target_weight_snapshot_weight WHERE snapshot_id=?1 AND role='normalized_weights' ORDER BY map_key")?,
            assets: connection.prepare("SELECT a.total_asset FROM business_asset_binding b JOIN business_asset_snapshot a ON a.asset_id=b.asset_id WHERE b.record_id=?1 AND b.path='/account_assets' AND a.observation_valid=1")?,
            ticks: TickReader::new(connection)?,
            sizing: SizingReader::new(connection)?,
        })
    }

    pub(crate) fn read(&mut self, account: i64, record: i64) -> Result<Observation, String> {
        let (execution_id, created_at, kind, input_id, target_state) = self
            .header
            .query_row(params![record, account], |row| {
                Ok((
                    row.get::<_, Option<String>>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, Option<String>>(2)?,
                    row.get::<_, Option<String>>(3)?,
                    row.get::<_, Option<String>>(4)?,
                ))
            })
            .map_err(|error| error.to_string())?;
        let mut target_weight = if target_state.as_deref() == Some("value") {
            weights(&mut self.target, input_id.as_deref().unwrap_or_default())?
        } else {
            None
        };
        if target_weight.is_none() {
            if let Some(execution) = &execution_id {
                let snapshot = self
                    .snapshot
                    .query_row(params![account, execution], |row| {
                        Ok((row.get::<_, i64>(0)?, row.get::<_, String>(1)?))
                    })
                    .optional()
                    .map_err(|error| error.to_string())?;
                if let Some((id, state)) = snapshot {
                    if state == "mapping" {
                        target_weight = weights(&mut self.snapshot_weights, id)?;
                    }
                }
            }
        }
        let sizing = self
            .sizing
            .read(record)
            .map_err(|error| error.to_string())?;
        let (mut target, mut target_units, mut target_quantities) = match sizing {
            Some(sizing) => (
                Some(sizing.weights),
                Some(sizing.units),
                Some(sizing.quantities),
            ),
            None => (target_weight.clone(), None, None),
        };
        if kind.as_deref() == Some("clear_positions") {
            target = Some(BTreeMap::new());
            target_weight = Some(BTreeMap::new());
            target_units = Some(BTreeMap::new());
            target_quantities = Some(BTreeMap::new());
        }
        let sizing_fallback = target_units.is_none()
            && target_weight
                .as_ref()
                .is_some_and(|weights| !weights.is_empty());
        let asset: Option<f64> = self
            .assets
            .query_row([record], |row| row.get(0))
            .optional()
            .map_err(|error| error.to_string())?
            .flatten();
        Ok(Observation {
            id: record,
            execution_id,
            time: crate::calendar::local_time(&created_at)?
                .format("%Y-%m-%dT%H:%M:%S%.6f")
                .to_string(),
            asset: asset.filter(|value| value.is_finite()),
            target,
            target_weight,
            target_units,
            target_quantities,
            sizing_fallback,
            prices: self
                .ticks
                .read(record)
                .map_err(|error| error.to_string())?
                .into_iter()
                .filter_map(|(symbol, prices)| prices.replay.map(|price| (symbol, price)))
                .collect(),
        })
    }
}

/// SQLite sorts and deduplicates scalars in its bounded, disk-backed temporary store.
pub(crate) fn scan(
    connection: &Connection,
    account: i64,
    start: &str,
    mut consume: impl FnMut(&Observation) -> Result<(), String>,
) -> Result<(), String> {
    let mut statement = connection
        .prepare(
            "WITH times AS MATERIALIZED (
                 SELECT id,axile_time(created_at) AS time FROM executerecord WHERE account_id=?1
             ) SELECT max(id) FROM times WHERE time>=?2 GROUP BY time ORDER BY time",
        )
        .map_err(|error| error.to_string())?;
    let mut rows = statement
        .query(params![account, start])
        .map_err(|error| error.to_string())?;
    let mut reader = ObservationReader::new(connection).map_err(|error| error.to_string())?;
    while let Some(row) = rows.next().map_err(|error| error.to_string())? {
        let id: i64 = row.get(0).map_err(|error| error.to_string())?;
        consume(&reader.read(account, id)?)?;
    }
    Ok(())
}

fn weights(
    statement: &mut Statement<'_>,
    owner: impl rusqlite::ToSql,
) -> Result<Option<BTreeMap<String, f64>>, String> {
    let mut values = BTreeMap::new();
    let mut rows = statement
        .query([owner])
        .map_err(|error| error.to_string())?;
    while let Some(row) = rows.next().map_err(|error| error.to_string())? {
        let symbol: String = row.get(0).map_err(|error| error.to_string())?;
        let value: Option<f64> = row.get(1).map_err(|error| error.to_string())?;
        let Some(value) = value.filter(|value| value.is_finite()) else {
            return Ok(None);
        };
        if symbol.is_empty() {
            return Ok(None);
        }
        values.insert(symbol, value);
    }
    Ok(Some(values))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn production_performance_reads_business_facts_without_replay_tables() {
        let connection = Connection::open_in_memory().unwrap();
        crate::calendar::register_time_function(&connection).unwrap();
        connection.execute_batch(
            "CREATE TABLE executerecord(id INTEGER,account_id INTEGER,execution_id TEXT,created_at TEXT,execution_kind TEXT,input_id TEXT);
             CREATE TABLE execution_input(id TEXT,account_id INTEGER,curr_target_state TEXT);
             CREATE TABLE execution_input_target(input_id TEXT,role TEXT,map_key TEXT,value REAL);
             CREATE TABLE target_weight_snapshot(id INTEGER,account_id INTEGER,execution_id TEXT,normalized_weights_kind TEXT);
             CREATE TABLE target_weight_snapshot_weight(snapshot_id INTEGER,role TEXT,map_key TEXT,value REAL);
             CREATE TABLE business_asset_binding(record_id INTEGER,path TEXT,asset_id TEXT);
             CREATE TABLE business_asset_snapshot(asset_id TEXT,total_asset REAL,observation_valid INTEGER);
             CREATE TABLE execution_symbol_result(record_id INTEGER,path TEXT,map_key TEXT,ordinal INTEGER);
             CREATE TABLE execution_symbol_tick(record_id INTEGER,path TEXT,map_key TEXT,ordinal INTEGER,bid_price REAL,ask_price REAL,last_price REAL,book_valid INTEGER);
             CREATE TABLE execution_symbol_sizing(record_id INTEGER,path TEXT,sizing_mode TEXT,status TEXT,target_quantity REAL,equity REAL,unit_notional REAL,reference_price REAL,unit_multiplier REAL,conversion_recorded INTEGER);
             CREATE TABLE execution_symbol_conversion(record_id INTEGER,path TEXT,rate REAL);
             INSERT INTO executerecord VALUES(1,7,'one','2026-01-01T09:00:00','clear_positions',NULL),(2,7,'two','2026-01-02T09:00:00','clear_positions',NULL);
             INSERT INTO business_asset_binding VALUES(1,'/account_assets','a'),(2,'/account_assets','b');
             INSERT INTO business_asset_snapshot VALUES('a',1000,1),('b',1100,1);"
        ).unwrap();
        let result = crate::performance::performance(
            &connection,
            &crate::performance::PerformanceRequest {
                account_id: 7,
                range: Some("all"),
                include_backtest: true,
                include_metadata: false,
                require_observation_facts: false,
                expected_source_version: crate::performance::SourceVersionCheck::Skip,
                backtest_fee_rate: 0.0,
            },
        )
        .unwrap();
        assert!(result.to_string().contains("1100"));
        let observation = ObservationReader::new(&connection)
            .unwrap()
            .read(7, 1)
            .unwrap();
        assert_eq!(observation.target, Some(BTreeMap::new()));
        assert_eq!(observation.target_units, Some(BTreeMap::new()));
        assert!(!observation.sizing_fallback);
        assert!(ObservationReader::new(&connection)
            .unwrap()
            .read(8, 1)
            .is_err());
    }
}
