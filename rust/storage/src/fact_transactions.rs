//! Final symbol/side transaction rows derived from this execution's fills.

use crate::error::AnalysisError;
use crate::fact_trades::Trade;
use crate::publication::{finite, sql_error, stage_error};
use rusqlite::params;
use rusqlite::{Connection, Statement};
use std::collections::BTreeMap;

pub(crate) struct TransactionReader<'a> {
    sizing: Statement<'a>,
}

impl<'a> TransactionReader<'a> {
    pub(crate) fn new(connection: &'a Connection) -> rusqlite::Result<Self> {
        Ok(Self { sizing: connection.prepare("SELECT s.map_key,z.current_quantity,z.target_quantity FROM execution_symbol_result s LEFT JOIN execution_symbol_sizing z ON z.record_id=s.record_id AND z.path=s.path || '/sizing' WHERE s.record_id=?1 ORDER BY s.ordinal")? })
    }

    pub(crate) fn write(
        &mut self,
        record: i64,
        fills: &[Trade],
        target: &Connection,
    ) -> Result<(), AnalysisError> {
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
            let mut summary = crate::costs::Summary::empty();
            for trade in &trades {
                summary.push(trade);
            }
            let (before, target_quantity) = sizing.get(symbol).copied().unwrap_or_default();
            target
                .prepare_cached(
                    r#"INSERT INTO native_stage.performance_cost_transaction (
                record_id, idx, symbol, side, quantity, price,
                reference, reference_source, time, end_time, time_estimated, before,
                target, value, cost, loss_bp, coverage, covered,
                count, amount_complete, fee_covered
            ) VALUES (
                ?1, ?2, ?3, ?4, ?5, ?6,
                ?7, ?8, ?9, ?10, ?11, ?12,
                ?13, ?14, ?15, ?16, ?17, ?18,
                ?19, ?20, ?21
            )"#,
                )
                .map_err(stage_error)?
                .execute(params![
                    record,
                    index,
                    symbol,
                    side,
                    finite(Some(quantity)),
                    finite(price),
                    finite(reference),
                    trades[0].reference_source,
                    time,
                    end,
                    trades.iter().any(|trade| trade.estimated),
                    finite(before),
                    finite(target_quantity),
                    finite(summary.value),
                    finite(summary.cost),
                    finite(summary.loss_bp()),
                    finite(summary.coverage()),
                    summary.covered,
                    summary.count,
                    summary.amount_complete(),
                    summary.fee_count,
                ])
                .map_err(stage_error)?;
            let mut insert = target.prepare_cached(
                "INSERT INTO native_stage.performance_cost_transaction_fee(record_id,transaction_idx,currency,value) VALUES(?1,?2,?3,?4)"
            ).map_err(stage_error)?;
            for (currency, (value, _)) in summary.fees {
                insert
                    .execute(params![record, index, currency, finite(Some(value))])
                    .map_err(stage_error)?;
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Value;
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
        connection.execute_batch("ATTACH DATABASE '' AS native_stage; CREATE TABLE native_stage.performance_cost_transaction(\"record_id\" INTEGER,\"idx\" INTEGER,\"symbol\" TEXT,\"side\" TEXT,\"quantity\" FLOAT,\"price\" FLOAT,\"reference\" FLOAT,\"reference_source\" TEXT,\"time\" FLOAT,\"end_time\" FLOAT,\"time_estimated\" BOOLEAN,\"before\" FLOAT,\"target\" FLOAT,\"value\" FLOAT,\"cost\" FLOAT,\"loss_bp\" FLOAT,\"coverage\" FLOAT,\"covered\" INTEGER,\"count\" INTEGER,\"amount_complete\" BOOLEAN,\"fee_covered\" INTEGER);CREATE TABLE native_stage.performance_cost_transaction_fee(\"record_id\" INTEGER,\"transaction_idx\" INTEGER,\"currency\" TEXT,\"value\" FLOAT);").unwrap();
        TransactionReader::new(&connection)
            .unwrap()
            .write(1, &fills, &connection)
            .unwrap();
        let rows: Vec<Value> = connection.prepare("SELECT json_array(\"record_id\",\"idx\",\"symbol\",\"side\",\"quantity\",\"price\",\"reference\",\"reference_source\",\"time\",\"end_time\",json(CASE WHEN \"time_estimated\" THEN 'true' ELSE 'false' END),\"before\",\"target\",\"value\",\"cost\",\"loss_bp\",\"coverage\",\"covered\",\"count\",json(CASE WHEN \"amount_complete\" THEN 'true' ELSE 'false' END),\"fee_covered\") FROM native_stage.performance_cost_transaction ORDER BY idx").unwrap().query_map([],|row| Ok(serde_json::from_str(&row.get::<_,String>(0)?).unwrap())).unwrap().collect::<rusqlite::Result<_>>().unwrap();
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
