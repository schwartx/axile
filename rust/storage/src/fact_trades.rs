//! Bounded typed trade scan; no historical result or input JSON reconstruction.

use rusqlite::{Connection, Statement};

use crate::fact_prices::positive;
use crate::fact_ticks::Prices;

#[derive(Debug, Clone)]
pub(crate) struct Trade {
    pub symbol: String,
    pub time: f64,
    pub day: String,
    pub estimated: bool,
    pub side: String,
    pub quantity: Option<f64>,
    pub price: Option<f64>,
    pub reference: Option<f64>,
    pub reference_source: Option<&'static str>,
    pub value: Option<f64>,
    pub cost: Option<f64>,
    pub loss_bp: Option<f64>,
    pub fee: Option<f64>,
    pub fee_currency: Option<String>,
}

fn timestamp(value: &str) -> Option<(f64, String)> {
    let time = crate::calendar::local_time(value).ok()?;
    Some((
        crate::calendar::timestamp_micros(time) as f64 / 1_000_000.0 * 1000.0,
        time.format("%Y-%m-%d").to_string(),
    ))
}

pub(crate) struct TradeReader<'a> {
    statement: Statement<'a>,
}

impl<'a> TradeReader<'a> {
    pub(crate) fn new(connection: &'a Connection) -> rusqlite::Result<Self> {
        Ok(Self { statement: connection.prepare(
            "SELECT s.map_key,t.trade_time,t.trade_price,t.trade_volume,z.unit_multiplier,
                COALESCE((SELECT lower(o.direction) FROM execution_order o
                    WHERE o.record_id=t.record_id AND o.path=s.path || '/orders'
                        AND o.order_id=t.order_id ORDER BY o.ordinal DESC LIMIT 1),'none'),
                CASE WHEN json_type(t.extra,'$.direction')='text' THEN json_extract(t.extra,'$.direction') END,
                CASE WHEN json_type(t.extra,'$.commission') IN ('integer','real','text')
                    THEN json_extract(t.extra,'$.commission') END,t.fee_currency
             FROM execution_symbol_result s
             JOIN business_trade_collection c ON c.record_id=s.record_id AND c.parent_path=s.path AND c.state='array'
             JOIN business_trade t ON t.record_id=c.record_id AND t.collection_path=c.path
             LEFT JOIN execution_symbol_sizing z ON z.record_id=s.record_id AND z.path=s.path || '/sizing'
             WHERE s.record_id=?1 ORDER BY s.ordinal,t.ordinal"
        )? })
    }

    pub(crate) fn read(
        &mut self,
        record: i64,
        created_at: &str,
        prices: &std::collections::BTreeMap<String, Prices>,
    ) -> Result<Vec<Trade>, String> {
        let fallback = timestamp(created_at).ok_or_else(|| {
            format!("SOURCE_FACTS_INCOMPLETE: record {record}: invalid execution time")
        })?;
        let rows = self
            .statement
            .query_map([record], |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, Option<String>>(1)?,
                    row.get::<_, Option<f64>>(2)?,
                    row.get::<_, Option<f64>>(3)?,
                    row.get::<_, Option<f64>>(4)?,
                    row.get::<_, String>(5)?,
                    row.get::<_, Option<String>>(6)?,
                    row.get::<_, rusqlite::types::Value>(7)?,
                    row.get::<_, Option<String>>(8)?,
                ))
            })
            .map_err(|error| error.to_string())?;
        rows.map(|row| {
            let (
                symbol,
                raw_time,
                price,
                quantity,
                multiplier,
                order_side,
                extra_side,
                fee,
                currency,
            ) = row.map_err(|error| error.to_string())?;
            let side = if matches!(order_side.as_str(), "buy" | "sell") {
                order_side
            } else {
                extra_side
                    .map(|side| side.to_lowercase())
                    .filter(|side| matches!(side.as_str(), "buy" | "sell"))
                    .unwrap_or_else(|| "none".into())
            };
            let price = positive(price);
            let quantity = positive(quantity);
            let multiplier = positive(multiplier);
            let reference = prices.get(&symbol).and_then(|prices| prices.cost);
            let reference_source = prices.get(&symbol).and_then(|prices| prices.cost_source);
            let difference =
                price
                    .zip(reference)
                    .filter(|_| side != "none")
                    .map(|(price, reference)| {
                        (price - reference) * if side == "buy" { 1.0 } else { -1.0 }
                    });
            let parsed_time = raw_time.as_deref().and_then(timestamp);
            let estimated = parsed_time.is_none();
            let (time, day) = parsed_time.unwrap_or_else(|| fallback.clone());
            let fee = match fee {
                rusqlite::types::Value::Integer(value) => Some(value as f64),
                rusqlite::types::Value::Real(value) => Some(value),
                rusqlite::types::Value::Text(value) => value.parse().ok(),
                _ => None,
            }
            .filter(|value: &f64| value.is_finite());
            Ok(Trade {
                symbol,
                time,
                day,
                estimated,
                side,
                quantity,
                price,
                reference,
                reference_source,
                value: price
                    .zip(quantity)
                    .zip(multiplier)
                    .map(|((price, quantity), multiplier)| price * quantity * multiplier),
                cost: difference
                    .zip(quantity)
                    .zip(multiplier)
                    .map(|((difference, quantity), multiplier)| difference * quantity * multiplier),
                loss_bp: difference
                    .zip(reference)
                    .map(|(difference, reference)| difference / reference * 1e4),
                fee,
                fee_currency: currency.filter(|value| !value.trim().is_empty()),
            })
        })
        .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn trade_timestamp_keeps_local_day_and_floating_microsecond_boundaries() {
        for value in ["2026-01-01T00:00:00.000003", "2025-12-31T16:00:00.000003Z"] {
            assert_eq!(
                timestamp(value),
                Some((
                    crate::calendar::timestamp_ms(value).unwrap(),
                    "2026-01-01".into()
                ))
            );
        }
        assert!(timestamp("invalid").is_none());
    }

    #[test]
    fn direct_trade_facts_preserve_unknown_zero_rebates_and_estimated_time() {
        let connection = Connection::open_in_memory().unwrap();
        connection.execute_batch(
            "CREATE TABLE execution_symbol_result(record_id INTEGER,path TEXT,map_key TEXT,ordinal INTEGER);
             CREATE TABLE business_trade_collection(record_id INTEGER,path TEXT,parent_path TEXT,state TEXT);
             CREATE TABLE business_trade(record_id INTEGER,collection_path TEXT,ordinal INTEGER,trade_time TEXT,trade_price REAL,trade_volume REAL,order_id TEXT,extra TEXT,fee_currency TEXT);
             CREATE TABLE execution_symbol_sizing(record_id INTEGER,path TEXT,unit_multiplier REAL);
             CREATE TABLE execution_order(record_id INTEGER,path TEXT,order_id TEXT,ordinal INTEGER,direction TEXT);
             INSERT INTO execution_symbol_result VALUES(1,'/symbol_results/A','A',0);
             INSERT INTO business_trade_collection VALUES(1,'/symbol_results/A/trades','/symbol_results/A','array');
             INSERT INTO execution_symbol_sizing VALUES(1,'/symbol_results/A/sizing',2);
             INSERT INTO execution_order VALUES(1,'/symbol_results/A/orders','1',0,'buy');
             INSERT INTO business_trade VALUES
               (1,'/symbol_results/A/trades',0,'invalid',101,3,'1','{\"commission\":0}','USD'),
               (1,'/symbol_results/A/trades',1,'2026-01-01T01:00:00Z',99,1,'2','{\"commission\":-0.5,\"direction\":\"sell\"}','USD'),
               (1,'/symbol_results/A/trades',2,NULL,100,1,'3','{\"commission\":true}',NULL);"
        ).unwrap();
        let prices = std::collections::BTreeMap::from([(
            "A".into(),
            Prices {
                cost: Some(100.0),
                cost_source: Some("mid"),
                replay: Some(103.0),
            },
        )]);
        let trades = TradeReader::new(&connection)
            .unwrap()
            .read(1, "2026-01-02T09:00:00", &prices)
            .unwrap();
        assert_eq!(trades.len(), 3);
        assert_eq!(trades[0].value, Some(606.0));
        assert_eq!(trades[0].cost, Some(6.0));
        assert_eq!(trades[0].fee, Some(0.0));
        assert!(trades[0].estimated);
        assert_eq!(trades[0].day, "2026-01-02");
        assert_eq!(trades[1].cost, Some(2.0));
        assert_eq!(trades[1].fee, Some(-0.5));
        assert!(!trades[1].estimated);
        assert_eq!(trades[2].cost, None);
        assert_eq!(trades[2].fee, None);
    }
}
