//! Current contribution balances; current-day marking is reversible without replaying its fills.
use super::{self as store, failure, sql, State};
use crate::{
    contributions::{self, Fill},
    error::AnalysisError,
    result_reader::Point,
};
use rusqlite::{params, Connection, OptionalExtension};
use serde::{Deserialize, Serialize};
use serde_json::json;
use std::collections::BTreeMap;
#[derive(Default, Clone, Serialize, Deserialize)]
struct Balance {
    account_id: i64,
    range_key: String,
    symbol: String,
    product: String,
    initial: Option<f64>,
    quantity: f64,
    cash: f64,
    fee: f64,
    fee_missing: i64,
    issues: i64,
    trades: i64,
    before_quantity: f64,
    before_initial: Option<f64>,
    before_cash: f64,
    before_issues: i64,
}
fn new_balance(s: &State, symbol: &str, product: &str) -> Balance {
    Balance {
        account_id: s.account_id,
        range_key: s.range_key.clone(),
        symbol: symbol.into(),
        product: product.into(),
        initial: (s.initial_known == 1).then_some(0.0),
        ..Default::default()
    }
}
fn apply(p: &mut Balance, t: &Fill, currency: Option<&str>) {
    p.trades += 1;
    if let Some(q) = t.quantity {
        p.quantity += q;
    } else {
        p.issues += 1;
    }
    if let Some(v) = t.amount {
        p.cash += v;
    } else {
        p.issues += 1;
    }
    if t.converted_currency
        .as_deref()
        .is_some_and(|v| Some(v) != currency)
    {
        p.issues += 1;
    }
    if let Some(f) = t
        .fee
        .filter(|_| t.currency.as_deref() == currency && currency.is_some())
    {
        p.fee += f;
    } else {
        p.fee_missing += 1;
    }
}

pub(crate) fn advance(
    source: &Connection,
    stage: &Connection,
    s: &mut State,
    points: &[Point],
    reset: bool,
) -> Result<(), AnalysisError> {
    let Some(baseline) = s.baseline_id else {
        return Ok(());
    };
    let mut balances = BTreeMap::<String, Balance>::new();
    if !reset {
        for row in store::rows(
            source,
            "SELECT * FROM analysis_contribution_state WHERE account_id=?1 AND range_key=?2",
            &[&s.account_id, &s.range_key],
        )? {
            let b: Balance = serde_json::from_value(row)
                .map_err(|e| failure("ANALYSIS_STATE", e.to_string()))?;
            balances.insert(b.symbol.clone(), b);
        }
    }
    if reset {
        let (currency, positions) = contributions::positions(source, baseline, &s.trade_channel)?;
        s.contribution_currency = currency;
        s.initial_known = i64::from(positions.is_some());
        if let Some(positions) = positions {
            for (symbol, p) in positions {
                let mut b = new_balance(s, &symbol, &p.product);
                b.initial = p.value;
                b.quantity = p.quantity;
                balances.insert(symbol, b);
            }
        } else {
            let mut b = new_balance(s, "[unknown]", "未归因持仓");
            b.issues = 1;
            balances.insert(b.symbol.clone(), b);
        }
        s.contribution_time = s
            .baseline_time
            .as_deref()
            .map(crate::calendar::canonical_time)
            .transpose()
            .map_err(|e| failure("ANALYSIS_TIME", e))?;
        s.contribution_day = None;
    }
    let bounded = contributions::FILLS.replace(
        "ORDER BY axile_time(r.created_at),r.id,t.ordinal",
        "AND axile_time(r.created_at)<=?3 ORDER BY axile_time(r.created_at),r.id,t.ordinal",
    );
    let first=contributions::FILLS.replace("axile_time(?2)","?2").replace("ORDER BY axile_time(r.created_at),r.id,t.ordinal","AND COALESCE(NULLIF(t.symbol,''),'[unknown]')=?3 AND t.trade_id=?4 AND COALESCE(json_extract(t.extra,'$.trading_day'),'')=?5 AND COALESCE(json_extract(t.extra,'$.exchange_id'),'')=?6 ORDER BY axile_time(r.created_at),r.id,t.ordinal LIMIT 1");
    let mut query = source.prepare(&bounded).map_err(sql)?;
    let mut first = source.prepare(&first).map_err(sql)?;
    for point in points {
        let cutoff = crate::calendar::canonical_time(&point.observed_at)
            .map_err(|e| failure("ANALYSIS_TIME", e))?;
        let after = s.contribution_time.clone().unwrap_or_default();
        if cutoff < after {
            return Err(failure(
                "ANALYSIS_ORDER",
                "contribution time moved backwards",
            ));
        }
        if cutoff == after && s.contribution_day.is_some() {
            continue;
        }
        let same_day = s.contribution_day.as_deref() == Some(point.date.as_str());
        if same_day {
            for b in balances.values_mut() {
                b.quantity = b.before_quantity;
                b.initial = b.before_initial;
                b.cash = b.before_cash;
                b.issues = b.before_issues;
            }
        }
        apply_fills(&mut query, &mut first, s, &mut balances, &after, &cutoff)?;
        mark(source, stage, s, &mut balances, point)?;
        s.contribution_time = Some(cutoff);
        s.contribution_day = Some(point.date.clone());
    }
    for b in balances.values() {
        store::put(
            stage,
            "analysis_contribution_state",
            &serde_json::to_value(b).map_err(|e| failure("ANALYSIS_STATE", e.to_string()))?,
        )?;
    }
    Ok(())
}

fn apply_fills(
    query: &mut rusqlite::Statement<'_>,
    first: &mut rusqlite::Statement<'_>,
    s: &State,
    balances: &mut BTreeMap<String, Balance>,
    after: &str,
    cutoff: &str,
) -> Result<(), AnalysisError> {
    let mut fills = query
        .query(params![s.account_id, after, cutoff])
        .map_err(sql)?;
    while let Some(row) = fills.next().map_err(sql)? {
        let t = contributions::fill(row, &s.trade_channel).map_err(sql)?;
        let b = balances
            .entry(t.symbol.clone())
            .or_insert_with(|| new_balance(s, &t.symbol, &t.product));
        if let Some(id) =
            t.id.as_ref()
                .filter(|id| !matches!(id.as_str(), "null" | "\"\""))
        {
            let original = first
                .query_row(
                    params![s.account_id, "", t.symbol, id, t.trading_day, t.exchange_id],
                    |r| {
                        Ok((
                            r.get::<_, i64>(14)?,
                            r.get::<_, String>(15)?,
                            contributions::fill(r, &s.trade_channel)?,
                        ))
                    },
                )
                .optional()
                .map_err(sql)?;
            if let Some((record, path, prior)) = original {
                if record != row.get::<_, i64>(14).map_err(sql)?
                    || path != row.get::<_, String>(15).map_err(sql)?
                {
                    if (
                        t.quantity,
                        t.amount,
                        t.fee,
                        &t.currency,
                        &t.converted_currency,
                    ) != (
                        prior.quantity,
                        prior.amount,
                        prior.fee,
                        &prior.currency,
                        &prior.converted_currency,
                    ) {
                        b.issues += 1;
                    }
                    continue;
                }
            }
        } else {
            b.issues += 1;
        }
        apply(b, &t, s.contribution_currency.as_deref());
    }
    Ok(())
}

fn mark(
    source: &Connection,
    stage: &Connection,
    s: &mut State,
    balances: &mut BTreeMap<String, Balance>,
    point: &Point,
) -> Result<(), AnalysisError> {
    let (currency, current) = point
        .record_id
        .map(|id| contributions::positions(source, id, &s.trade_channel))
        .transpose()?
        .unwrap_or((None, None));
    if s.contribution_currency.is_none() && current.is_some() {
        s.contribution_currency = currency.clone();
    }
    if let Some(current) = &current {
        for (symbol, p) in current {
            balances
                .entry(symbol.clone())
                .or_insert_with(|| new_balance(s, symbol, &p.product));
        }
    }
    for b in balances.values_mut() {
        b.before_quantity = b.quantity;
        b.before_initial = b.initial;
        b.before_cash = b.cash;
        b.before_issues = b.issues;
        let value = if let Some(current) = &current {
            let actual = current.get(&b.symbol);
            let quantity = actual.map_or(0.0, |p| p.quantity);
            if (quantity - b.quantity).abs() > 1e-8 {
                b.issues += 1;
            }
            b.quantity = quantity;
            actual.map_or(Some(0.0), |p| p.value)
        } else {
            b.issues += 1;
            None
        };
        let value = value
            .filter(|v| v.is_finite() && currency == s.contribution_currency && currency.is_some());
        if value.is_none() {
            b.issues += 1;
        }
        if b.initial.is_none() && value.is_some() && b.symbol != "[unknown]" {
            b.initial = value;
            b.cash = 0.0;
            b.issues += 1;
        }
        let gross = value
            .zip(b.initial)
            .map(|(v, i)| b.cash + v - i)
            .filter(|v| v.is_finite());
        store::put(
            stage,
            "analysis_contribution_day",
            &json!({"account_id":s.account_id,"range_key":s.range_key,"day":point.date,"symbol":b.symbol,"product":b.product,"currency":s.contribution_currency,"gross_pnl":gross,"fee":b.fee,"fee_missing":b.fee_missing,"issues":b.issues,"trades":b.trades}),
        )?;
    }
    Ok(())
}
