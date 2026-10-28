//! 成交成本与费用聚合，计算和读取共用相同规则。

use serde_json::{json, Map, Value};
use std::collections::BTreeMap;

#[derive(Debug)]
pub(crate) struct Summary {
    pub(crate) count: i64,
    pub(crate) valued: i64,
    pub(crate) value: Option<f64>,
    pub(crate) cost: Option<f64>,
    pub(crate) covered: i64,
    pub(crate) covered_value: Option<f64>,
    covered_value_json: Value,
    weighted_loss: Option<f64>,
    pub(crate) estimated: i64,
    pub(crate) fees: BTreeMap<String, (f64, i64)>,
    pub(crate) fee_count: i64,
}
impl Summary {
    pub(crate) fn empty() -> Self {
        Self {
            count: 0,
            valued: 0,
            value: None,
            cost: None,
            covered: 0,
            covered_value: Some(0.0),
            covered_value_json: json!(0),
            weighted_loss: Some(0.0),
            estimated: 0,
            fees: BTreeMap::new(),
            fee_count: 0,
        }
    }

    pub(crate) fn push(&mut self, trade: &crate::fact_trades::Trade) {
        self.count += 1;
        self.estimated += i64::from(trade.estimated);
        if let Some(value) = trade.value {
            self.valued += 1;
            *self.value.get_or_insert(0.0) += value;
            if let Some(cost) = trade.cost {
                *self.cost.get_or_insert(0.0) += cost;
                self.covered += 1;
                *self.covered_value.get_or_insert(0.0) += value;
                self.covered_value_json = json!(self.covered_value);
                *self.weighted_loss.get_or_insert(0.0) += trade.loss_bp.unwrap_or(0.0) * value;
            }
        }
        if let (Some(fee), Some(currency)) = (trade.fee, trade.fee_currency.as_deref()) {
            self.add_fee(currency, fee, 1);
        }
    }

    pub(crate) fn add_fee(&mut self, currency: &str, amount: f64, count: i64) {
        let fee = self.fees.entry(currency.to_owned()).or_default();
        fee.0 += amount;
        fee.1 += count;
        self.fee_count += count;
    }

    pub(crate) fn amount_complete(&self) -> bool {
        self.count == self.valued
    }

    pub(crate) fn loss_bp(&self) -> Option<f64> {
        self.covered_value
            .filter(|value| *value != 0.0)
            .and_then(|value| self.weighted_loss.map(|weighted| weighted / value))
    }

    pub(crate) fn coverage(&self) -> Option<f64> {
        self.value
            .filter(|value| self.amount_complete() && *value != 0.0)
            .and_then(|value| self.covered_value.map(|covered| covered / value))
    }

    pub(crate) fn output(&self) -> Value {
        let fees: Map<_, _> = self
            .fees
            .iter()
            .map(|(currency, (amount, _))| (currency.clone(), json!(amount)))
            .collect();
        json!({
            "value": self.value, "cost": self.cost, "lossBp": self.loss_bp(), "coverage": self.coverage(),
            "covered": self.covered, "coveredValue": self.covered_value_json, "count": self.count,
            "amountComplete": self.amount_complete(), "fees": fees, "feeCovered": self.fee_count, "estimated": self.estimated,
        })
    }
}
