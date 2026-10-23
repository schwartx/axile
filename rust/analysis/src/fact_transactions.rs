//! Final symbol/side transaction rows derived from this execution's fills.

use crate::fact_trades::Trade;
use rusqlite::{Connection, Statement};
use serde_json::json;
use std::collections::BTreeMap;

const COLUMNS: &[&str] = &[
    "record_id",
    "idx",
    "symbol",
    "side",
    "quantity",
    "price",
    "reference",
    "reference_source",
    "time",
    "end_time",
    "time_estimated",
    "before",
    "target",
    "value",
    "cost",
    "loss_bp",
    "coverage",
    "covered",
    "count",
    "amount_complete",
    "fee_covered",
];

pub(crate) struct TransactionReader<'a> {
    sizing: Statement<'a>,
}

impl<'a> TransactionReader<'a> {
    pub(crate) fn new(connection: &'a Connection) -> rusqlite::Result<Self> {
        Ok(Self { sizing: connection.prepare("SELECT s.map_key,z.current_quantity,z.target_quantity FROM execution_symbol_result s LEFT JOIN execution_symbol_sizing z ON z.record_id=s.record_id AND z.path=s.path || '/sizing' WHERE s.record_id=?1 ORDER BY s.ordinal")? })
    }

    pub(crate) fn emit(
        &mut self,
        record: i64,
        fills: &[Trade],
        emit: &mut crate::publication::RowEmitter<'_>,
    ) -> Result<(), String> {
        let sizing: BTreeMap<String, (Option<f64>, Option<f64>)> = self
            .sizing
            .query_map([record], |row| {
                Ok((row.get(0)?, (row.get(1)?, row.get(2)?)))
            })
            .map_err(|error| error.to_string())?
            .collect::<Result<_, _>>()
            .map_err(|error| error.to_string())?;
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
        for (index, (symbol, side, trades)) in groups.into_iter().enumerate() {
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
            let valued: Vec<_> = trades
                .iter()
                .filter(|trade| trade.value.is_some())
                .collect();
            let covered: Vec<_> = valued.iter().filter(|trade| trade.cost.is_some()).collect();
            let value: f64 = valued.iter().filter_map(|trade| trade.value).sum();
            let cost: f64 = covered.iter().filter_map(|trade| trade.cost).sum();
            let covered_value: f64 = covered.iter().filter_map(|trade| trade.value).sum();
            let weighted: f64 = covered
                .iter()
                .map(|trade| trade.value.unwrap() * trade.loss_bp.unwrap_or(0.0))
                .sum();
            let mut fees = BTreeMap::<&str, f64>::new();
            let mut fee_count = 0;
            for trade in &trades {
                if let (Some(fee), Some(currency)) = (trade.fee, trade.fee_currency.as_deref()) {
                    *fees.entry(currency).or_default() += fee;
                    fee_count += 1;
                }
            }
            let (before, target) = sizing.get(symbol).copied().unwrap_or_default();
            crate::publication::rows(
                "performance_cost_transaction",
                COLUMNS,
                [Ok(vec![
                    json!(record),
                    json!(index),
                    json!(symbol),
                    json!(side),
                    json!(quantity),
                    json!(price),
                    json!(reference),
                    json!(trades[0].reference_source),
                    json!(time),
                    json!(end),
                    json!(trades.iter().any(|trade| trade.estimated)),
                    json!(before),
                    json!(target),
                    json!((!valued.is_empty()).then_some(value)),
                    json!((!covered.is_empty()).then_some(cost)),
                    json!((covered_value > 0.0).then(|| weighted / covered_value)),
                    json!((valued.len() == trades.len() && value > 0.0)
                        .then(|| covered_value / value)),
                    json!(covered.len()),
                    json!(trades.len()),
                    json!(valued.len() == trades.len()),
                    json!(fee_count),
                ])],
                emit,
            )?;
            crate::publication::rows(
                "performance_cost_transaction_fee",
                &["record_id", "transaction_idx", "currency", "value"],
                fees.into_iter().map(|(currency, value)| {
                    Ok(vec![
                        json!(record),
                        json!(index),
                        json!(currency),
                        json!(value),
                    ])
                }),
                emit,
            )?;
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn grouping_preserves_encounter_order_and_unknown_amounts() {
        let connection = Connection::open_in_memory().unwrap();
        connection.execute_batch("CREATE TABLE execution_symbol_result(record_id INTEGER,path TEXT,map_key TEXT,ordinal INTEGER); CREATE TABLE execution_symbol_sizing(record_id INTEGER,path TEXT,current_quantity REAL,target_quantity REAL);").unwrap();
        let trade = |side: &str, price| Trade {
            symbol: "A".into(),
            time: 100.0,
            day: "2026-01-01".into(),
            estimated: false,
            side: side.into(),
            quantity: Some(1.0),
            price,
            reference: Some(100.0),
            reference_source: Some("mid"),
            value: price,
            cost: price.map(|price| price - 100.0),
            loss_bp: price.map(|price| (price - 100.0) * 100.0),
            fee: Some(0.0),
            fee_currency: Some("USD".into()),
        };
        let fills = vec![
            trade("sell", Some(102.0)),
            trade("buy", Some(101.0)),
            trade("sell", None),
        ];
        let mut rows = Vec::new();
        TransactionReader::new(&connection)
            .unwrap()
            .emit(1, &fills, &mut |table, _, items| {
                if table == "performance_cost_transaction" {
                    rows.extend_from_slice(items);
                }
                Ok(())
            })
            .unwrap();
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0][3], "sell");
        assert_eq!(rows[1][3], "buy");
        assert!(rows[0][5].is_null());
        assert!(rows[0][16].is_null());
        assert_eq!(rows[0][18], 2);
        assert_eq!(rows[0][19], false);
        assert_eq!(rows[0][20], 2);
    }
}
