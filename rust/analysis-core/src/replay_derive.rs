//! Fixed-size business reducers. Only scalar contributions survive an identity batch.
use super::{known, lower, number, text, DEPENDENCY_SLOTS, POSITION_FIELDS, SLOTS};
use serde_json::{json, Value};

pub fn order_facts(trades: &[Value], orders: &[Value]) -> Vec<Value> {
    orders
        .iter()
        .map(|order| {
            let key: Value =
                serde_json::from_str(order["key"].as_str().unwrap_or("")).unwrap_or(Value::Null);
            let mut quantity = 0.0;
            let mut last = 0;
            let mut complete = true;
            for trade in trades {
                if trade["first_seq"] == 0 || trade["resolved_seq"].as_u64().unwrap_or(0) > 0 {
                    continue;
                }
                let d = &trade["data"];
                if trade["conflict"] != true
                    && (text(d, "symbol").is_some_and(|s| key[0] != s)
                        || text(d, "order_id").is_some_and(|s| key[1] != s))
                {
                    continue;
                }
                last = last.max(trade["last_fact_seq"].as_u64().unwrap_or(0));
                let valid = trade["kind"] == "trade"
                    && trade["valid"] == true
                    && text(d, "symbol").is_some()
                    && text(d, "order_id").is_some();
                if valid {
                    quantity += number(d, "quantity").unwrap();
                } else {
                    complete = false;
                }
            }
            if !quantity.is_finite() {
                complete = false;
            }
            json!({"quantity":quantity,"last_fact_seq":last,"complete":complete})
        })
        .collect()
}

pub struct Derivation {
    mode: String,
    targets: Vec<Value>,
    values: Vec<Value>,
    execution: String,
    symbol: String,
    start: u64,
    end: u64,
    stats: Value,
}
impl Derivation {
    pub fn new(request: &Value) -> Result<Self, String> {
        let mode = request["mode"].as_str().ok_or("REPLAY_DERIVE_MODE")?;
        let targets = request["targets"].as_array().cloned().unwrap_or_default();
        if targets.len()
            > if mode == "owner" {
                DEPENDENCY_SLOTS
            } else {
                SLOTS
            }
            || !matches!(mode, "owner" | "position" | "statistics" | "fences")
        {
            return Err("REPLAY_DERIVE_BUDGET".into());
        }
        let symbol = request["scope_symbol"].as_str().unwrap_or("");
        let start = request["interval_start_us"].as_u64().unwrap_or(0);
        let end = request["interval_end_us"]
            .as_u64()
            .unwrap_or(9_007_199_254_740_991);
        if mode == "statistics"
            && (symbol.is_empty()
                || symbol.len() > 4096
                || start >= end
                || end > 9_007_199_254_740_991)
        {
            return Err("REPLAY_STATS_SCOPE".into());
        }
        if mode == "fences"
            && request["cutoffs"].as_array().is_none_or(|a| {
                a.is_empty()
                    || a.len() > 128
                    || a.iter()
                        .any(|s| s.as_u64().is_none_or(|n| n > 9_007_199_254_740_991))
                    || a.windows(2).any(|w| w[0].as_u64() >= w[1].as_u64())
            })
        {
            return Err("REPLAY_FENCES_BUDGET".into());
        }
        let values=targets.iter().map(|t|match mode {
            "owner"=>json!({"execution_id":null,"seq":0,"conflict":false,"cancel_pending":false}),
            "position"=>{
                let mut fields=json!({});
                for field in POSITION_FIELDS {
                    let p=&t["proofs"][*field];
                    fields[*field]=json!({"value":number(&t["data"],field),"complete":p["valid"]==true && number(&t["data"],field).is_some(),"changed":false,"delta":0.0,"compensation":0.0});
                } fields
            },
            "fences"=>json!(request["cutoffs"].as_array().unwrap_or(&Vec::new()).iter().map(|seq|json!({"seq":seq,"quantity":0.0,"last_fact_seq":0,"complete":true})).collect::<Vec<_>>()),
            _=>json!({}),
        }).collect();
        Ok(Self {
            mode: mode.into(),
            targets,
            values,
            execution: request["execution"].as_str().unwrap_or("").into(),
            symbol: symbol.into(),
            start,
            end,
            stats: json!({"known_quantity":0.0,"known_amount":0.0,"stable_count":0,"weak_count":0,"unknown_count":0,"unowned_count":0,"complete":request["start_observed"]==true}),
        })
    }
    pub fn feed(&mut self, request: &Value) -> Result<(), String> {
        let rows = request["subjects"]
            .as_array()
            .ok_or("REPLAY_DERIVE_INPUT")?;
        if rows.len() > DEPENDENCY_SLOTS {
            return Err("REPLAY_DERIVE_BUDGET".into());
        }
        match self.mode.as_str() {
            "owner" => self.commands(rows),
            "position" => self.positions(rows, &request["members"]),
            "statistics" => self.statistics(rows),
            "fences" => {
                let fences = request["fences"].as_array().ok_or("REPLAY_FENCES")?;
                if fences.len() > 128 {
                    return Err("REPLAY_FENCES_BUDGET".into());
                }
                for (i, out) in self.values.iter_mut().enumerate() {
                    let out = out.as_array_mut().ok_or("REPLAY_FENCES_STATE")?;
                    if out.is_empty() {
                        for f in fences {
                            out.push(json!({"seq":f["seq"],"quantity":0.0,"last_fact_seq":0,"complete":true}));
                        }
                    }
                    if out.len() != fences.len() {
                        return Err("REPLAY_FENCES_CONTEXT".into());
                    }
                    for (sum, f) in out.iter_mut().zip(fences) {
                        if sum["seq"] != f["seq"] {
                            return Err("REPLAY_FENCES_CONTEXT".into());
                        }
                        let next = &f["orders"][i];
                        sum["quantity"] = json!(
                            sum["quantity"].as_f64().unwrap_or(0.0)
                                + next["quantity"].as_f64().ok_or("REPLAY_FENCES_QUANTITY")?
                        );
                        sum["last_fact_seq"] =
                            json!(sum["last_fact_seq"].as_u64().unwrap_or(0).max(
                                next["last_fact_seq"]
                                    .as_u64()
                                    .ok_or("REPLAY_FENCES_SEQUENCE")?
                            ));
                        sum["complete"] = json!(
                            sum["complete"] == true
                                && next["complete"] == true
                                && sum["quantity"].as_f64().is_some_and(f64::is_finite)
                        );
                    }
                }
            }
            _ => unreachable!(),
        }
        Ok(())
    }
    fn commands(&mut self, commands: &[Value]) {
        for (target, owner) in self.targets.iter().zip(&mut self.values) {
            let d = &target["data"];
            let key: Value =
                serde_json::from_str(target["key"].as_str().unwrap_or("")).unwrap_or(Value::Null);
            let symbol = if target["kind"] == "order" {
                key[0].as_str()
            } else {
                text(d, "symbol")
            };
            let order = if target["kind"] == "order" {
                key[1].as_str()
            } else {
                text(d, "order_id")
            };
            for command in commands {
                let c = &command["data"];
                if symbol.is_none()
                    || order.is_none()
                    || text(c, "symbol") != symbol
                    || text(c, "order_id") != order
                {
                    continue;
                }
                if lower(c, "operation").as_deref() == Some("cancel_order") {
                    let pending = c["success"] != false
                        && c["accepted"] != false
                        && matches!(
                            lower(c, "phase").as_deref(),
                            Some(
                                "request"
                                    | "returned"
                                    | "result"
                                    | "native_request"
                                    | "native_return"
                                    | "native_returned"
                            )
                        );
                    owner["cancel_pending"] = json!(owner["cancel_pending"] == true || pending);
                }
                if lower(c, "operation").as_deref() != Some("place_order") {
                    continue;
                }
                if command["owner_conflict"] == true {
                    owner["conflict"] = json!(true);
                    continue;
                }
                let Some(execution) = text(c, "execution_id") else {
                    continue;
                };
                if owner["execution_id"]
                    .as_str()
                    .is_some_and(|old| old != execution)
                {
                    owner["conflict"] = json!(true);
                }
                owner["execution_id"] = json!(execution);
                owner["seq"] = json!(owner["seq"]
                    .as_u64()
                    .unwrap_or(0)
                    .max(command["owner_seq"].as_u64().unwrap_or(0)));
            }
        }
    }
    fn positions(&mut self, trades: &[Value], members: &Value) {
        for (i, (target, fields)) in self.targets.iter().zip(&mut self.values).enumerate() {
            let symbol = text(&target["data"], "symbol");
            let side = lower(&target["data"], "position_side");
            for (j, trade) in trades.iter().enumerate() {
                if trade["first_seq"] == 0 || trade["resolved_seq"].as_u64().unwrap_or(0) > 0 {
                    continue;
                }
                let d = &trade["data"];
                if trade["conflict"] != true
                    && symbol.zip(text(d, "symbol")).is_some_and(|(a, b)| a != b)
                {
                    continue;
                }
                let ts = lower(d, "position_side");
                if side.as_deref() != Some("net")
                    && side.as_ref().zip(ts.as_ref()).is_some_and(|(a, b)| a != b)
                {
                    continue;
                }
                for field in POSITION_FIELDS {
                    let p = &target["proofs"][*field];
                    let state = &mut fields[*field];
                    if state["complete"] != true {
                        continue;
                    }
                    let coverage = match p["kind"].as_str() {
                        Some("included_trade_keys") => {
                            let response = &members[i][*field];
                            if response["members_count"] != p["members"]
                                || response["positions"]
                                    .as_array()
                                    .is_none_or(|a| a.len() != trades.len())
                            {
                                state["complete"] = json!(false);
                                continue;
                            }
                            let position = &response["positions"][j];
                            // Raw proof was validated by the baseline fold; metadata only locates its member.
                            if trade["kind"] == "trade"
                                && (position.is_null()
                                    || position
                                        .as_u64()
                                        .is_some_and(|n| n < p["members"].as_u64().unwrap_or(0)))
                            {
                                Some(!position.is_null())
                            } else {
                                None
                            }
                        }
                        Some("source_watermark")
                            if trade["kind"] == "trade"
                                && text(d, "source") == p["source"].as_str()
                                && known(d, "source_seq") =>
                        {
                            d["source_seq"]
                                .as_u64()
                                .zip(p["through"].as_u64())
                                .map(|(a, b)| a <= b)
                        }
                        _ => None,
                    };
                    if coverage == Some(true) {
                        continue;
                    }
                    if coverage.is_none()
                        || trade["kind"] != "trade"
                        || trade["valid"] != true
                        || symbol.is_none()
                        || text(d, "symbol").is_none()
                    {
                        state["complete"] = json!(false);
                        continue;
                    }
                    state["changed"] = json!(true);
                    let q = number(d, "quantity").unwrap();
                    let direction = lower(d, "direction");
                    let offset = lower(d, "offset");
                    let sign = if side.as_deref() != Some("net") && ts.is_none() {
                        None
                    } else {
                        match (side.as_deref(), direction.as_deref(), offset.as_deref()) {
                            (Some("net"), Some("buy"), _) => Some(1.0),
                            (Some("net"), Some("sell"), _) => Some(-1.0),
                            (Some("long"), Some("buy"), Some("open"))
                            | (Some("short"), Some("sell"), Some("open")) => Some(1.0),
                            (
                                Some("long"),
                                Some("sell"),
                                Some("close" | "close_today" | "close_yesterday"),
                            )
                            | (
                                Some("short"),
                                Some("buy"),
                                Some("close" | "close_today" | "close_yesterday"),
                            ) => Some(-1.0),
                            _ => None,
                        }
                    };
                    let delta = match *field {
                        "quantity" => sign.map(|s| s * q),
                        "today_quantity" => match offset.as_deref() {
                            Some("open" | "close_today") => sign.map(|s| s * q),
                            Some("close_yesterday") => Some(0.0),
                            _ => None,
                        },
                        "yesterday_quantity" => match offset.as_deref() {
                            Some("close_yesterday") => sign.map(|s| s * q),
                            Some("open" | "close_today") => Some(0.0),
                            _ => None,
                        },
                        _ => None,
                    };
                    if let Some(delta) = delta {
                        let sum = state["delta"].as_f64().unwrap_or(0.0);
                        let adjusted = delta - state["compensation"].as_f64().unwrap_or(0.0);
                        let next = sum + adjusted;
                        if next.is_finite() {
                            state["delta"] = json!(next);
                            state["compensation"] = json!((next - sum) - adjusted);
                        } else {
                            state["complete"] = json!(false);
                        }
                    } else {
                        state["complete"] = json!(false);
                    }
                }
            }
        }
    }
    fn statistics(&mut self, trades: &[Value]) {
        for trade in trades {
            if trade["first_seq"] == 0 || trade["resolved_seq"].as_u64().unwrap_or(0) > 0 {
                continue;
            }
            let d = &trade["data"];
            if text(d, "symbol").is_some_and(|s| s != self.symbol)
                || trade["first_time_us"]
                    .as_u64()
                    .is_some_and(|t| t < self.start || t >= self.end)
            {
                continue;
            }
            let owner = text(d, "execution_id");
            if owner.is_none()
                && trade["owner_conflict"] != true
                && matches!(lower(d, "origin").as_deref(), Some("manual" | "external"))
            {
                continue;
            }
            if owner.is_none() || trade["owner_conflict"] == true {
                self.stats["unowned_count"] =
                    json!(self.stats["unowned_count"].as_u64().unwrap() + 1);
                self.stats["complete"] = json!(false);
            }
            if owner.is_some_and(|o| o != self.execution) {
                continue;
            }
            let name = if trade["kind"] == "trade" {
                "stable_count"
            } else {
                "weak_count"
            };
            self.stats[name] = json!(self.stats[name].as_u64().unwrap() + 1);
            if trade["kind"] != "trade"
                || trade["valid"] != true
                || owner.is_none()
                || trade["owner_conflict"] == true
                || text(d, "symbol").is_none()
            {
                self.stats["unknown_count"] =
                    json!(self.stats["unknown_count"].as_u64().unwrap() + 1);
                self.stats["complete"] = json!(false);
                continue;
            }
            let q = number(d, "quantity").unwrap();
            let p = number(d, "price").unwrap();
            for (name, value) in [("known_quantity", q), ("known_amount", p * q)] {
                let Some(sum) = self.stats[name].as_f64() else {
                    continue;
                };
                let next = sum + value;
                if next.is_finite() {
                    self.stats[name] = json!(next);
                } else {
                    self.stats["complete"] = json!(false);
                    self.stats[name] = Value::Null;
                }
            }
        }
    }
    pub fn results(&self) -> Value {
        match self.mode.as_str() {
            "statistics" => {
                let mut s = self.stats.clone();
                s["symbol"] = json!(self.symbol);
                s["interval_start_us"] = json!(self.start);
                s["interval_end_us"] = json!(self.end);
                s["vwap"] = s["known_amount"]
                    .as_f64()
                    .zip(s["known_quantity"].as_f64().filter(|q| *q > 0.0))
                    .map(|(a, q)| a / q)
                    .filter(|v| v.is_finite())
                    .map_or(Value::Null, |v| json!(v));
                s
            }
            "fences" => Value::Array(self.values.clone()),
            "owner" => Value::Array(
                self.targets
                    .iter()
                    .zip(&self.values)
                    .map(|(target, owner)| {
                        let mut t = target.clone();
                        t["owner"] = owner.clone();
                        if owner["conflict"] == true {
                            t["owner_conflict"] = json!(true);
                            t["data"]["execution_id"] = Value::Null;
                        } else if text(&t["data"], "execution_id").is_none()
                            && !matches!(
                                lower(&t["data"], "origin").as_deref(),
                                Some("manual" | "external")
                            )
                        {
                            t["data"]["execution_id"] = owner["execution_id"].clone();
                            t["data"]["association_seq"] = owner["seq"].clone();
                        }
                        t
                    })
                    .collect(),
            ),
            "position" => Value::Array(
                self.targets
                    .iter()
                    .zip(&self.values)
                    .map(|(target, fields)| {
                        let mut t = target.clone();
                        t["baseline"] = json!({});
                        for field in POSITION_FIELDS {
                            t["baseline"][*field] = target["data"][*field].clone();
                        }
                        t["inference"] = fields.clone();
                        for field in POSITION_FIELDS {
                            let next = fields[*field]["value"]
                                .as_f64()
                                .zip(fields[*field]["delta"].as_f64())
                                .map(|(a, b)| a + b)
                                .filter(|v| {
                                    v.is_finite()
                                        && (lower(&target["data"], "position_side").as_deref()
                                            == Some("net")
                                            || *v >= 0.0)
                                });
                            let complete = fields[*field]["complete"] == true && next.is_some();
                            t["data"][*field] = if complete { json!(next) } else { Value::Null };
                            t["data"]["field_quality"][*field] =
                                json!(if complete { "known" } else { "missing" });
                            t["inference"][*field]["complete"] = json!(complete);
                            t["inference"][*field]["value"] = t["data"][*field].clone();
                        }
                        let q = number(&t["data"], "quantity");
                        let today = number(&t["data"], "today_quantity");
                        let yesterday = number(&t["data"], "yesterday_quantity");
                        let hedge = lower(&t["data"], "position_side").as_deref() != Some("net");
                        let inconsistent = q.is_some_and(|q| {
                            let tolerance = 32.0 * f64::EPSILON * q.abs().max(1.0);
                            today
                                .zip(yesterday)
                                .is_some_and(|(a, b)| (a + b - q).abs() > tolerance)
                                || hedge
                                    && [
                                        today,
                                        yesterday,
                                        number(&t["data"], "available_quantity"),
                                        number(&t["data"], "frozen_quantity"),
                                    ]
                                    .into_iter()
                                    .flatten()
                                    .any(|v| v > q + tolerance)
                        });
                        if inconsistent {
                            t["conflict"] = json!(true);
                            for field in POSITION_FIELDS {
                                t["data"][*field] = Value::Null;
                                t["data"]["field_quality"][*field] = json!("invalid");
                                t["inference"][*field]["value"] = Value::Null;
                                t["inference"][*field]["complete"] = json!(false);
                            }
                        }
                        t
                    })
                    .collect(),
            ),
            _ => Value::Null,
        }
    }
    pub fn needed(&self) -> bool {
        self.mode != "position"
            || self.values.iter().any(|fields| {
                POSITION_FIELDS
                    .iter()
                    .any(|field| fields[*field]["complete"] == true)
            })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn trade(key: &str, q: f64) -> Value {
        json!({"key":key,"kind":"trade","first_seq":1,"first_time_us":1,"last_fact_seq":2,"valid":true,"resolved_seq":0,"data":{"symbol":"S","order_id":"O","quantity":q,"price":10,"direction":"buy","position_side":"long","offset":"open","execution_id":"E","source":"B","source_seq":3}})
    }
    #[test]
    fn source_watermark_cannot_claim_an_unresolved_weak_trade_is_covered() {
        let target = json!({"data":{"symbol":"S","position_side":"long","quantity":4},"proofs":{"quantity":{"valid":true,"kind":"source_watermark","source":"B","through":9}}});
        let mut weak = trade("weak", 1.0);
        weak["kind"] = json!("weak_trade");
        let mut d = Derivation::new(&json!({"mode":"position","targets":[target]})).unwrap();
        d.feed(&json!({"subjects":[weak]})).unwrap();
        assert!(d.results()[0]["data"]["quantity"].is_null());
        assert_eq!(d.results()[0]["baseline"]["quantity"], 4);
    }
    #[test]
    fn partial_position_proofs_and_members() {
        let target = json!({"data":{"symbol":"S","position_side":"long","quantity":4,"today_quantity":2,"available_quantity":2},"proofs":{"quantity":{"valid":true,"kind":"included_trade_keys","members":1},"today_quantity":{"valid":true,"kind":"source_watermark","source":"B","through":2},"available_quantity":{"valid":true,"kind":"source_watermark","source":"B","through":2}}});
        let mut d = Derivation::new(&json!({"mode":"position","targets":[target]})).unwrap();
        d.feed(&json!({"subjects":[trade("T",1.0)],"members":[{"quantity":{"members_count":1,"positions":[0]}}]})).unwrap();
        let r = d.results();
        assert_eq!(r[0]["data"]["quantity"], 4.0);
        assert_eq!(r[0]["data"]["today_quantity"], 3.0);
        assert!(r[0]["data"]["available_quantity"].is_null());
    }
    #[test]
    fn uncovered_contributions_are_summed_before_checking_final_position() {
        let target = json!({"data":{"symbol":"S","position_side":"long","quantity":0},"proofs":{"quantity":{"valid":true,"kind":"source_watermark","source":"B","through":0}}});
        let mut d = Derivation::new(&json!({"mode":"position","targets":[target]})).unwrap();
        let mut close = trade("C", 1.0);
        close["data"]["direction"] = json!("sell");
        close["data"]["offset"] = json!("close_today");
        d.feed(&json!({"subjects":[close,trade("O",1.0)]})).unwrap();
        assert_eq!(d.results()[0]["data"]["quantity"], 0.0);
    }
    #[test]
    fn member_negative_requires_matching_count_and_invalid_source_sequence_is_unknown() {
        for kind in ["included_trade_keys", "source_watermark"] {
            let target = json!({"data":{"symbol":"S","position_side":"long","quantity":2},"proofs":{"quantity":{"valid":true,"kind":kind,"members":1,"source":"B","through":9}}});
            let mut d = Derivation::new(&json!({"mode":"position","targets":[target]})).unwrap();
            let mut t = trade("T", 1.0);
            t["data"]["field_quality"] = json!({"source_seq":"invalid"});
            d.feed(&json!({"subjects":[t],"members":[{"quantity":{"members_count":2,"positions":[null]}}]})).unwrap();
            assert!(d.results()[0]["data"]["quantity"].is_null());
            assert!(!d.needed());
        }
    }
    #[test]
    fn unknown_ownership_precedes_execution_filter_and_weak_resolution() {
        let mut d = Derivation::new(
            &json!({"mode":"statistics","execution":"E","scope_symbol":"S","start_observed":true}),
        )
        .unwrap();
        let mut unknown = trade("U", 2.0);
        unknown["data"]["execution_id"] = Value::Null;
        let mut weak = unknown.clone();
        weak["kind"] = json!("weak_trade");
        weak["resolved_seq"] = json!(3);
        d.feed(&json!({"subjects":[trade("T",1.0),unknown,weak]}))
            .unwrap();
        let r = d.results();
        assert_eq!(r["known_quantity"], 1.0);
        assert_eq!(r["unowned_count"], 1);
        assert_eq!(r["complete"], false);
    }
    #[test]
    fn statistics_fix_symbol_and_interval_without_hiding_unowned_evidence() {
        let mut d=Derivation::new(&json!({"mode":"statistics","execution":"E","scope_symbol":"S","interval_start_us":2,"interval_end_us":4,"start_observed":true})).unwrap();
        let mut included = trade("T", 2.0);
        included["first_time_us"] = json!(3);
        let mut outside = trade("X", 5.0);
        outside["data"]["symbol"] = json!("X");
        outside["data"]["execution_id"] = Value::Null;
        d.feed(&json!({"subjects":[trade("before",1.0),included,outside]}))
            .unwrap();
        let r = d.results();
        assert_eq!(r["known_quantity"], 2.0);
        assert_eq!(r["vwap"], 10.0);
        assert_eq!(r["stable_count"], 1);
        assert_eq!(r["unowned_count"], 0);
        assert_eq!(r["complete"], true);
        let mut unknown_symbol = trade("unknown", 1.0);
        unknown_symbol["first_time_us"] = json!(3);
        unknown_symbol["data"]["symbol"] = Value::Null;
        unknown_symbol["data"]["execution_id"] = Value::Null;
        d.feed(&json!({"subjects":[unknown_symbol]})).unwrap();
        assert_eq!(d.results()["unowned_count"], 1);
        assert_eq!(d.results()["complete"], false);
    }
    #[test]
    fn explicit_execution_evidence_is_not_hidden_by_a_manual_origin_label() {
        let mut d = Derivation::new(
            &json!({"mode":"statistics","execution":"E","scope_symbol":"S","start_observed":true}),
        )
        .unwrap();
        let mut explicit = trade("E", 1.0);
        explicit["data"]["origin"] = json!("manual");
        let mut external = trade("M", 2.0);
        external["data"]["origin"] = json!("manual");
        external["data"]["execution_id"] = Value::Null;
        d.feed(&json!({"subjects":[explicit,external]})).unwrap();
        let r = d.results();
        assert_eq!(r["known_quantity"], 1.0);
        assert_eq!(r["complete"], true);
    }
    #[test]
    fn inconsistent_position_fields_keep_baselines_but_do_not_claim_known_values() {
        let mut d=Derivation::new(&json!({"mode":"position","targets":[{"data":{"symbol":"S","position_side":"long","quantity":2,"today_quantity":3},"proofs":{"quantity":{"valid":true},"today_quantity":{"valid":true}}}]})).unwrap();
        d.feed(&json!({"subjects":[]})).unwrap();
        let r = d.results();
        assert_eq!(r[0]["conflict"], true);
        assert!(r[0]["data"]["quantity"].is_null());
        assert_eq!(r[0]["baseline"]["quantity"], 2);
    }
    #[test]
    fn cancel_does_not_assign_owner_and_conflicting_placements_are_unknown() {
        let mut d = Derivation::new(&json!({"mode":"owner","targets":[trade("T",1.0)]})).unwrap();
        d.feed(&json!({"subjects":[{"data":{"operation":"cancel_order","symbol":"S","order_id":"O","execution_id":"X","phase":"request"}}]})).unwrap();
        assert_eq!(d.results()[0]["data"]["execution_id"], "E");
        assert_eq!(d.results()[0]["owner"]["cancel_pending"], true);
        d.feed(&json!({"subjects":[{"data":{"operation":"place_order","symbol":"S","order_id":"O","execution_id":"X"}},{"data":{"operation":"place_order","symbol":"S","order_id":"O","execution_id":"Y"}}]})).unwrap();
        assert!(d.results()[0]["data"]["execution_id"].is_null());
    }
}
