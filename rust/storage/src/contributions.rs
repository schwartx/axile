//! Frozen cash-flow P&L at the same observations as the account curve.
use crate::error::AnalysisError;
use crate::publication::{finite, sql_error, stage_error};
use rusqlite::{params, Connection, OptionalExtension};
use serde_json::Value;
use std::collections::BTreeMap;

#[derive(Default, Clone)]
struct Position {
    quantity: f64,
    value: Option<f64>,
    product: String,
}
#[derive(Default)]
struct Ledger {
    initial: Option<f64>,
    quantity: f64,
    cash: f64,
    fee: f64,
    fee_missing: i64,
    issues: i64,
    trades: i64,
    product: String,
}

fn product(channel: &str, symbol: &str, extra: &Value) -> String {
    if !matches!(channel, "tq" | "ctp") {
        return symbol.into();
    }
    let native = extra
        .get("tq_symbol")
        .and_then(Value::as_str)
        .unwrap_or(symbol);
    let (exchange, contract) = native.split_once('.').unwrap_or(("", native));
    let exchange = if exchange.is_empty() {
        extra
            .pointer("/position_attributes/exchange_id")
            .or_else(|| extra.get("exchange_id"))
            .and_then(Value::as_str)
            .unwrap_or("")
    } else {
        exchange
    };
    let prefix = contract.trim_end_matches(|c: char| c.is_ascii_digit());
    let suffix = &contract[prefix.len()..];
    if !exchange.is_empty()
        && prefix.chars().all(|c| c.is_ascii_alphabetic())
        && !prefix.is_empty()
        && matches!(suffix.len(), 3 | 4)
    {
        format!("{exchange}.{prefix}")
    } else {
        symbol.into()
    }
}

fn positions(
    connection: &Connection,
    record: i64,
    channel: &str,
) -> Result<(Option<String>, Option<BTreeMap<String, Position>>), AnalysisError> {
    let root = connection.query_row(
        "SELECT a.asset_id,a.currency,a.positions_kind,a.positions_count,a.observation_valid,a.source
         FROM business_asset_binding b JOIN business_asset_snapshot a ON a.asset_id=b.asset_id
         WHERE b.record_id=?1 AND b.path='/account_assets'", [record], |r| Ok((
            r.get::<_,String>(0)?,r.get::<_,Option<String>>(1)?,r.get::<_,String>(2)?,
            r.get::<_,Option<usize>>(3)?,r.get::<_,bool>(4)?,r.get::<_,Option<String>>(5)?)))
        .optional().map_err(sql_error)?;
    let Some((asset, currency, kind, count, valid, source)) = root else {
        return Ok((None, None));
    };
    if !valid
        || kind != "array"
        || matches!(source.as_deref(), Some("assumed" | "error" | "unavailable"))
    {
        return Ok((currency, None));
    }
    let mut statement=connection.prepare("SELECT symbol,volume,direction,market_value,extra,volume_state,market_value_state FROM business_asset_position WHERE asset_id=?1 ORDER BY ordinal").map_err(sql_error)?;
    let mut rows = statement.query([asset]).map_err(sql_error)?;
    let mut output = BTreeMap::<String, Position>::new();
    let mut seen = 0;
    while let Some(row) = rows.next().map_err(sql_error)? {
        seen += 1;
        let symbol: Option<String> = row.get(0).map_err(sql_error)?;
        let quantity: Option<f64> = row.get(1).map_err(sql_error)?;
        let direction: Option<String> = row.get(2).map_err(sql_error)?;
        let (Some(symbol), Some(quantity)) = (symbol, finite(quantity)) else {
            return Ok((currency, None));
        };
        let quantity_state: String = row.get(5).map_err(sql_error)?;
        if quantity_state != "value" || quantity < 0.0 || symbol.is_empty() {
            return Ok((currency, None));
        }
        let sign = match direction.as_deref() {
            Some("多头" | "LONG" | "long") => 1.0,
            Some("空头" | "SHORT" | "short") => -1.0,
            _ => return Ok((currency, None)),
        };
        let extra: Value = serde_json::from_str(
            &row.get::<_, Option<String>>(4)
                .map_err(sql_error)?
                .unwrap_or_default(),
        )
        .unwrap_or(Value::Null);
        let state: String = row.get(6).map_err(sql_error)?;
        let value = if quantity == 0.0 {
            Some(0.0)
        } else if state == "value" {
            finite(row.get(3).map_err(sql_error)?).map(|v| v * sign)
        } else {
            None
        };
        let p = output.entry(symbol.clone()).or_insert_with(|| Position {
            quantity: 0.0,
            value: Some(0.0),
            product: product(channel, &symbol, &extra),
        });
        p.quantity += quantity * sign;
        p.value = p.value.zip(value).map(|(a, b)| a + b);
    }
    Ok((currency, (count == Some(seen)).then_some(output)))
}

#[derive(Clone)]
struct Fill {
    symbol: String,
    id: Option<String>,
    quantity: Option<f64>,
    amount: Option<f64>,
    fee: Option<f64>,
    currency: Option<String>,
    product: String,
    converted_currency: Option<String>,
}

fn fill(row: &rusqlite::Row<'_>, channel: &str) -> rusqlite::Result<Fill> {
    let symbol: String = row.get(1)?;
    let extra: Value = serde_json::from_str(&row.get::<_, Option<String>>(6)?.unwrap_or_default())
        .unwrap_or(Value::Null);
    let direction = extra
        .get("direction")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_ascii_lowercase();
    let order_direction: Option<String> = row.get(13)?;
    let direction = if matches!(direction.as_str(), "buy" | "sell") {
        direction
    } else {
        order_direction.unwrap_or_default().to_ascii_lowercase()
    };
    let raw_quantity: Option<f64> = row.get(3)?;
    let quantity =
        finite(raw_quantity)
            .filter(|v| *v > 0.0)
            .and_then(|v| match direction.as_str() {
                "buy" => Some(v),
                "sell" => Some(-v),
                _ => None,
            });
    let price: Option<f64> = row.get(4)?;
    let multiplier: Option<f64> = row.get(5)?;
    let conversion_recorded: bool = row.get(10)?;
    let rate: Option<f64> = row.get(11)?;
    let rate = if conversion_recorded {
        finite(rate).filter(|v| *v > 0.0)
    } else {
        Some(1.0)
    };
    let amount = quantity
        .zip(finite(price))
        .zip(finite(multiplier).filter(|v| *v > 0.0))
        .zip(rate)
        .and_then(|(((q, p), m), fx)| finite(Some(-q * p * m * fx)));
    let fee_state: String = row.get(8)?;
    let fee = if fee_state == "value" {
        finite(row.get(7)?)
    } else {
        None
    };
    let currency: Option<String> = row.get(9)?;
    Ok(Fill {
        symbol: symbol.clone(),
        id: row.get(2)?,
        quantity,
        amount,
        fee,
        currency,
        product: product(channel, &symbol, &extra),
        converted_currency: if conversion_recorded {
            Some(
                row.get::<_, Option<String>>(12)?
                    .unwrap_or_else(|| "[unknown]".into()),
            )
        } else {
            None
        },
    })
}

fn apply(
    ledger: &mut BTreeMap<String, Ledger>,
    target: &Connection,
    trade: Fill,
    currency: Option<&str>,
) -> Result<(), AnalysisError> {
    let p = ledger
        .entry(trade.symbol.clone())
        .or_insert_with(|| Ledger {
            initial: Some(0.0),
            product: trade.product.clone(),
            ..Default::default()
        });
    if let Some(id) = trade
        .id
        .as_ref()
        .filter(|id| !matches!(id.as_str(), "null" | "\"\""))
    {
        let previous = target.query_row(
            "SELECT quantity,amount,fee,currency FROM native_stage.contribution_seen WHERE symbol=?1 AND id=?2",
            params![trade.symbol,id], |r| Ok((r.get::<_,Option<f64>>(0)?,r.get::<_,Option<f64>>(1)?,r.get::<_,Option<f64>>(2)?,r.get::<_,Option<String>>(3)?))
        ).optional().map_err(stage_error)?;
        let fingerprint = (
            trade.quantity,
            trade.amount,
            trade.fee,
            trade.currency.clone(),
        );
        if let Some(previous) = previous {
            if previous != fingerprint {
                p.issues += 1;
            }
            return Ok(());
        }
        target
            .execute(
                "INSERT INTO native_stage.contribution_seen VALUES(?1,?2,?3,?4,?5,?6)",
                params![
                    trade.symbol,
                    id,
                    trade.quantity,
                    trade.amount,
                    trade.fee,
                    trade.currency
                ],
            )
            .map_err(stage_error)?;
    } else {
        p.issues += 1;
    }
    p.trades += 1;
    if let Some(q) = trade.quantity {
        p.quantity += q;
    } else {
        p.issues += 1;
    }
    if trade
        .converted_currency
        .as_deref()
        .is_some_and(|value| Some(value) != currency)
    {
        p.issues += 1;
    }
    if let Some(v) = trade.amount {
        p.cash += v;
    } else {
        p.issues += 1;
    }
    if let Some(fee) = trade
        .fee
        .filter(|_| trade.currency.as_deref() == currency && currency.is_some())
    {
        p.fee += fee;
    } else {
        p.fee_missing += 1;
    }
    Ok(())
}

pub(crate) fn write(
    source: &Connection,
    target: &Connection,
    account: i64,
) -> Result<(), AnalysisError> {
    target.execute_batch("CREATE TABLE IF NOT EXISTS native_stage.contribution_seen(symbol TEXT,id TEXT,quantity REAL,amount REAL,fee REAL,currency TEXT,PRIMARY KEY(symbol,id));").map_err(stage_error)?;
    let channel: String = source
        .query_row(
            "SELECT trade_channel FROM account WHERE id=?1",
            [account],
            |r| r.get(0),
        )
        .map_err(sql_error)?;
    for range in ["30", "90", "all"] {
        write_range(source, target, account, &channel, range)?;
    }
    Ok(())
}

fn write_range(
    source: &Connection,
    target: &Connection,
    account: i64,
    channel: &str,
    range: &str,
) -> Result<(), AnalysisError> {
    let mut points=target.prepare("SELECT idx,observed_at,record_id FROM native_stage.performance_result_point WHERE range_key=?1 ORDER BY idx").map_err(stage_error)?;
    let points = points
        .query_map([range], |r| {
            Ok((
                r.get::<_, i64>(0)?,
                r.get::<_, String>(1)?,
                r.get::<_, Option<i64>>(2)?,
            ))
        })
        .map_err(stage_error)?
        .collect::<rusqlite::Result<Vec<_>>>()
        .map_err(stage_error)?;
    let Some((_, first_time, Some(first_record))) = points.first() else {
        return Ok(());
    };
    let (currency, initial) = positions(source, *first_record, channel)?;
    let mut ledger = BTreeMap::<String, Ledger>::new();
    if initial.is_none() {
        ledger.insert(
            "[unknown]".into(),
            Ledger {
                initial: None,
                product: "未归因持仓".into(),
                issues: 1,
                ..Default::default()
            },
        );
    }
    if let Some(initial) = initial.as_ref() {
        for (symbol, p) in initial {
            ledger.insert(
                symbol.clone(),
                Ledger {
                    initial: p.value,
                    quantity: p.quantity,
                    product: p.product.clone(),
                    ..Default::default()
                },
            );
        }
    }
    target
        .execute("DELETE FROM native_stage.contribution_seen", [])
        .map_err(stage_error)?;
    let mut statement=source.prepare("SELECT axile_time(r.created_at),COALESCE(NULLIF(t.symbol,''),'[unknown]'),t.trade_id,t.trade_volume,t.trade_price,z.unit_multiplier,t.extra,t.fee,t.fee_state,t.fee_currency,t.conversion_recorded,v.rate,v.value_currency,
        (SELECT o.direction FROM execution_order o WHERE o.record_id=t.record_id AND o.order_id=t.order_id ORDER BY o.ordinal DESC LIMIT 1)
        FROM executerecord r JOIN business_trade t ON t.record_id=r.id
        LEFT JOIN execution_symbol_sizing z ON z.record_id=t.record_id AND z.path=substr(t.collection_path,1,length(t.collection_path)-7) || '/sizing'
        LEFT JOIN business_trade_conversion v ON v.record_id=t.record_id AND v.trade_path=t.path
        WHERE r.account_id=?1 AND axile_time(r.created_at)>axile_time(?2) ORDER BY axile_time(r.created_at),r.id,t.ordinal").map_err(sql_error)?;
    let mut fills = statement
        .query(params![account, first_time])
        .map_err(sql_error)?;
    let mut pending = fills
        .next()
        .map_err(sql_error)?
        .map(|row| {
            Ok::<_, AnalysisError>((
                row.get::<_, String>(0).map_err(sql_error)?,
                fill(row, channel).map_err(sql_error)?,
            ))
        })
        .transpose()?;
    let mut insert=target.prepare_cached("INSERT INTO native_stage.performance_contribution(range_key,idx,symbol,product,currency,gross_pnl,fee,fee_missing,issues,trades) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10)").map_err(stage_error)?;
    for (idx, time, record) in points {
        let cutoff = crate::calendar::canonical_time(&time)
            .map_err(|e| AnalysisError::new("INVALID_RESULT_TIME", e))?;
        while pending.as_ref().is_some_and(|(t, _)| t <= &cutoff) {
            let (_, trade) = pending.take().unwrap();
            apply(&mut ledger, target, trade, currency.as_deref())?;
            pending = fills
                .next()
                .map_err(sql_error)?
                .map(|row| {
                    Ok::<_, AnalysisError>((
                        row.get::<_, String>(0).map_err(sql_error)?,
                        fill(row, channel).map_err(sql_error)?,
                    ))
                })
                .transpose()?;
        }
        let (current_currency, current) = match record {
            Some(id) => positions(source, id, channel)?,
            None => (None, None),
        };
        if let Some(positions) = current.as_ref() {
            for (symbol, p) in positions {
                ledger.entry(symbol.clone()).or_insert_with(|| Ledger {
                    initial: if initial.is_some() { Some(0.0) } else { None },
                    product: p.product.clone(),
                    ..Default::default()
                });
            }
        }
        for (symbol, p) in &mut ledger {
            let value = if let Some(positions) = current.as_ref() {
                let actual = positions.get(symbol);
                let quantity = actual.map_or(0.0, |v| v.quantity);
                if (quantity - p.quantity).abs() > 1e-8 {
                    p.issues += 1;
                }
                p.quantity = quantity;
                actual.map_or(Some(0.0), |v| v.value)
            } else {
                p.issues += 1;
                None
            };
            if current_currency != currency || currency.is_none() {
                p.issues += 1;
            }
            if initial.is_none() {
                p.initial = None;
            }
            let gross = finite(value.zip(p.initial).map(|(v, b)| p.cash + v - b));
            insert
                .execute(params![
                    range,
                    idx,
                    symbol,
                    p.product,
                    currency,
                    gross,
                    p.fee,
                    p.fee_missing,
                    p.issues,
                    p.trades
                ])
                .map_err(stage_error)?;
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    fn database() -> Connection {
        let c = Connection::open_in_memory().unwrap();
        crate::calendar::register_time_function(&c).unwrap();
        c.execute_batch("CREATE TABLE account(id INTEGER,trade_channel TEXT);
          INSERT INTO account VALUES(1,'tq');
          CREATE TABLE executerecord(id INTEGER,account_id INTEGER,created_at TEXT);
          CREATE TABLE business_asset_binding(record_id INTEGER,path TEXT,asset_id TEXT);
          CREATE TABLE business_asset_snapshot(asset_id TEXT,currency TEXT,positions_kind TEXT,positions_count INTEGER,observation_valid INTEGER,source TEXT);
          CREATE TABLE business_asset_position(asset_id TEXT,ordinal INTEGER,symbol TEXT,volume REAL,direction TEXT,market_value REAL,extra TEXT,volume_state TEXT,market_value_state TEXT);
          CREATE TABLE business_trade(record_id INTEGER,symbol TEXT,trade_id TEXT,trade_volume REAL,trade_price REAL,extra TEXT,fee REAL,fee_state TEXT,fee_currency TEXT,conversion_recorded INTEGER,collection_path TEXT,path TEXT,ordinal INTEGER);
          CREATE TABLE execution_symbol_sizing(record_id INTEGER,path TEXT,unit_multiplier REAL);
          CREATE TABLE business_trade_conversion(record_id INTEGER,trade_path TEXT,rate REAL,value_currency TEXT);
          CREATE TABLE execution_order(record_id INTEGER,order_id TEXT,direction TEXT,ordinal INTEGER);
          ATTACH DATABASE '' AS native_stage;
          CREATE TABLE native_stage.performance_result_point(range_key TEXT,idx INTEGER,observed_at TEXT,record_id INTEGER);
          CREATE TABLE native_stage.performance_contribution(range_key TEXT,idx INTEGER,symbol TEXT,product TEXT,currency TEXT,gross_pnl REAL,fee REAL,fee_missing INTEGER,issues INTEGER,trades INTEGER);
          INSERT INTO executerecord VALUES(1,1,'2026-01-01T09:00:00'),(2,1,'2026-01-02T09:00:00'),(3,1,'2026-01-03T09:00:00');
          INSERT INTO native_stage.performance_result_point VALUES('all',0,'2026-01-01T09:00:00',1),('all',1,'2026-01-02T09:00:00',2),('all',2,'2026-01-03T09:00:00',3);
          INSERT INTO business_asset_binding VALUES(1,'/account_assets','a'),(2,'/account_assets','b'),(3,'/account_assets','c');
          INSERT INTO business_asset_snapshot VALUES('a','CNY','array',1,1,'real'),('b','CNY','array',1,1,'real'),('c','CNY','array',0,1,'real');
          INSERT INTO business_asset_position VALUES('a',0,'fu2601',2,'多头',2000,'{\"tq_symbol\":\"SHFE.fu2601\"}','value','value'),('b',0,'fu2601',1,'多头',1200,'{\"tq_symbol\":\"SHFE.fu2601\"}','value','value');
          INSERT INTO execution_symbol_sizing VALUES(2,'/symbol_results/fu2601/sizing',10),(3,'/symbol_results/fu2601/sizing',10);
          INSERT INTO business_trade VALUES(2,'fu2601','sell1',1,110,'{\"direction\":\"SELL\",\"tq_symbol\":\"SHFE.fu2601\"}',1,'value','CNY',0,'/symbol_results/fu2601/trades','/symbol_results/fu2601/trades/0',0),
          (3,'fu2601','sell2',1,125,'{\"direction\":\"SELL\",\"tq_symbol\":\"SHFE.fu2601\"}',NULL,'absent',NULL,0,'/symbol_results/fu2601/trades','/symbol_results/fu2601/trades/0',0);").unwrap();
        c.execute_batch("ALTER TABLE business_trade ADD COLUMN order_id TEXT;")
            .unwrap();
        c
    }
    fn values(c: &Connection) -> Vec<(f64, f64, i64, i64)> {
        c.prepare("SELECT gross_pnl,fee,fee_missing,issues FROM native_stage.performance_contribution ORDER BY idx").unwrap()
          .query_map([],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).unwrap().collect::<rusqlite::Result<_>>().unwrap()
    }
    #[test]
    fn initial_holdings_partial_close_and_flat_contract_keep_money_and_unknown_fees() {
        let c = database();
        write(&c, &c, 1).unwrap();
        assert_eq!(
            values(&c),
            vec![(0.0, 0.0, 0, 0), (300.0, 1.0, 0, 0), (350.0, 1.0, 1, 0)]
        );
    }
    #[test]
    fn duplicate_fill_is_counted_once_but_missing_fill_is_an_issue() {
        let c = database();
        c.execute_batch("INSERT INTO business_trade SELECT record_id,symbol,trade_id,trade_volume,trade_price,extra,fee,fee_state,fee_currency,conversion_recorded,collection_path,path,1,order_id FROM business_trade WHERE record_id=2;").unwrap();
        write(&c, &c, 1).unwrap();
        assert_eq!(values(&c)[1], (300.0, 1.0, 0, 0));
        c.execute_batch("DELETE FROM native_stage.performance_contribution; DELETE FROM business_trade WHERE record_id=2;").unwrap();
        write(&c, &c, 1).unwrap();
        assert!(values(&c)[1].3 > 0);
    }
    #[test]
    fn futures_rolls_group_by_exchange_and_options_remain_separate() {
        assert_eq!(
            product(
                "tq",
                "fu2601",
                &serde_json::json!({"tq_symbol":"SHFE.fu2601"})
            ),
            "SHFE.fu"
        );
        assert_eq!(
            product("ctp", "fu2605", &serde_json::json!({"exchange_id":"SHFE"})),
            "SHFE.fu"
        );
        assert_eq!(
            product(
                "tq",
                "fu2601C1000",
                &serde_json::json!({"tq_symbol":"SHFE.fu2601C1000"})
            ),
            "fu2601C1000"
        );
        assert_eq!(product("qmt", "000001", &Value::Null), "000001");
    }

    #[test]
    fn short_holdings_and_missing_multiplier_keep_direction_and_quality() {
        let c = database();
        c.execute_batch("UPDATE business_asset_position SET direction='空头'; UPDATE business_trade SET extra=replace(extra,'SELL','BUY');").unwrap();
        write(&c, &c, 1).unwrap();
        assert_eq!(values(&c)[2].0, -350.0);
        c.execute_batch("DELETE FROM native_stage.performance_contribution; UPDATE execution_symbol_sizing SET unit_multiplier=NULL;").unwrap();
        write(&c, &c, 1).unwrap();
        assert!(values(&c)[2].3 > 0);
    }

    #[test]
    fn unknown_initial_holdings_never_become_a_flat_baseline() {
        let c = database();
        c.execute_batch("DELETE FROM business_asset_snapshot WHERE asset_id='a';")
            .unwrap();
        write(&c, &c, 1).unwrap();
        let known: i64 = c.query_row("SELECT count(*) FROM native_stage.performance_contribution WHERE gross_pnl IS NOT NULL",[],|r|r.get(0)).unwrap();
        assert_eq!(known, 0);
    }
}
