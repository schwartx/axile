//! 成交成本与费用聚合，计算和读取共用相同规则。

use serde_json::{json, Map, Value};
use std::collections::BTreeMap;

pub struct CostInput<'a> {
    pub value: Option<f64>,
    pub cost: Option<f64>,
    pub loss_bp: Option<f64>,
    pub fee: Option<f64>,
    pub fee_currency: Option<&'a str>,
    pub estimated: bool,
}
pub trait CostSource {
    fn cost_input(&self) -> CostInput<'_>;
}
impl CostSource for CostInput<'_> {
    fn cost_input(&self) -> CostInput<'_> {
        CostInput {
            value: self.value,
            cost: self.cost,
            loss_bp: self.loss_bp,
            fee: self.fee,
            fee_currency: self.fee_currency,
            estimated: self.estimated,
        }
    }
}

#[derive(Debug)]
pub struct Summary {
    pub count: i64,
    pub valued: i64,
    pub value: Option<f64>,
    pub cost: Option<f64>,
    pub covered: i64,
    pub covered_value: Option<f64>,
    covered_value_json: Value,
    weighted_loss: Option<f64>,
    pub estimated: i64,
    pub fees: BTreeMap<String, (f64, i64)>,
    pub fee_count: i64,
}
impl Summary {
    pub fn empty() -> Self {
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

    pub fn push(&mut self, source: &impl CostSource) {
        let trade = source.cost_input();
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
        if let (Some(fee), Some(currency)) = (trade.fee, trade.fee_currency) {
            self.add_fee(currency, fee, 1);
        }
    }

    pub fn add_fee(&mut self, currency: &str, amount: f64, count: i64) {
        if let Some(fee) = self.fees.get_mut(currency) {
            fee.0 += amount;
            fee.1 += count;
        } else {
            self.fees.insert(currency.to_owned(), (amount, count));
        }
        self.fee_count += count;
    }

    pub fn amount_complete(&self) -> bool {
        self.count == self.valued
    }

    pub fn loss_bp(&self) -> Option<f64> {
        self.covered_value
            .filter(|value| *value != 0.0)
            .and_then(|value| self.weighted_loss.map(|weighted| weighted / value))
    }

    pub fn coverage(&self) -> Option<f64> {
        self.value
            .filter(|value| self.amount_complete() && *value != 0.0)
            .and_then(|value| self.covered_value.map(|covered| covered / value))
    }

    pub fn output(&self) -> Value {
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
