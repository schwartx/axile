//! Independent equity ledger, matching Python's `equity-ledger-1` semantics.
//!
//! A ledger accepts one already-filtered observation at a time. Only current
//! holdings, their last prices, and one closing equity per observed day remain
//! resident; historical observations are never retained by the replay engine.

use std::collections::BTreeMap;

use serde::Serialize;

pub const ENGINE_VERSION: &str = "equity-ledger-1";

/// One normalized execution observation. Absent and empty targets differ:
/// upstream participation filtering skips absent targets and accepts clears.
#[derive(Clone, Debug, Default)]
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
    pub target_units: Option<BTreeMap<String, f64>>,
    pub target_quantities: Option<BTreeMap<String, f64>>,
}

#[derive(Clone, Copy, Debug, Serialize)]
pub struct ReplayClose {
    pub cumulative_return: f64,
    pub daily_return: Option<f64>,
}

#[derive(Clone, Copy, Debug)]
pub(crate) struct Holding {
    /// Price-to-account-currency sensitivity, which can include conversion.
    pub(crate) units: f64,
    /// Original quantity prevents conversion changes from creating turnover.
    pub(crate) quantity: Option<f64>,
    pub(crate) mark: f64,
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
    pub(crate) initial_equity: f64,
    pub(crate) equity: f64,
    pub(crate) fee_rate: f64,
    pub(crate) target_weight: bool,
    pub(crate) initialized: bool,
    pub(crate) previous_close: f64,
    pub(crate) positions: BTreeMap<String, Holding>,
    pub(crate) closes: BTreeMap<String, f64>,
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
        let day = crate::time_us::local_datetime(item.time)?
            .date()
            .to_string();

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
    if !target_weight {
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

#[cfg(test)]
mod tests {
    use super::*;

    fn snapshot(index: usize, day: usize, price: f64, quantity: f64) -> Observation {
        Observation {
            id: index as i64,
            execution_id: Some(format!("exec-{index}")),
            time: crate::time_us::parse(&format!(
                "2026-01-{day:02}T{:02}:00:00+08:00",
                9 + index % 10
            ))
            .unwrap(),
            asset: Some(100.0),
            target: Some(BTreeMap::from([("A".into(), quantity * price / 100.0)])),
            prices: BTreeMap::from([("A".into(), price)]),
            target_weight: Some(BTreeMap::from([("A".into(), 1.0)])),
            sizing_fallback: false,
            target_units: Some(BTreeMap::from([("A".into(), quantity)])),
            target_quantities: Some(BTreeMap::from([("A".into(), quantity)])),
        }
    }

    fn run(items: &[Observation], fee: f64, weight: bool) -> BTreeMap<String, ReplayClose> {
        let mut ledger = ReplayLedger::new(100.0, fee, weight).unwrap();
        for item in items {
            ledger.push(item).unwrap();
        }
        ledger.finish()
    }

    fn near(actual: f64, expected: f64) {
        assert!((actual - expected).abs() < 1e-12, "{actual} != {expected}");
    }

    #[test]
    fn baseline_does_not_charge_opening_fee() {
        let result = run(&[snapshot(0, 1, 100.0, 1.0)], 0.1, false);
        near(result["2026-01-01"].cumulative_return, 0.0);
        near(result["2026-01-01"].daily_return.unwrap(), 0.0);
    }

    #[test]
    fn fixed_quantity_is_sampling_invariant_and_ignores_real_equity() {
        let first = snapshot(0, 1, 100.0, 1.0);
        let mut middle = snapshot(1, 1, 110.0, 1.0);
        let mut last = snapshot(2, 1, 121.0, 1.0);
        middle.asset = Some(200.0);
        last.asset = Some(400.0);
        let dense = run(&[first.clone(), middle, last.clone()], 0.0, false);
        let sparse = run(&[first, last], 0.0, false);
        near(dense["2026-01-01"].cumulative_return, 0.21);
        near(
            dense["2026-01-01"].cumulative_return,
            sparse["2026-01-01"].cumulative_return,
        );
    }

    #[test]
    fn add_reduce_reverse_and_clear_charge_quantity_turnover() {
        let items: Vec<_> = [
            (100.0, 1.0),
            (110.0, 2.0),
            (120.0, 1.0),
            (115.0, -1.0),
            (105.0, 0.0),
            (90.0, 0.0),
        ]
        .into_iter()
        .enumerate()
        .map(|(index, (price, quantity))| snapshot(index, 1, price, quantity))
        .collect();
        let result = run(&items, 0.001, false);
        near(
            result["2026-01-01"].cumulative_return,
            (35.0 - 0.565) / 100.0,
        );
    }

    #[test]
    fn conversion_changes_without_quantity_changes_do_not_charge_turnover() {
        let mut first = snapshot(0, 1, 100.0, 1.0);
        let mut second = snapshot(1, 1, 100.0, 1.0);
        first.target_units = Some(BTreeMap::from([("A".into(), 7.0)]));
        second.target_units = Some(BTreeMap::from([("A".into(), 8.0)]));
        let result = run(&[first, second], 0.01, false);
        near(result["2026-01-01"].cumulative_return, 0.0);
    }

    #[test]
    fn contract_roll_charges_both_sides() {
        let first = snapshot(0, 1, 100.0, 1.0);
        let mut roll = snapshot(1, 1, 200.0, 1.0);
        roll.prices = BTreeMap::from([("A".into(), 110.0), ("B".into(), 200.0)]);
        roll.target = Some(BTreeMap::from([("B".into(), 2.0)]));
        roll.target_units = Some(BTreeMap::from([("B".into(), 1.0)]));
        roll.target_quantities = Some(BTreeMap::from([("B".into(), 1.0)]));
        let mut last = roll.clone();
        last.time = crate::time_us::parse("2026-01-01T11:00:00+08:00").unwrap();
        last.prices = BTreeMap::from([("B".into(), 220.0)]);
        let result = run(&[first, roll, last], 0.001, false);
        near(result["2026-01-01"].cumulative_return, 0.2969);
    }

    #[test]
    fn multiplier_is_applied_to_pnl_and_daily_returns() {
        for direction in [1.0, -1.0] {
            let items: Vec<_> = [100.0, 102.0, 101.0]
                .into_iter()
                .enumerate()
                .map(|(index, price)| {
                    let mut item = snapshot(index, index + 1, price, 2.0 * direction);
                    item.target_units = Some(BTreeMap::from([("A".into(), 20.0 * direction)]));
                    item
                })
                .collect();
            let result = run(&items, 0.0, false);
            near(result["2026-01-02"].cumulative_return, direction * 0.4);
            near(result["2026-01-03"].cumulative_return, direction * 0.2);
            near(
                result["2026-01-03"].daily_return.unwrap(),
                -direction * 20.0 / (100.0 + direction * 40.0),
            );
        }
    }

    #[test]
    fn original_weight_mode_rebalances_with_its_own_equity() {
        let items: Vec<_> = [100.0, 110.0, 121.0]
            .into_iter()
            .enumerate()
            .map(|(index, price)| {
                let mut item = snapshot(index, 1, price, 0.0);
                item.target_weight = Some(BTreeMap::from([("A".into(), 0.5)]));
                item
            })
            .collect();
        let quantity = run(&items, 0.0, false);
        let weight = run(&items, 0.0, true);
        near(quantity["2026-01-01"].cumulative_return, 0.0);
        near(
            weight["2026-01-01"].cumulative_return,
            1.05_f64.powi(2) - 1.0,
        );
    }

    #[test]
    fn missing_quantity_evidence_uses_disclosed_executable_weights() {
        let items: Vec<_> = [100.0, 110.0, 121.0]
            .into_iter()
            .enumerate()
            .map(|(index, price)| {
                let mut item = snapshot(index, 1, price, 0.0);
                item.target_units = None;
                item.target_quantities = None;
                item.target = Some(BTreeMap::from([("A".into(), 0.5)]));
                item.sizing_fallback = true;
                item
            })
            .collect();
        near(
            run(&items, 0.0, false)["2026-01-01"].cumulative_return,
            1.05_f64.powi(2) - 1.0,
        );
    }

    #[test]
    fn weight_rebalance_uses_equity_before_turnover_fee() {
        let mut first = snapshot(0, 1, 100.0, 0.0);
        first.target_weight = Some(BTreeMap::from([("A".into(), 0.5)]));
        let mut rebalance = snapshot(1, 1, 110.0, 0.0);
        rebalance.target_weight = Some(BTreeMap::from([("A".into(), 1.0)]));
        let mut last = snapshot(2, 1, 121.0, 0.0);
        last.target_weight = Some(BTreeMap::new());
        // 0.5 units earn 5; rebalance to 105/110 units costs 50*1%.
        // Those pre-fee units then earn 10.5, and the clear costs 115.5*1%.
        near(
            run(&[first, rebalance, last], 0.01, true)["2026-01-01"].cumulative_return,
            (105.0 - 0.5 + 10.5 - 1.155) / 100.0 - 1.0,
        );
    }

    #[test]
    fn clear_uses_exit_price_and_does_not_resume_exposure() {
        let mut first = snapshot(0, 1, 100.0, 1.0);
        first.target_units = Some(BTreeMap::from([("A".into(), 10.0)]));
        let mut clear = snapshot(1, 1, 102.0, 0.0);
        clear.target_units = Some(BTreeMap::new());
        clear.target = Some(BTreeMap::new());
        clear.target_weight = Some(BTreeMap::new());
        let last = snapshot(2, 1, 120.0, 0.0);
        let result = run(&[first, clear, last], 0.001, false);
        near(result["2026-01-01"].cumulative_return, 0.1898);
    }

    #[test]
    fn zero_previous_equity_makes_only_daily_return_unknown() {
        let items: Vec<_> = [100.0, 50.0, 60.0]
            .into_iter()
            .enumerate()
            .map(|(index, price)| snapshot(index, index + 1, price, 2.0))
            .collect();
        let result = run(&items, 0.0, false);
        near(result["2026-01-02"].cumulative_return, -1.0);
        near(result["2026-01-03"].cumulative_return, -0.8);
        assert!(result["2026-01-03"].daily_return.is_none());
    }

    #[test]
    fn failed_transition_does_not_modify_ledger() {
        let first = snapshot(0, 1, 100.0, 1.0);
        let mut missing = snapshot(1, 1, 110.0, 2.0);
        missing.prices.clear();
        let last = snapshot(2, 1, 120.0, 0.0);
        let mut ledger = ReplayLedger::new(100.0, 0.0, false).unwrap();
        ledger.push(&first).unwrap();
        assert!(ledger.push(&missing).is_err());
        ledger.push(&last).unwrap();
        near(ledger.finish()["2026-01-01"].cumulative_return, 0.2);
    }

    #[test]
    fn nonpositive_equity_cannot_rebalance_nonzero_weights() {
        let first = snapshot(0, 1, 100.0, 1.0);
        let last = snapshot(1, 1, 0.0, 1.0);
        let mut ledger = ReplayLedger::new(100.0, 0.0, true).unwrap();
        ledger.push(&first).unwrap();
        assert!(ledger.push(&last).unwrap_err().contains("模拟权益非正"));
    }

    #[test]
    fn utc_microseconds_cross_the_shanghai_day_at_the_exact_boundary() {
        let mut first = snapshot(0, 1, 100.0, 1.0);
        let mut second = snapshot(1, 1, 110.0, 1.0);
        first.time = crate::time_us::parse("2025-12-31T15:59:59.999999Z").unwrap();
        second.time = crate::time_us::parse("2025-12-31T16:00:00.000000Z").unwrap();
        assert_eq!(second.time - first.time, 1);
        let result = run(&[first, second], 0.0, false);
        assert_eq!(
            result.keys().map(String::as_str).collect::<Vec<_>>(),
            ["2025-12-31", "2026-01-01"]
        );
        near(result["2026-01-01"].cumulative_return, 0.1);
    }

    #[test]
    fn an_empty_stream_produces_no_daily_closes() {
        assert!(ReplayLedger::new(100.0, 0.0, false)
            .unwrap()
            .finish()
            .is_empty());
    }
}
