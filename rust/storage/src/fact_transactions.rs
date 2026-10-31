//! Final symbol/side transaction rows derived from this execution's fills.

use crate::analysis::sql as sql_error;
use crate::error::AnalysisError;
use crate::fact_trades::Trade;
fn finite(v: Option<f64>) -> Option<f64> {
    v.filter(|v| v.is_finite())
}
use rusqlite::{Connection, Statement};
use std::collections::BTreeMap;

pub(crate) struct TransactionReader<'a> {
    sizing: Statement<'a>,
}

impl<'a> TransactionReader<'a> {
    pub(crate) fn new(connection: &'a Connection) -> rusqlite::Result<Self> {
        Ok(Self { sizing: connection.prepare("SELECT s.map_key,z.current_quantity,z.target_quantity FROM execution_symbol_result s LEFT JOIN execution_symbol_sizing z ON z.record_id=s.record_id AND z.path=s.path || '/sizing' WHERE s.record_id=?1 ORDER BY s.ordinal")? })
    }

    pub(crate) fn read(
        &mut self,
        record: i64,
        fills: &[Trade],
    ) -> Result<Vec<serde_json::Value>, AnalysisError> {
        let sizing: BTreeMap<String, (Option<f64>, Option<f64>)> = self
            .sizing
            .query_map([record], |row| {
                Ok((row.get(0)?, (row.get(1)?, row.get(2)?)))
            })
            .map_err(sql_error)?
            .collect::<Result<_, _>>()
            .map_err(sql_error)?;
        let mut groups: Vec<(&str, &str, Vec<&Trade>)> = Vec::new();
        for trade in fills.iter().filter(|trade| trade.quantity.is_some()) {
            if let Some(group) = groups
                .iter_mut()
                .find(|group| group.0 == trade.symbol && group.1 == trade.side)
            {
                group.2.push(trade);
            } else {
                groups.push((&trade.symbol, &trade.side, vec![trade]));
            }
        }
        let mut output = Vec::new();
        for (symbol, side, trades) in groups {
            let quantity: f64 = trades.iter().map(|trade| trade.quantity.unwrap()).sum();
            let price = trades.iter().all(|trade| trade.price.is_some()).then(|| {
                trades
                    .iter()
                    .map(|trade| trade.price.unwrap() * trade.quantity.unwrap())
                    .sum::<f64>()
                    / quantity
            });
            let reference = trades
                .iter()
                .all(|trade| trade.reference == trades[0].reference)
                .then_some(trades[0].reference)
                .flatten();
            let time = trades
                .iter()
                .map(|trade| trade.time)
                .min()
                .expect("nonempty trade group");
            let end = trades
                .iter()
                .map(|trade| trade.time)
                .max()
                .expect("nonempty trade group");
            let mut summary = crate::cost_summary::Summary::empty();
            for trade in &trades {
                summary.push(trade);
            }
            let (before, target_quantity) = sizing.get(symbol).copied().unwrap_or_default();
            output.push(serde_json::json!({"symbol":symbol,"side":side,"quantity":quantity,"price":finite(price),"reference":finite(reference),"referenceSource":trades[0].reference_source,"time":time as f64 / 1000.0,"endTime":end as f64 / 1000.0,"timeEstimated":trades.iter().any(|t|t.estimated),"before":before,"target":target_quantity,"summary":summary.output()}));
        }
        Ok(output)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn transaction_time_bounds_use_exact_microseconds_until_api_output() {
        let connection = Connection::open_in_memory().unwrap();
        connection.execute_batch("CREATE TABLE execution_symbol_result(record_id INTEGER,map_key TEXT,path TEXT,ordinal INTEGER);
            CREATE TABLE execution_symbol_sizing(record_id INTEGER,path TEXT,current_quantity REAL,target_quantity REAL);").unwrap();
        let trade = |time| Trade {
            symbol: "rb2610".into(),
            time,
            day: "1970-01-01".into(),
            estimated: false,
            side: "buy".into(),
            quantity: Some(1.0),
            price: Some(2.0),
            reference: None,
            reference_source: None,
            value: Some(2.0),
            cost: None,
            loss_bp: None,
            fee: None,
            fee_currency: None,
        };
        let rows = TransactionReader::new(&connection)
            .unwrap()
            .read(1, &[trade(1), trade(-1)])
            .unwrap();
        assert_eq!(rows[0]["time"], -0.001);
        assert_eq!(rows[0]["endTime"], 0.001);
    }
}
