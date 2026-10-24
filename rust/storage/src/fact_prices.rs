//! Distinct cost and replay prices from the same finite business evidence.

pub(crate) fn positive(value: Option<f64>) -> Option<f64> {
    value.filter(|value| value.is_finite() && *value > 0.0)
}

pub(crate) fn mid(bid: Option<f64>, ask: Option<f64>) -> Option<f64> {
    let (bid, ask) = (positive(bid)?, positive(ask)?);
    // Halving first avoids overflow for two individually finite quotes.
    (ask >= bid).then_some(bid / 2.0 + ask / 2.0)
}

pub(crate) fn cost_reference(
    bid: Option<f64>,
    ask: Option<f64>,
    last: Option<f64>,
) -> (Option<f64>, Option<&'static str>) {
    if let Some(value) = mid(bid, ask) {
        (Some(value), Some("mid"))
    } else if let Some(value) = positive(last) {
        (Some(value), Some("last"))
    } else {
        (None, None)
    }
}

pub(crate) fn replay_reference(
    bid: Option<f64>,
    ask: Option<f64>,
    last: Option<f64>,
    book_valid: Option<bool>,
) -> Option<f64> {
    if book_valid == Some(false) {
        return None;
    }
    positive(last).or_else(|| mid(bid, ask))
}

/// Frozen sizing evidence; zero targets do not require a price or conversion.
pub(crate) struct Sizing<'a> {
    pub mode: Option<&'a str>,
    pub status: Option<&'a str>,
    pub quantity: Option<f64>,
    pub equity: Option<f64>,
    pub notional: Option<f64>,
    pub reference: Option<f64>,
    pub multiplier: Option<f64>,
    pub conversion: Option<Option<f64>>,
}

impl Sizing<'_> {
    pub(crate) fn weight_and_units(&self) -> Option<(f64, f64)> {
        if self.mode.unwrap_or("weight") != "weight" || self.status != Some("SIZED") {
            return None;
        }
        let equity = positive(self.equity)?;
        let quantity = self.quantity.filter(|value| value.is_finite())?;
        if quantity == 0.0 {
            return Some((0.0, 0.0));
        }
        let notional = positive(self.notional)?;
        let multiplier = match positive(self.reference) {
            Some(reference) => notional / reference,
            None => positive(self.multiplier)? * positive(self.conversion.unwrap_or(Some(1.0)))?,
        };
        let weight = quantity * notional / equity;
        let units = quantity * multiplier;
        (weight.is_finite() && units.is_finite()).then_some((weight, units))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cost_and_replay_keep_distinct_reference_priorities() {
        assert_eq!(
            cost_reference(Some(99.0), Some(101.0), Some(103.0)),
            (Some(100.0), Some("mid"))
        );
        assert_eq!(
            replay_reference(Some(99.0), Some(101.0), Some(103.0), None),
            Some(103.0)
        );
        assert_eq!(
            cost_reference(Some(101.0), Some(99.0), Some(103.0)),
            (Some(103.0), Some("last"))
        );
        assert_eq!(
            replay_reference(Some(99.0), Some(101.0), None, None),
            Some(100.0)
        );
    }

    #[test]
    fn sizing_preserves_fx_and_zero_target_without_price() {
        let mut sizing = Sizing {
            mode: None,
            status: Some("SIZED"),
            quantity: Some(2.0),
            equity: Some(1000.0),
            notional: Some(200.0),
            reference: Some(10.0),
            multiplier: Some(2.0),
            conversion: Some(Some(10.0)),
        };
        assert_eq!(sizing.weight_and_units(), Some((0.4, 40.0)));
        sizing.reference = None;
        assert_eq!(sizing.weight_and_units(), Some((0.4, 40.0)));
        sizing.conversion = Some(None);
        assert_eq!(sizing.weight_and_units(), None);
        sizing.quantity = Some(0.0);
        sizing.notional = None;
        assert_eq!(sizing.weight_and_units(), Some((0.0, 0.0)));
        sizing.quantity = None;
        assert_eq!(sizing.weight_and_units(), None);
    }

    #[test]
    fn missing_invalid_and_zero_prices_are_not_valuations() {
        for value in [
            None,
            Some(0.0),
            Some(-1.0),
            Some(f64::NAN),
            Some(f64::INFINITY),
        ] {
            assert_eq!(cost_reference(value, value, value), (None, None));
            assert_eq!(replay_reference(value, value, value, None), None);
        }
        assert_eq!(mid(Some(f64::MAX), Some(f64::MAX)), Some(f64::MAX));
        assert_eq!(
            replay_reference(Some(99.0), Some(101.0), Some(100.0), Some(false)),
            None
        );
    }
}
