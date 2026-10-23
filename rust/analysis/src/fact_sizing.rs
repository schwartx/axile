//! Frozen executable weights and units, without reconstructing input payloads.

use crate::fact_prices::Sizing;
use rusqlite::{Connection, Statement};
use std::collections::BTreeMap;

pub(crate) struct Targets {
    pub weights: BTreeMap<String, f64>,
    pub units: BTreeMap<String, f64>,
    pub quantities: BTreeMap<String, f64>,
}

pub(crate) struct SizingReader<'a> {
    statement: Statement<'a>,
}

impl<'a> SizingReader<'a> {
    pub(crate) fn new(connection: &'a Connection) -> rusqlite::Result<Self> {
        Ok(Self { statement: connection.prepare(
            "SELECT s.map_key,z.sizing_mode,z.status,z.target_quantity,z.equity,
                z.unit_notional,z.reference_price,z.unit_multiplier,z.conversion_recorded,c.rate
             FROM execution_symbol_result s
             LEFT JOIN execution_symbol_sizing z ON z.record_id=s.record_id AND z.path=s.path || '/sizing'
             LEFT JOIN execution_symbol_conversion c ON c.record_id=s.record_id AND c.path=z.path || '/value_conversion'
             WHERE s.record_id=?1 ORDER BY s.ordinal"
        )? })
    }

    pub(crate) fn read(&mut self, record: i64) -> rusqlite::Result<Option<Targets>> {
        let mut result = Targets {
            weights: BTreeMap::new(),
            units: BTreeMap::new(),
            quantities: BTreeMap::new(),
        };
        let mut rows = self.statement.query([record])?;
        while let Some(row) = rows.next()? {
            let symbol: Option<String> = row.get(0)?;
            let Some(symbol) = symbol.filter(|symbol| !symbol.is_empty()) else {
                return Ok(None);
            };
            let mode: Option<String> = row.get(1)?;
            let status: Option<String> = row.get(2)?;
            let quantity: Option<f64> = row.get(3)?;
            let sizing = Sizing {
                mode: mode.as_deref(),
                status: status.as_deref(),
                quantity,
                equity: row.get(4)?,
                notional: row.get(5)?,
                reference: row.get(6)?,
                multiplier: row.get(7)?,
                conversion: if row.get::<_, Option<bool>>(8)? == Some(true) {
                    Some(row.get(9)?)
                } else {
                    None
                },
            };
            let Some((weight, units)) = sizing.weight_and_units() else {
                return Ok(None);
            };
            result.weights.insert(symbol.clone(), weight);
            result.units.insert(symbol.clone(), units);
            result
                .quantities
                .insert(symbol, quantity.expect("validated quantity"));
        }
        Ok((!result.weights.is_empty()).then_some(result))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn missing_sizing_is_unknown_but_explicit_zero_is_flat() {
        let connection = Connection::open_in_memory().unwrap();
        connection.execute_batch(
            "CREATE TABLE execution_symbol_result(record_id INTEGER,path TEXT,map_key TEXT,ordinal INTEGER);
             CREATE TABLE execution_symbol_sizing(record_id INTEGER,path TEXT,sizing_mode TEXT,status TEXT,target_quantity REAL,equity REAL,unit_notional REAL,reference_price REAL,unit_multiplier REAL,conversion_recorded INTEGER);
             CREATE TABLE execution_symbol_conversion(record_id INTEGER,path TEXT,rate REAL);
             INSERT INTO execution_symbol_result VALUES(1,'/symbol_results/A','A',0);") .unwrap();
        let mut reader = SizingReader::new(&connection).unwrap();
        assert!(reader.read(1).unwrap().is_none());
        connection.execute("INSERT INTO execution_symbol_sizing VALUES(1,'/symbol_results/A/sizing','weight','SIZED',0,1000,NULL,NULL,NULL,0)", []).unwrap();
        let targets = reader.read(1).unwrap().unwrap();
        assert_eq!(targets.weights["A"], 0.0);
        assert_eq!(targets.units["A"], 0.0);
        assert_eq!(targets.quantities["A"], 0.0);
        connection.execute("UPDATE execution_symbol_sizing SET target_quantity=2,unit_notional=200,reference_price=10", []).unwrap();
        let targets = reader.read(1).unwrap().unwrap();
        assert_eq!(targets.weights["A"], 0.4);
        assert_eq!(targets.units["A"], 40.0);
        assert_eq!(targets.quantities["A"], 2.0);
        assert!(reader.read(2).unwrap().is_none());
    }
}
