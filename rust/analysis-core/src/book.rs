//! Time-weighted statistics over valid recorded intervals, separate from heat pixels.
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, HashMap, HashSet};

#[derive(Deserialize)]
pub struct Level {
    pub price: f64,
    pub quantity: Option<f64>,
}
#[derive(Deserialize)]
pub struct Depth {
    pub start: f64,
    pub end: f64,
    pub bids: Vec<Level>,
    pub asks: Vec<Level>,
    pub available_depth: usize,
}
#[derive(Deserialize)]
pub struct Trade {
    pub recording: String,
    pub key: Option<String>,
    pub time: f64,
    pub price: Option<f64>,
    pub quantity: Option<f64>,
    pub stable: bool,
    pub conflict: bool,
}
#[derive(Deserialize)]
pub struct Book {
    pub depth: Vec<Depth>,
    pub market: Vec<Trade>,
    pub own: Vec<Trade>,
    pub tick: Option<f64>,
}
#[derive(Deserialize)]
pub struct Query {
    pub start: f64,
    pub end: f64,
    pub levels: usize,
    pub tick: Option<f64>,
}
#[derive(Serialize)]
pub struct Volume {
    pub count: usize,
    pub omitted: usize,
    pub quantity: f64,
    pub vwap: Option<f64>,
    pub profile: Vec<(f64, f64)>,
    pub bin_width: Option<f64>,
}
#[derive(Serialize)]
pub struct Stats {
    pub spread: Option<f64>,
    pub spread_coverage: f64,
    pub imbalance: Option<f64>,
    pub imbalance_coverage: f64,
    pub market: Volume,
    pub own: Volume,
}
impl Book {
    pub fn validate(&self) -> Result<(), String> {
        if self.depth.len() > 150_000
            || self.market.len() + self.own.len() > 150_000
            || self.depth.iter().any(|d| {
                !d.start.is_finite()
                    || !d.end.is_finite()
                    || d.start > d.end
                    || d.bids.iter().chain(&d.asks).any(|l| {
                        !l.price.is_finite()
                            || l.price <= 0.0
                            || l.quantity.is_some_and(|q| !q.is_finite() || q < 0.0)
                    })
            })
        {
            return Err("回放数据无效或超过本地分析上限".into());
        }
        // Overlapping recording intervals would count elapsed time twice.
        if self.depth.windows(2).any(|w| w[0].end > w[1].start) {
            return Err("录制段盘口区间重叠，无法统计时间权重".into());
        }
        Ok(())
    }
    pub fn query(&self, q: Query) -> Result<Stats, String> {
        if !q.start.is_finite()
            || !q.end.is_finite()
            || q.start >= q.end
            || !(1..=20).contains(&q.levels)
        {
            return Err("请选择非空回放区间及 1–20 档深度".into());
        }
        let mut spread_sum = 0.0;
        let mut spread_time = 0.0;
        let mut imbalance_sum = 0.0;
        let mut imbalance_time = 0.0;
        for d in &self.depth {
            let duration = d.end.min(q.end) - d.start.max(q.start);
            if duration <= 0.0 {
                continue;
            }
            let (Some(bid), Some(ask)) = (d.bids.first(), d.asks.first()) else {
                continue;
            };
            if ask.price < bid.price {
                continue;
            }
            spread_sum += (ask.price - bid.price) * duration;
            spread_time += duration;
            if d.available_depth < q.levels || d.bids.len() < q.levels || d.asks.len() < q.levels {
                continue;
            }
            let bids: Option<f64> = d.bids[..q.levels].iter().map(|l| l.quantity).sum();
            let asks: Option<f64> = d.asks[..q.levels].iter().map(|l| l.quantity).sum();
            if let Some((bid, ask)) = bids.zip(asks).filter(|(b, a)| b + a > 0.0) {
                imbalance_sum += (bid - ask) / (bid + ask) * duration;
                imbalance_time += duration;
            }
        }
        Ok(Stats {
            spread: (spread_time > 0.0).then(|| spread_sum / spread_time),
            spread_coverage: spread_time / (q.end - q.start),
            imbalance: (imbalance_time > 0.0).then(|| imbalance_sum / imbalance_time),
            imbalance_coverage: imbalance_time / (q.end - q.start),
            market: volume(&self.market, &q, q.tick.or(self.tick)),
            own: volume(&self.own, &q, q.tick.or(self.tick)),
        })
    }
}
fn volume(trades: &[Trade], q: &Query, tick: Option<f64>) -> Volume {
    let mut result = Volume {
        count: 0,
        omitted: 0,
        quantity: 0.0,
        vwap: None,
        profile: Vec::new(),
        bin_width: None,
    };
    let mut seen = HashSet::new();
    let mut identities = HashMap::new();
    let mut conflicts = HashSet::new();
    for t in trades {
        if let Some(key) = &t.key {
            let identity = (&t.recording, key);
            let value = (t.price, t.quantity);
            if t.conflict
                || identities
                    .insert(identity, value)
                    .is_some_and(|old| old != value)
            {
                conflicts.insert(identity);
            }
        }
    }
    let mut included = Vec::new();
    let mut value = 0.0;
    for t in trades {
        if t.time < q.start || t.time >= q.end {
            continue;
        }
        let valid = t
            .price
            .zip(t.quantity)
            .filter(|(p, q)| p.is_finite() && *p > 0.0 && q.is_finite() && *q > 0.0);
        if !t.stable
            || t.key
                .as_ref()
                .is_none_or(|key| conflicts.contains(&(&t.recording, key)))
            || valid.is_none()
        {
            result.omitted += 1;
            continue;
        }
        if !seen.insert((&t.recording, t.key.as_ref().unwrap())) {
            continue;
        }
        let (price, quantity) = valid.unwrap();
        result.count += 1;
        result.quantity += quantity;
        value += price * quantity;
        included.push((price, quantity));
    }
    result.vwap = (result.quantity > 0.0).then(|| value / result.quantity);
    if let Some(tick) = tick.filter(|t| t.is_finite() && *t > 0.0) {
        let indexed: Option<Vec<_>> = included
            .into_iter()
            .map(|(price, quantity)| {
                let ratio = price / tick;
                let nearest = ratio.round();
                // Prices on a tick can land just below an integer after division.
                let index = if (ratio - nearest).abs() <= 4.0 * f64::EPSILON * ratio.abs().max(1.0)
                {
                    nearest
                } else {
                    ratio.floor()
                };
                (index.is_finite() && index.abs() <= 9007199254740991.0)
                    .then_some((index as i64, quantity))
            })
            .collect();
        if let Some(indexed) = indexed {
            let min = indexed.iter().map(|(i, _)| *i).min();
            let max = indexed.iter().map(|(i, _)| *i).max();
            if let Some((min, max)) = min.zip(max) {
                let width = (((max - min + 1) as f64 / 128.0).ceil() as i64).max(1);
                let mut bins = BTreeMap::<i64, f64>::new();
                for (index, quantity) in indexed {
                    *bins.entry((index - min) / width).or_default() += quantity;
                }
                result.profile = bins
                    .into_iter()
                    .map(|(i, q)| ((min + i * width) as f64 * tick, q))
                    .collect();
                result.bin_width = Some(width as f64 * tick);
            }
        }
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn unknown_quantity_and_gaps_reduce_coverage_without_becoming_zero() {
        let book:Book=serde_json::from_str(r#"{"tick":1,"market":[],"own":[],"depth":[
            {"start":0,"end":10,"bids":[{"price":100,"quantity":3}],"asks":[{"price":102,"quantity":1}],"available_depth":1},
            {"start":20,"end":30,"bids":[{"price":100,"quantity":null}],"asks":[{"price":104,"quantity":1}],"available_depth":1}]}"#).unwrap();
        book.validate().unwrap();
        let s = book
            .query(Query {
                start: 0.0,
                end: 40.0,
                levels: 1,
                tick: None,
            })
            .unwrap();
        assert_eq!(s.spread, Some(3.0));
        assert_eq!(s.spread_coverage, 0.5);
        assert_eq!(s.imbalance, Some(0.5));
        assert_eq!(s.imbalance_coverage, 0.25);
        assert_eq!(
            book.query(Query {
                start: 0.0,
                end: 40.0,
                levels: 5,
                tick: None
            })
            .unwrap()
            .imbalance,
            None
        );
    }
    #[test]
    fn trades_keep_recording_identity_exclude_conflicts_and_use_half_open_interval() {
        let t = |recording: &str, time| Trade {
            recording: recording.into(),
            key: Some("1".into()),
            time,
            price: Some(100.0),
            quantity: Some(2.0),
            stable: true,
            conflict: false,
        };
        let mut conflict = t("a", 1.0);
        conflict.key = Some("bad".into());
        conflict.conflict = true;
        let s = volume(
            &[t("a", 0.0), t("a", 0.0), t("b", 1.0), conflict, t("c", 2.0)],
            &Query {
                start: 0.0,
                end: 2.0,
                levels: 1,
                tick: None,
            },
            Some(1.0),
        );
        assert_eq!(s.count, 2);
        assert_eq!(s.quantity, 4.0);
        assert_eq!(s.omitted, 1);
        assert_eq!(s.vwap, Some(100.0));
    }

    #[test]
    fn late_conflict_invalidates_the_whole_identity() {
        let t = |price| Trade {
            recording: "r".into(),
            key: Some("a".into()),
            time: 1.0,
            price: Some(price),
            quantity: Some(1.0),
            stable: true,
            conflict: false,
        };
        let s = volume(
            &[t(100.0), t(101.0)],
            &Query {
                start: 0.0,
                end: 2.0,
                levels: 1,
                tick: None,
            },
            None,
        );
        assert_eq!(s.count, 0);
        assert_eq!(s.omitted, 2);
        assert_eq!(s.vwap, None);
    }

    #[test]
    fn fractional_price_ticks_do_not_merge_adjacent_bins() {
        let trades: Vec<_> = (0..30)
            .map(|i| Trade {
                recording: "r".into(),
                key: Some(i.to_string()),
                time: 1.0,
                price: Some(100.2 + i as f64 * 0.01),
                quantity: Some(1.0),
                stable: true,
                conflict: false,
            })
            .collect();
        let s = volume(
            &trades,
            &Query {
                start: 0.0,
                end: 2.0,
                levels: 1,
                tick: None,
            },
            Some(0.01),
        );
        assert_eq!(s.profile.len(), 30);
        assert!(s.profile.iter().all(|(_, q)| *q == 1.0));
        assert_eq!(s.profile.iter().map(|(_, q)| q).sum::<f64>(), 30.0);
    }
}
