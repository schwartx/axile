//! Native storage uses the same equity engine as browser previews.
pub(crate) use axile_analysis_core::equity::Holding;
pub use axile_analysis_core::equity::{Observation, ReplayClose, ReplayLedger, ENGINE_VERSION};

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeMap;

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
            quantity_target: false,
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
    #[test]
    fn quantity_targets_keep_units_in_both_replay_curves() {
        let mut items = vec![
            snapshot(0, 1, 10.0, 1.0),
            snapshot(1, 2, 20.0, 1.0),
            snapshot(2, 3, 30.0, 1.0),
        ];
        for item in &mut items {
            item.quantity_target = true;
            item.target_weight = item.target.clone();
        }
        let executable = run(&items, 0.0, false);
        let original = run(&items, 0.0, true);
        assert!((executable["2026-01-03"].cumulative_return - 0.2).abs() < 1e-12);
        assert_eq!(
            original["2026-01-03"].cumulative_return,
            executable["2026-01-03"].cumulative_return
        );
    }
}
