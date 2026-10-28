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
                .fold(f64::INFINITY, f64::min);
            let end = trades
                .iter()
                .map(|trade| trade.time)
                .fold(f64::NEG_INFINITY, f64::max);
            let mut summary = crate::cost_summary::Summary::empty();
            for trade in &trades {
                summary.push(trade);
            }
            let (before, target_quantity) = sizing.get(symbol).copied().unwrap_or_default();
            output.push(serde_json::json!({"symbol":symbol,"side":side,"quantity":quantity,"price":finite(price),"reference":finite(reference),"referenceSource":trades[0].reference_source,"time":time,"endTime":end,"timeEstimated":trades.iter().any(|t|t.estimated),"before":before,"target":target_quantity,"summary":summary.output()}));
        }
        Ok(output)
    }
}
