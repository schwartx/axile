//! Independent equity ledger, matching Python's `equity-ledger-1` semantics.
//!
//! A ledger accepts one already-filtered observation at a time. Only current
//! holdings, their last prices, and one closing equity per observed day remain
//! resident; historical observations are never retained by the replay engine.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};

pub const ENGINE_VERSION: &str = "equity-ledger-1";

/// One normalized execution observation. Absent and empty targets differ:
/// upstream participation filtering skips absent targets and accepts clears.
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Observation {
    pub id: i64,
    pub execution_id: Option<String>,
    /// UTC Unix microseconds supplied by the storage projection.
    pub time: i64,
    pub asset: Option<f64>,
    pub target: Option<BTreeMap<String, f64>>,
    pub prices: BTreeMap<String, f64>,
    pub target_weight: Option<BTreeMap<String, f64>>,
    pub sizing_fallback: bool,
    pub quantity_target: bool,
    pub target_units: Option<BTreeMap<String, f64>>,
    pub target_quantities: Option<BTreeMap<String, f64>>,
}

#[derive(Clone, Copy, Debug, Serialize)]
pub struct ReplayClose {
    pub cumulative_return: f64,
    pub daily_return: Option<f64>,
}

#[derive(Clone, Copy, Debug)]
pub struct Holding {
    /// Price-to-account-currency sensitivity, which can include conversion.
    pub units: f64,
    /// Original quantity prevents conversion changes from creating turnover.
    pub quantity: Option<f64>,
    pub mark: f64,
}

impl Holding {
    const ZERO: Self = Self {
        units: 0.0,
        quantity: Some(0.0),
        mark: 0.0,
    };
}

/// Streaming state for one target mode and one independent equity baseline.
#[derive(Clone)]
pub struct ReplayLedger {
    pub initial_equity: f64,
    pub equity: f64,
    pub fee_rate: f64,
    pub target_weight: bool,
    pub initialized: bool,
    pub previous_close: f64,
    pub positions: BTreeMap<String, Holding>,
    pub closes: BTreeMap<String, f64>,
}

impl ReplayLedger {
    pub fn new(initial_equity: f64, fee_rate: f64, target_weight: bool) -> Result<Self, String> {
        if !initial_equity.is_finite() || initial_equity <= 0.0 {
            return Err("initial equity must be finite and positive".into());
        }
        if !fee_rate.is_finite() || fee_rate < 0.0 {
            return Err("fee rate must be finite and non-negative".into());
        }
        Ok(Self {
            initial_equity,
            equity: initial_equity,
            fee_rate,
            target_weight,
            initialized: false,
            previous_close: initial_equity,
            positions: BTreeMap::new(),
            closes: BTreeMap::new(),
        })
    }

    /// Settle the previous holdings, then rebalance at pre-fee equity.
    ///
    /// The first observation establishes an already-open baseline, so opening
    /// turnover before this baseline is deliberately not charged. Missing-target
    /// and missing-price observations must be rejected by the participation
    /// filter before calling this method: skipped observations do not move marks.
    pub fn push(&mut self, item: &Observation) -> Result<(), String> {
        let day = local_datetime(item.time)?.date().to_string();

        // Compute the complete transition before replacing state, so a missing
        // price or invalid weight transition never partially advances a ledger.
        let mut equity = self.equity;
        if self.initialized {
            let mut pnl = 0.0;
            for (symbol, holding) in &self.positions {
                pnl += holding.units * (price(item, symbol)? - holding.mark);
            }
            equity += pnl;
        }
        let target = positions(item, equity, self.target_weight)?;
        if self.initialized {
            let mut total_turnover = 0.0;
            for (symbol, old) in &self.positions {
                let new = target.get(symbol).copied().unwrap_or(Holding::ZERO);
                total_turnover += turnover(*old, new, price(item, symbol)?);
            }
            for (symbol, new) in &target {
                if !self.positions.contains_key(symbol) {
                    total_turnover += turnover(Holding::ZERO, *new, new.mark);
                }
            }
            equity -= self.fee_rate * total_turnover;
        }
        self.positions = target;
        self.equity = equity;
        self.initialized = true;
        self.closes.insert(day, equity);
        Ok(())
    }

    /// Consume the ledger without retaining a second map of closing equities.
    pub fn finish(self) -> BTreeMap<String, ReplayClose> {
        let mut previous_close = self.previous_close;
        self.closes
            .into_iter()
            .map(|(day, close)| {
                let value = ReplayClose {
                    cumulative_return: close / self.initial_equity - 1.0,
                    daily_return: if previous_close > 0.0 {
                        Some(close / previous_close - 1.0)
                    } else {
                        None
                    },
                };
                previous_close = close;
                (day, value)
            })
            .collect()
    }
}

fn price(item: &Observation, symbol: &str) -> Result<f64, String> {
    item.prices
        .get(symbol)
        .copied()
        .ok_or_else(|| format!("missing price for {symbol} at {}", item.time))
}

fn positions(
    item: &Observation,
    equity: f64,
    target_weight: bool,
) -> Result<BTreeMap<String, Holding>, String> {
    if !target_weight || item.quantity_target {
        if let Some(units) = &item.target_units {
            return units
                .iter()
                .filter(|(_, units)| **units != 0.0)
                .map(|(symbol, units)| {
                    Ok((
                        symbol.clone(),
                        Holding {
                            units: *units,
                            quantity: item
                                .target_quantities
                                .as_ref()
                                .and_then(|quantities| quantities.get(symbol))
                                .copied(),
                            mark: price(item, symbol)?,
                        },
                    ))
                })
                .collect();
        }
    }
    let weights = if target_weight {
        item.target_weight.as_ref()
    } else {
        item.target.as_ref()
    };
    let Some(weights) = weights else {
        return Ok(BTreeMap::new());
    };
    if equity <= 0.0 && weights.values().any(|weight| *weight != 0.0) {
        return Err("模拟权益非正，无法继续按权重调仓".into());
    }
    weights
        .iter()
        .filter(|(_, weight)| **weight != 0.0)
        .map(|(symbol, weight)| {
            let mark = price(item, symbol)?;
            if mark == 0.0 {
                return Err(format!("zero price for {symbol} at {}", item.time));
            }
            Ok((
                symbol.clone(),
                Holding {
                    units: weight * equity / mark,
                    quantity: None,
                    mark,
                },
            ))
        })
        .collect()
}

fn turnover(old: Holding, new: Holding, price: f64) -> f64 {
    if let (Some(old_quantity), Some(new_quantity)) = (old.quantity, new.quantity) {
        let multiplier = if new_quantity != 0.0 {
            new.units / new_quantity
        } else if old_quantity != 0.0 {
            old.units / old_quantity
        } else {
            0.0
        };
        (new_quantity - old_quantity).abs() * multiplier * price
    } else {
        (new.units - old.units).abs() * price
    }
}

pub fn local_datetime(time: i64) -> Result<chrono::NaiveDateTime, String> {
    chrono::DateTime::<chrono::Utc>::from_timestamp_micros(time)
        .map(|v| {
            v.with_timezone(&chrono::FixedOffset::east_opt(28800).unwrap())
                .naive_local()
        })
        .ok_or_else(|| "microsecond timestamp is out of range".into())
}

/// Skip observations without moving the previous accepted target or marks.
pub fn missing_prices(
    item: &Observation,
    target_weight: bool,
    previous: &std::collections::BTreeSet<String>,
) -> bool {
    let target = if target_weight {
        &item.target_weight
    } else {
        &item.target
    };
    target.as_ref().is_some_and(|weights| {
        previous.iter().any(|s| !item.prices.contains_key(s))
            || weights
                .iter()
                .any(|(s, w)| *w != 0.0 && !item.prices.contains_key(s))
    })
}

#[derive(Serialize)]
pub struct PreviewMode {
    pub start: Option<i64>,
    pub used: usize,
    pub skipped: usize,
    pub closes: BTreeMap<String, ReplayClose>,
}

pub fn preview(
    items: &[Observation],
    equity: f64,
    fee: f64,
    target_weight: bool,
) -> Result<PreviewMode, String> {
    let mut ledger = ReplayLedger::new(equity, fee, target_weight)?;
    let mut previous = std::collections::BTreeSet::new();
    let mut start = None;
    let mut used = 0;
    for item in items {
        let target = if target_weight {
            &item.target_weight
        } else {
            &item.target
        };
        if target.is_none() || missing_prices(item, target_weight, &previous) {
            continue;
        }
        previous = target
            .as_ref()
            .unwrap()
            .iter()
            .filter_map(|(s, w)| (*w != 0.0).then_some(s.clone()))
            .collect();
        ledger.push(item)?;
        start.get_or_insert(item.time);
        used += 1;
    }
    Ok(PreviewMode {
        start,
        used,
        skipped: items.len() - used,
        closes: ledger.finish(),
    })
}
