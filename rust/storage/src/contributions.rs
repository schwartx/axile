//! Frozen cash-flow P&L at the same observations as the account curve.
use crate::analysis::sql as sql_error;
use crate::error::AnalysisError;
fn finite(v: Option<f64>) -> Option<f64> {
    v.filter(|v| v.is_finite())
}
use rusqlite::{Connection, OptionalExtension};
use serde_json::Value;
use std::collections::BTreeMap;

#[derive(Default, Clone, serde::Serialize, serde::Deserialize)]
pub(crate) struct Position {
    pub(crate) quantity: f64,
    pub(crate) value: Option<f64>,
    pub(crate) product: String,
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

pub(crate) fn positions(
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
pub(crate) struct Fill {
    pub(crate) symbol: String,
    pub(crate) id: Option<String>,
    pub(crate) trading_day: String,
    pub(crate) exchange_id: String,
    pub(crate) quantity: Option<f64>,
    pub(crate) amount: Option<f64>,
    pub(crate) fee: Option<f64>,
    pub(crate) currency: Option<String>,
    pub(crate) product: String,
    pub(crate) converted_currency: Option<String>,
}

pub(crate) fn fill(row: &rusqlite::Row<'_>, channel: &str) -> rusqlite::Result<Fill> {
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
        trading_day: extra["trading_day"].as_str().unwrap_or_default().into(),
        exchange_id: extra["exchange_id"].as_str().unwrap_or_default().into(),
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

pub(crate) const FILLS:&str="SELECT axile_time(r.created_at),COALESCE(NULLIF(t.symbol,''),'[unknown]'),t.trade_id,t.trade_volume,t.trade_price,
        COALESCE(z.unit_multiplier,(
            SELECT previous.unit_multiplier FROM execution_symbol_sizing previous
            JOIN executerecord pr ON pr.id=previous.record_id
            WHERE pr.account_id=r.account_id AND pr.result_staged=0
              AND previous.path=substr(t.collection_path,1,length(t.collection_path)-7) || '/sizing'
              AND previous.unit_multiplier>0
              AND (axile_time(pr.created_at)<axile_time(r.created_at)
                OR (axile_time(pr.created_at)=axile_time(r.created_at) AND pr.id<r.id))
            ORDER BY axile_time(pr.created_at) DESC,pr.id DESC LIMIT 1
        )),t.extra,t.fee,t.fee_state,t.fee_currency,
        CASE WHEN v.state='malformed' AND v.malformed_value='null' THEN 0 ELSE t.conversion_recorded END,
        v.rate,v.value_currency,
        (SELECT o.direction FROM execution_order o WHERE o.record_id=t.record_id AND o.order_id=t.order_id ORDER BY o.ordinal DESC LIMIT 1),r.id,t.path
        FROM executerecord r JOIN business_trade t ON t.record_id=r.id
        LEFT JOIN execution_symbol_sizing z ON z.record_id=t.record_id AND z.path=substr(t.collection_path,1,length(t.collection_path)-7) || '/sizing'
        LEFT JOIN business_trade_conversion v ON v.record_id=t.record_id AND v.trade_path=t.path
        WHERE r.account_id=?1 AND r.result_staged=0 AND axile_time(r.created_at)>axile_time(?2) ORDER BY axile_time(r.created_at),r.id,t.ordinal";
