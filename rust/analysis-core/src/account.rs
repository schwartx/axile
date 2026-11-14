//! Resident, bounded facts for interactive cost queries and hypothetical fees.
use crate::{
    cost::{CostInput, Summary},
    equity::{self, Observation},
    trade_value, ALGORITHM, PROTOCOL,
};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::{BTreeMap, VecDeque};

#[derive(Serialize, Deserialize)]
pub struct RawTrade {
    pub symbol: String,
    pub time_us: i64,
    pub day: String,
    pub estimated: bool,
    pub side: String,
    pub price: Option<f64>,
    pub quantity: Option<f64>,
    pub multiplier: Option<f64>,
    pub reference: Option<f64>,
    pub fee: Option<f64>,
    pub fee_currency: Option<String>,
}
#[derive(Serialize, Deserialize)]
pub struct Record {
    pub record_id: String,
    pub observation: Option<Observation>,
    pub trades: Vec<RawTrade>,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum InputKind {
    Costs,
    Fees,
}

#[derive(Deserialize)]
pub struct Inputs {
    pub protocol: u32,
    pub kind: InputKind,
    pub algorithm: String,
    pub initial_equity: Option<f64>,
    pub records: Vec<Record>,
}
#[derive(Default, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Query {
    pub start: Option<f64>,
    pub end: Option<f64>,
    pub day: Option<String>,
    pub record_id: Option<String>,
    pub symbol: Option<String>,
    pub side: Option<String>,
    pub fee_rate: Option<f64>,
}
struct Trade {
    record: String,
    raw: RawTrade,
    value: Option<f64>,
    cost: Option<f64>,
    loss: Option<f64>,
}
impl Query {
    fn matches(&self, trade: &Trade) -> bool {
        let raw = &trade.raw;
        let time = raw.time_us as f64 / 1000.0;
        !self
            .start
            .zip(self.end)
            .is_some_and(|(a, b)| time <= a || time > b)
            && self.day.as_ref().is_none_or(|day| day == &raw.day)
            && self.record_id.as_ref().is_none_or(|id| id == &trade.record)
            && self
                .symbol
                .as_ref()
                .is_none_or(|symbol| symbol == &raw.symbol)
            && self.side.as_ref().is_none_or(|side| side == &raw.side)
    }
}

pub struct Account {
    kind: InputKind,
    fee_cache: VecDeque<(u64, Value)>,
    equity: Option<f64>,
    observations: Vec<Observation>,
    trades: Vec<Trade>,
}
impl Account {
    pub fn new(input: Inputs) -> Result<Self, String> {
        if input.protocol != PROTOCOL || input.algorithm != ALGORITHM {
            return Err("分析版本不匹配，请刷新页面".into());
        }
        if input.records.len() > 20_000
            || input.records.iter().map(|r| r.trades.len()).sum::<usize>() > 150_000
        {
            return Err("窗口超过本地分析上限，请缩短范围".into());
        }
        let mut observations = Vec::new();
        let mut trades = Vec::new();
        for record in input.records {
            if (input.kind == InputKind::Costs && record.observation.is_some())
                || (input.kind == InputKind::Fees && !record.trades.is_empty())
            {
                return Err("分析输入类型不匹配".into());
            }
            if let Some(item) = record.observation {
                if observations
                    .last()
                    .is_some_and(|last: &Observation| last.time >= item.time)
                {
                    return Err("观测顺序无效，请重新读取".into());
                }
                observations.push(item);
            }
            for raw in record.trades {
                let (value, cost, loss) = trade_value(
                    raw.price,
                    raw.quantity,
                    raw.multiplier,
                    raw.reference,
                    &raw.side,
                );
                trades.push(Trade {
                    record: record.record_id.clone(),
                    raw,
                    value,
                    cost,
                    loss,
                });
            }
        }
        Ok(Self {
            kind: input.kind,
            fee_cache: VecDeque::new(),
            equity: input.initial_equity,
            observations,
            trades,
        })
    }
    pub fn query(&mut self, q: Query) -> Result<Value, String> {
        if self.kind == InputKind::Fees {
            if q.start.is_some()
                || q.end.is_some()
                || q.day.is_some()
                || q.record_id.is_some()
                || q.symbol.is_some()
                || q.side.is_some()
            {
                return Err("费率试算不接受成本筛选条件".into());
            }
            return self.preview(q.fee_rate.ok_or("fee_rate is required")?);
        }
        if q.fee_rate.is_some() {
            return Err("请先加载费率试算观测".into());
        }
        self.costs(&q)
    }

    fn costs(&self, q: &Query) -> Result<Value, String> {
        if q.start.is_some() != q.end.is_some()
            || q.start
                .zip(q.end)
                .is_some_and(|(a, b)| !a.is_finite() || !b.is_finite() || a >= b)
            || q.day.is_some() && q.start.is_some()
        {
            return Err("成本区间必须为 (起点, 终点]".into());
        }
        let mut summary = Summary::empty();
        let mut symbols = BTreeMap::<&str, Summary>::new();
        let mut histogram = [0usize; 7];
        let mut unknown = 0usize;
        for t in &self.trades {
            let raw = &t.raw;
            if !q.matches(t) {
                continue;
            }
            let input = CostInput {
                value: t.value,
                cost: t.cost,
                loss_bp: t.loss,
                fee: raw.fee,
                fee_currency: raw.fee_currency.as_deref(),
                estimated: raw.estimated,
            };
            summary.push(&input);
            symbols
                .entry(&raw.symbol)
                .or_insert_with(Summary::empty)
                .push(&input);
            if let Some(loss) = t.loss {
                histogram
                    [[-10.0, -5.0, -1.0, 1.0, 5.0, 10.0].partition_point(|edge| loss >= *edge)] +=
                    1;
            } else {
                unknown += 1;
            }
        }
        Ok(
            json!({"summary": summary.output(), "symbols": symbols.into_iter().map(|(symbol,s)| json!({"symbol":symbol,"summary":s.output()})).collect::<Vec<_>>(), "histogram": histogram, "unknown": unknown}),
        )
    }

    fn preview(&mut self, fee: f64) -> Result<Value, String> {
        if !fee.is_finite() || !(0.0..1.0).contains(&fee) {
            return Err("费率须大于等于 0 且小于 10000 BP".into());
        }
        let key = fee.to_bits();
        if let Some((_, value)) = self.fee_cache.iter().find(|(rate, _)| *rate == key) {
            return Ok(value.clone());
        }
        let preview = self.equity.ok_or_else(|| "此窗口没有有效的回测权益基线".to_string()).and_then(|equity| {
            Ok(json!({"sized": equity::preview(&self.observations, equity, fee, false)?, "target": equity::preview(&self.observations, equity, fee, true)?}))
        });
        let result = match preview {
            Ok(value) => json!({"preview":value,"preview_error":null}),
            Err(error) => json!({"preview":null,"preview_error":error}),
        };
        // Each resident window keeps at most four rates, including failed trials.
        if self.fee_cache.len() == 4 {
            self.fee_cache.pop_front();
        }
        self.fee_cache.push_back((key, result.clone()));
        Ok(result)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn complete_cost_window_keeps_open_closed_bounds_and_missing_values() {
        let input = json!({"protocol":PROTOCOL,"kind":"costs","algorithm":ALGORITHM,"initial_equity":null,"records":[{"record_id":"9","observation":null,"trades":[
            {"symbol":"A","time_us":1000,"day":"2026-01-01","estimated":false,"side":"buy","price":101,"quantity":2,"multiplier":10,"reference":100,"fee":1,"fee_currency":"CNY"},
            {"symbol":"B","time_us":2000,"day":"2026-01-01","estimated":true,"side":"none","price":null,"quantity":2,"multiplier":null,"reference":null,"fee":-1,"fee_currency":"USD"}
        ]}]});
        let mut account = Account::new(serde_json::from_value(input).unwrap()).unwrap();
        let all = account.query(Query::default()).unwrap();
        assert_eq!(all["summary"]["count"], 2);
        assert_eq!(all["summary"]["cost"], 20.0);
        assert_eq!(all["summary"]["coverage"], Value::Null);
        assert_eq!(all["summary"]["fees"]["USD"], -1.0);
        let result = account
            .query(Query {
                start: Some(1.0),
                end: Some(2.0),
                ..Default::default()
            })
            .unwrap();
        assert_eq!(result["summary"]["count"], 1);
        assert_eq!(result["summary"]["value"], Value::Null);
        assert_eq!(result["unknown"], 1);
    }

    #[test]
    fn fee_cache_reuses_resident_results_and_has_a_fixed_bound() {
        let mut account = Account::new(serde_json::from_value(json!({"protocol":PROTOCOL,"kind":"fees","algorithm":ALGORITHM,"initial_equity":100,"records":[]})).unwrap()).unwrap();
        let q = |fee| Query {
            fee_rate: Some(fee),
            ..Default::default()
        };
        let first = account.query(q(0.001)).unwrap();
        // Changing the private baseline proves a cache hit does not run the engine again.
        account.equity = None;
        assert_eq!(account.query(q(0.001)).unwrap(), first);
        assert!(account.query(q(0.002)).unwrap()["preview_error"].is_string());
        for rate in [0.003, 0.004, 0.005] {
            account.query(q(rate)).unwrap();
        }
        assert_eq!(account.fee_cache.len(), 4);
        assert!(!account
            .fee_cache
            .iter()
            .any(|(rate, _)| *rate == 0.001f64.to_bits()));
        assert!(account
            .query(Query {
                start: Some(0.0),
                end: Some(1.0),
                ..q(0.005)
            })
            .is_err());
    }

    #[test]
    fn cost_facts_cannot_silently_produce_a_fee_preview() {
        let mut account=Account::new(serde_json::from_value(json!({"protocol":PROTOCOL,"kind":"costs","algorithm":ALGORITHM,"initial_equity":null,"records":[]})).unwrap()).unwrap();
        assert!(account
            .query(Query {
                fee_rate: Some(0.001),
                ..Default::default()
            })
            .is_err());
    }
}
