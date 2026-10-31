//! Recording-local replay state; observations remain separate from inferred facts.

use std::collections::{BTreeMap, BTreeSet};

use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};

use crate::error::AnalysisError;

const MAX_STATE_BYTES: usize = 2 * 1024 * 1024;
const MAX_STATE_ENTRIES: usize = 20_000;
const JS_SAFE_INTEGER: u64 = 9_007_199_254_740_991;

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct TradeAccounting {
    own_seen_keys: BTreeSet<String>,
    market_seen_keys: BTreeSet<String>,
    own_records: BTreeMap<String, Value>,
    market_records: BTreeMap<String, Value>,
    pending: BTreeMap<String, Value>,
    execution_totals: BTreeMap<String, Value>,
    own_complete: bool,
    market_complete: bool,
}

impl TradeAccounting {
    fn new() -> Self {
        Self {
            own_seen_keys: BTreeSet::new(),
            market_seen_keys: BTreeSet::new(),
            own_records: BTreeMap::new(),
            market_records: BTreeMap::new(),
            pending: BTreeMap::new(),
            execution_totals: BTreeMap::new(),
            own_complete: false,
            market_complete: false,
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Projection {
    schema_version: u32,
    pub(crate) recording_id: String,
    pub(crate) state_through_seq: u64,
    pub(crate) time_us: Option<u64>,
    quotes: BTreeMap<String, Value>,
    orders: BTreeMap<String, Value>,
    executions: BTreeMap<String, Value>,
    commands: BTreeMap<String, Value>,
    connections: BTreeMap<String, Value>,
    trade_accounting: TradeAccounting,
    observed_positions: BTreeMap<String, Value>,
    derived_positions: BTreeMap<String, Value>,
    position_basis: BTreeMap<String, Value>,
    position_scopes: BTreeMap<String, Value>,
    sync_generation: BTreeMap<String, u64>,
    synchronized: BTreeMap<String, bool>,
    order_change_seq: BTreeMap<String, u64>,
    last_set_revision: BTreeMap<String, u64>,
}

impl Projection {
    pub(crate) fn new(recording_id: &str) -> Self {
        Self {
            schema_version: 1,
            recording_id: recording_id.to_owned(),
            state_through_seq: 0,
            time_us: None,
            quotes: BTreeMap::new(),
            orders: BTreeMap::new(),
            executions: BTreeMap::new(),
            commands: BTreeMap::new(),
            connections: BTreeMap::new(),
            trade_accounting: TradeAccounting::new(),
            observed_positions: BTreeMap::new(),
            derived_positions: BTreeMap::new(),
            position_basis: BTreeMap::new(),
            position_scopes: BTreeMap::new(),
            sync_generation: BTreeMap::new(),
            synchronized: BTreeMap::new(),
            order_change_seq: BTreeMap::new(),
            last_set_revision: BTreeMap::new(),
        }
    }

    pub(crate) fn restore(value: Value) -> Result<Self, AnalysisError> {
        let state: Self = serde_json::from_value(value).map_err(|error| {
            AnalysisError::new("REPLAY_CHECKPOINT", format!("invalid checkpoint: {error}"))
        })?;
        if state.schema_version != 1
            || state.recording_id.is_empty()
            || state.state_through_seq > JS_SAFE_INTEGER
            || state.time_us.is_some_and(|time| time > JS_SAFE_INTEGER)
            || (state.state_through_seq > 0 && state.time_us.is_none())
        {
            return Err(failure("unsupported or inconsistent checkpoint"));
        }
        state.check_budget()?;
        Ok(state)
    }

    pub(crate) fn snapshot(&self) -> Result<Value, AnalysisError> {
        self.check_budget()?;
        serde_json::to_value(self)
            .map_err(|error| AnalysisError::caused_by("REPLAY_CHECKPOINT", error))
    }

    pub(crate) fn apply(&mut self, message: &Value) -> Result<(), AnalysisError> {
        if message["schema_version"].as_u64() != Some(1)
            || message["recording_id"].as_str() != Some(self.recording_id.as_str())
        {
            return Err(failure(
                "message schema or recording does not match checkpoint",
            ));
        }
        let seq = safe_integer(&message["capture_seq"])?;
        let time = safe_integer(&message["time_us"])?;
        if seq == 0 {
            return Err(failure("observation sequence must be positive"));
        }
        if seq <= self.state_through_seq {
            return Ok(());
        }
        if self.time_us.is_some_and(|previous| time < previous) {
            return Err(AnalysisError::new(
                "REPLAY_TIME_BACKWARDS",
                "observation time moved backwards",
            ));
        }
        let topic = message["topic"]
            .as_str()
            .ok_or_else(|| failure("missing topic"))?;
        let data = message["data"]
            .as_object()
            .ok_or_else(|| failure("data must be an object"))?;
        match topic {
            "/execution" => self.execution(data, seq, time)?,
            "/command" => self.command(data, seq, time),
            "/order" => self.order(data, seq, time)?,
            "/order_set" => self.order_set(data, seq, time)?,
            "/trade" => self.trade(data, false, seq, time)?,
            "/account_observation" => self.account_observation(data, seq, time)?,
            "/connection" => self.connection(data, seq, time)?,
            _ if topic.starts_with("/quote/") => {
                let symbol = topic_symbol(topic, "/quote/", data)?;
                self.quotes.insert(symbol, annotate(data, seq, time));
            }
            _ if topic.starts_with("/market_trade/") => {
                let symbol = topic_symbol(topic, "/market_trade/", data)?;
                let mut data = data.clone();
                data.insert("symbol".into(), Value::String(symbol));
                self.trade(&data, true, seq, time)?;
            }
            _ => {
                return Err(AnalysisError::new(
                    "REPLAY_SCHEMA",
                    format!("unsupported topic {topic}"),
                ))
            }
        }
        self.state_through_seq = seq;
        self.time_us = Some(time);
        self.check_budget()
    }

    fn check_budget(&self) -> Result<(), AnalysisError> {
        let entries = self.quotes.len()
            + self.orders.len()
            + self.executions.len()
            + self.commands.len()
            + self.connections.len()
            + self.observed_positions.len()
            + self.derived_positions.len()
            + self.position_basis.len()
            + self.position_scopes.len()
            + self.trade_accounting.own_records.len()
            + self.trade_accounting.market_records.len()
            + self.trade_accounting.pending.len()
            + self.order_change_seq.len()
            + self.last_set_revision.len()
            + self.sync_generation.len()
            + self.synchronized.len()
            + self.trade_accounting.own_seen_keys.len()
            + self.trade_accounting.market_seen_keys.len();
        if entries > MAX_STATE_ENTRIES {
            return Err(AnalysisError::new(
                "REPLAY_BUDGET",
                "projection entry limit exceeded",
            ));
        }
        let bytes = serde_json::to_vec(self)
            .map_err(|error| AnalysisError::caused_by("REPLAY_CHECKPOINT", error))?;
        if bytes.len() > MAX_STATE_BYTES {
            return Err(AnalysisError::new(
                "REPLAY_BUDGET",
                "projection exceeds 2 MiB checkpoint budget",
            ));
        }
        Ok(())
    }

    fn execution(
        &mut self,
        data: &Map<String, Value>,
        seq: u64,
        time: u64,
    ) -> Result<(), AnalysisError> {
        let key =
            identity(data.get("execution_id")).ok_or_else(|| failure("missing execution_id"))?;
        let mut value = annotate(data, seq, time);
        if matches!(text(data, "event").as_deref(), Some("start" | "started"))
            || matches!(text(data, "phase").as_deref(), Some("start" | "started"))
        {
            value["start_observed"] = Value::Bool(true);
        }
        if let Some(symbol) = data.get("symbol").and_then(Value::as_str) {
            let mut symbols = self
                .executions
                .get(&key)
                .and_then(|execution| execution["by_symbol"].as_object())
                .cloned()
                .unwrap_or_default();
            let mut symbol_value = symbols
                .get(symbol)
                .and_then(Value::as_object)
                .cloned()
                .unwrap_or_default();
            merge_known(&mut symbol_value, value.as_object().unwrap());
            symbols.insert(symbol.into(), Value::Object(symbol_value));
            value["by_symbol"] = Value::Object(symbols);
        }
        merge_map(&mut self.executions, key, value);
        self.rebuild_totals();
        Ok(())
    }

    fn command(&mut self, data: &Map<String, Value>, seq: u64, time: u64) {
        let key =
            identity(data.get("command_id")).unwrap_or_else(|| format!("unknown-command:{seq}"));
        merge_map(&mut self.commands, key.clone(), annotate(data, seq, time));
        if let Some((symbol, order_id, execution_id)) =
            self.commands.get(&key).and_then(command_ownership)
        {
            if self.owner_for_order(&symbol, &order_id).as_deref() == Some(execution_id.as_str()) {
                self.associate_order_owner(&symbol, &order_id, &execution_id, seq, time);
            }
        }
        self.rebuild_order_amounts();
        self.rebuild_totals();
        self.rebuild_positions();
    }

    fn owner_for_order(&self, symbol: &str, order_id: &str) -> Option<String> {
        let owners: BTreeSet<_> = self
            .commands
            .values()
            .filter_map(command_ownership)
            .filter(|(candidate_symbol, candidate_order, _)| {
                candidate_symbol == symbol && candidate_order == order_id
            })
            .map(|(_, _, execution)| execution)
            .collect();
        (owners.len() == 1).then(|| owners.into_iter().next().unwrap())
    }

    fn data_with_owner(
        &self,
        data: &Map<String, Value>,
        seq: u64,
        time: u64,
    ) -> Map<String, Value> {
        let mut associated = Value::Object(data.clone());
        if let (Some(symbol), Some(order_id)) = (
            data.get("symbol").and_then(Value::as_str),
            identity(data.get("order_id")),
        ) {
            if let Some(execution_id) = self.owner_for_order(symbol, &order_id) {
                supplement_owner(&mut associated, &execution_id, seq, time);
            }
        }
        associated.as_object().unwrap().clone()
    }

    fn associate_order_owner(
        &mut self,
        symbol: &str,
        order_id: &str,
        execution_id: &str,
        seq: u64,
        time: u64,
    ) {
        let key = compound(symbol, order_id);
        if let Some(order) = self.orders.get_mut(&key) {
            supplement_owner(order, execution_id, seq, time);
        }
        let mut changed = Vec::new();
        for (key, record) in &mut self.trade_accounting.own_records {
            if matches_order(&record["data"], symbol, order_id)
                && supplement_owner(&mut record["data"], execution_id, seq, time)
            {
                record["last_fact_seq"] = json!(seq);
                changed.push((key.clone(), record.clone()));
            }
        }
        for (key, record) in changed {
            let pending_key = format!("own:trade:{key}");
            if trade_amount(&record).is_some() && !unassociated_trade(&record["data"]) {
                self.trade_accounting.pending.remove(&pending_key);
            } else {
                self.trade_accounting
                    .pending
                    .insert(pending_key, json!({"market":false,"observation":record}));
            }
        }
        for pending in self
            .trade_accounting
            .pending
            .values_mut()
            .filter(|pending| pending["market"] == false)
        {
            let observation = &mut pending["observation"];
            let data = if observation.get("data").is_some() {
                &mut observation["data"]
            } else {
                observation
            };
            if matches_order(data, symbol, order_id) {
                supplement_owner(data, execution_id, seq, time);
            }
        }
    }

    fn trade(
        &mut self,
        data: &Map<String, Value>,
        market: bool,
        seq: u64,
        time: u64,
    ) -> Result<(), AnalysisError> {
        let associated = if market {
            data.clone()
        } else {
            self.data_with_owner(data, seq, time)
        };
        let data = &associated;
        let stable = text(data, "identity_quality").as_deref() == Some("stable");
        let key = data
            .get("trade_key")
            .and_then(Value::as_str)
            .filter(|key| !key.is_empty());
        if !stable || key.is_none() {
            self.trade_accounting.pending.insert(
                format!(
                    "{}:observation:{seq}",
                    if market { "market" } else { "own" }
                ),
                json!({"market":market,"observation":annotate(data,seq,time)}),
            );
            self.mark_incomplete(market);
            if !market {
                self.rebuild_totals();
                self.rebuild_positions();
            }
            return Ok(());
        }
        let key = key.unwrap().to_owned();
        let records = if market {
            &mut self.trade_accounting.market_records
        } else {
            &mut self.trade_accounting.own_records
        };
        let previous = records.get(&key).cloned();
        let record = merge_trade_record(previous.as_ref(), data, seq, time);
        records.insert(key.clone(), record.clone());
        let valid = trade_amount(&record).is_some();
        let seen = if market {
            &mut self.trade_accounting.market_seen_keys
        } else {
            &mut self.trade_accounting.own_seen_keys
        };
        if valid {
            seen.insert(key.clone());
        } else {
            self.mark_incomplete(market);
        }
        if let Some(original) = data.get("related_capture_seq").and_then(Value::as_u64) {
            self.trade_accounting.pending.remove(&format!(
                "{}:observation:{original}",
                if market { "market" } else { "own" }
            ));
        }
        let unassociated = !market && unassociated_trade(&record["data"]);
        if !valid || unassociated {
            self.trade_accounting.pending.insert(
                format!("{}:trade:{key}", if market { "market" } else { "own" }),
                json!({"market":market,"observation":record}),
            );
        } else {
            self.trade_accounting.pending.remove(&format!(
                "{}:trade:{key}",
                if market { "market" } else { "own" }
            ));
        }
        if !market {
            self.rebuild_order_amounts();
            self.rebuild_totals();
            self.rebuild_positions();
        }
        Ok(())
    }

    fn mark_incomplete(&mut self, market: bool) {
        if market {
            self.trade_accounting.market_complete = false;
        } else {
            self.trade_accounting.own_complete = false;
        }
    }

    fn rebuild_order_amounts(&mut self) {
        let mut amounts: BTreeMap<String, (f64, bool, u64)> = BTreeMap::new();
        for record in self.trade_accounting.own_records.values() {
            let Some(symbol) = record["data"]["symbol"].as_str() else {
                continue;
            };
            let Some(order_id) = identity(record["data"].get("order_id")) else {
                continue;
            };
            let amount = amounts
                .entry(compound(symbol, &order_id))
                .or_insert((0.0, true, 0));
            amount.2 = amount.2.max(record["last_fact_seq"].as_u64().unwrap_or(0));
            if let Some((quantity, _)) = trade_amount(record) {
                amount.0 += quantity;
                amount.1 &= amount.0.is_finite();
            } else {
                amount.1 = false;
            }
        }
        for pending in self
            .trade_accounting
            .pending
            .values()
            .filter(|pending| pending["market"] == false)
        {
            let observation = &pending["observation"];
            let data = observation.get("data").unwrap_or(observation);
            if data.get("trade_key").and_then(Value::as_str).is_some() {
                continue;
            }
            if let (Some(symbol), Some(order_id)) =
                (data["symbol"].as_str(), identity(data.get("order_id")))
            {
                amounts
                    .entry(compound(symbol, &order_id))
                    .or_insert((0.0, true, 0))
                    .1 = false;
            }
        }
        for (key, order) in &mut self.orders {
            let data = order.as_object().unwrap();
            let observed = number(data, "observed_filled_quantity")
                .or_else(|| number(data, "filled_quantity"));
            let (known_quantity, complete, last_fact) =
                amounts.get(key).copied().unwrap_or((0.0, true, 0));
            let quantity = number(data, "quantity");
            let filled = observed
                .map(|observed| observed.max(known_quantity))
                .or_else(|| (known_quantity > 0.0).then_some(known_quantity));
            let remaining = quantity
                .zip(filled)
                .map(|(quantity, filled)| quantity - filled)
                .filter(|value| value.is_finite() && *value >= 0.0);
            let from_zero = order["fill_start_observed"] == true;
            order["known_trade_quantity"] = if known_quantity.is_finite() {
                json!(known_quantity)
            } else {
                Value::Null
            };
            order["filled_quantity"] = filled
                .filter(|value| value.is_finite())
                .map_or(Value::Null, |value| json!(value));
            order["remaining_quantity"] = if complete {
                remaining.map_or_else(
                    || order["observed_remaining_quantity"].clone(),
                    |value| json!(value),
                )
            } else {
                Value::Null
            };
            order["remaining_quantity_complete"] =
                Value::Bool(complete && from_zero && remaining.is_some());
            if last_fact > 0 {
                self.order_change_seq
                    .entry(key.clone())
                    .and_modify(|previous| *previous = (*previous).max(last_fact))
                    .or_insert(last_fact);
            }
        }
    }

    fn rebuild_totals(&mut self) {
        let mut totals = BTreeMap::new();
        for (key, execution) in &self.executions {
            let complete = execution["start_observed"].as_bool() == Some(true);
            let mut by_symbol = Map::new();
            if let Some(symbols) = execution["symbols"].as_array() {
                for symbol in symbols.iter().filter_map(Value::as_str) {
                    by_symbol.insert(symbol.into(), empty_total(complete));
                }
            }
            if let Some(symbol) = execution["symbol"].as_str() {
                by_symbol.insert(symbol.into(), empty_total(complete));
            }
            if let Some(symbols) = execution["by_symbol"].as_object() {
                for symbol in symbols.keys() {
                    by_symbol.insert(symbol.into(), empty_total(complete));
                }
            }
            totals.insert(
                key.clone(),
                json!({"complete":complete,"by_symbol":by_symbol}),
            );
        }
        for record in self.trade_accounting.own_records.values() {
            let Some(execution) = identity(record["data"].get("execution_id")) else {
                continue;
            };
            let total = totals
                .entry(execution)
                .or_insert_with(|| json!({"complete":false,"by_symbol":{}}));
            let Some(symbol) = record["data"]["symbol"].as_str() else {
                total["complete"] = Value::Bool(false);
                continue;
            };
            let complete = total["complete"].as_bool() == Some(true);
            let symbol_total = total["by_symbol"]
                .as_object_mut()
                .unwrap()
                .entry(symbol)
                .or_insert_with(|| empty_total(complete));
            if let Some((quantity, price)) = trade_amount(record) {
                add_total(symbol_total, "quantity", quantity);
                add_total(symbol_total, "notional", quantity * price);
            } else {
                symbol_total["complete"] = Value::Bool(false);
            }
        }
        for pending in self
            .trade_accounting
            .pending
            .values()
            .filter(|value| value["market"] == false)
        {
            let observation = &pending["observation"];
            let data = observation.get("data").unwrap_or(observation);
            if let Some(execution) = identity(data.get("execution_id")) {
                if let Some(total) = totals.get_mut(&execution) {
                    incomplete_total(total, data["symbol"].as_str());
                }
            } else if !data.as_object().is_some_and(known_external) {
                for total in totals.values_mut() {
                    incomplete_total(total, data["symbol"].as_str());
                }
            }
        }
        for total in totals.values_mut() {
            if total["by_symbol"]
                .as_object()
                .is_some_and(|symbols| symbols.values().any(|value| value["complete"] != true))
            {
                total["complete"] = Value::Bool(false);
            }
        }
        self.trade_accounting.execution_totals = totals;
    }

    fn order(
        &mut self,
        data: &Map<String, Value>,
        seq: u64,
        time: u64,
    ) -> Result<(), AnalysisError> {
        let symbol = required_text(data, "symbol")?;
        let generation = data.get("sync_generation").and_then(Value::as_u64);
        if !self.accept_generation(&symbol, generation) {
            return Ok(());
        }
        let id =
            identity(data.get("order_id")).unwrap_or_else(|| format!("unknown-observation:{seq}"));
        let associated = self.data_with_owner(data, seq, time);
        let data = &associated;
        let key = compound(&symbol, &id);
        if generation.is_none() {
            if let Some(previous) = self.orders.get_mut(&key) {
                previous["confirmed_book"] = Value::Bool(false);
                previous["confirmation_state"] = json!("unverified_generation");
                self.order_change_seq.insert(key, seq);
                return Ok(());
            }
        }
        let value = self.merge_order(
            &key,
            data,
            seq,
            time,
            generation.is_some() && identity(data.get("order_id")).is_some(),
        );
        self.orders.insert(key.clone(), value);
        self.order_change_seq.insert(key, seq);
        self.rebuild_order_amounts();
        Ok(())
    }

    fn accept_generation(&mut self, symbol: &str, generation: Option<u64>) -> bool {
        let Some(generation) = generation else {
            return true;
        };
        let previous = self.sync_generation.get(symbol).copied();
        if previous.is_some_and(|previous| generation < previous) {
            return false;
        }
        if previous.is_none_or(|previous| generation > previous) {
            self.invalidate_orders(symbol, "awaiting_sync");
            self.sync_generation.insert(symbol.into(), generation);
            self.synchronized.insert(symbol.into(), false);
            self.last_set_revision.remove(symbol);
        }
        true
    }

    fn merge_order(
        &self,
        key: &str,
        data: &Map<String, Value>,
        seq: u64,
        time: u64,
        authority: bool,
    ) -> Value {
        let previous = self.orders.get(key);
        let mut merged = previous
            .and_then(Value::as_object)
            .cloned()
            .unwrap_or_default();
        let previous_terminal = previous.is_some_and(terminal);
        let previous_filled = previous.and_then(Value::as_object).and_then(|value| {
            number(value, "observed_filled_quantity").or_else(|| number(value, "filled_quantity"))
        });
        merge_known(&mut merged, data);
        if previous_terminal && !terminal(&Value::Object(data.clone())) {
            if let Some(previous) = previous.and_then(Value::as_object) {
                for field in ["status", "remaining_quantity", "confirmed_book"] {
                    if let Some(value) = previous.get(field) {
                        merged.insert(field.into(), value.clone());
                    }
                }
            }
        }
        if let (Some(before), Some(after)) = (previous_filled, number(data, "filled_quantity")) {
            if after < before {
                if let Some(previous) = previous.and_then(Value::as_object) {
                    for field in ["filled_quantity", "remaining_quantity", "status"] {
                        if let Some(value) = previous.get(field) {
                            merged.insert(field.into(), value.clone());
                        }
                    }
                }
            }
        }
        let is_terminal = terminal(&Value::Object(merged.clone()));
        if let Some(filled) = number(data, "filled_quantity")
            .filter(|filled| previous_filled.is_none_or(|previous| *filled >= previous))
        {
            merged.insert("observed_filled_quantity".into(), json!(filled));
            if filled == 0.0 {
                merged.insert("fill_start_observed".into(), Value::Bool(true));
            }
        }
        if let Some(remaining) = number(data, "remaining_quantity") {
            merged.insert("observed_remaining_quantity".into(), json!(remaining));
        }
        let proof = known(data, "confirmed_book")
            && data.get("confirmed_book").and_then(Value::as_bool) == Some(true);
        let existing_proof =
            previous.is_some_and(|value| value["confirmed_book"].as_bool() == Some(true));
        let supplied = data.contains_key("confirmed_book");
        let confirmed = authority && !is_terminal && if supplied { proof } else { existing_proof };
        merged.insert("confirmed_book".into(), Value::Bool(confirmed));
        merged.insert(
            "confirmation_state".into(),
            json!(if is_terminal {
                "terminal"
            } else if confirmed {
                "confirmed"
            } else {
                "unknown"
            }),
        );
        merged.insert("observed_seq".into(), json!(seq));
        merged.insert("observed_at_us".into(), json!(time));
        Value::Object(merged)
    }

    fn order_set(
        &mut self,
        data: &Map<String, Value>,
        seq: u64,
        time: u64,
    ) -> Result<(), AnalysisError> {
        let rows = data
            .get("orders")
            .and_then(Value::as_array)
            .ok_or_else(|| failure("order_set.orders must be an array"))?;
        let symbols = self.scope(data, rows);
        let generation = data.get("sync_generation").and_then(Value::as_u64);
        let basis = order_set_basis(data, seq, &self.recording_id);
        let identified = rows
            .iter()
            .all(|row| row["symbol"].as_str().is_some() && identity(row.get("order_id")).is_some());
        for symbol in symbols {
            if !self.accept_generation(&symbol, generation) {
                continue;
            }
            let Some((generation, start, revision)) = basis.filter(|_| identified) else {
                continue;
            };
            if self
                .last_set_revision
                .get(&symbol)
                .is_some_and(|last| revision <= *last)
            {
                continue;
            }
            self.apply_set_symbol(&symbol, rows, seq, time, generation, start)?;
            self.last_set_revision.insert(symbol.clone(), revision);
            self.synchronized.insert(symbol, true);
        }
        self.rebuild_order_amounts();
        Ok(())
    }

    fn apply_set_symbol(
        &mut self,
        symbol: &str,
        rows: &[Value],
        seq: u64,
        time: u64,
        generation: u64,
        start: u64,
    ) -> Result<(), AnalysisError> {
        let mut present = BTreeSet::new();
        for row in rows
            .iter()
            .filter(|row| row["symbol"].as_str() == Some(symbol))
        {
            let row = row
                .as_object()
                .ok_or_else(|| failure("order_set row must be an object"))?;
            let id = identity(row.get("order_id"))
                .ok_or_else(|| failure("order_set row lacks order_id"))?;
            let key = compound(symbol, &id);
            present.insert(key.clone());
            if self
                .order_change_seq
                .get(&key)
                .is_some_and(|changed| *changed > start)
            {
                continue;
            }
            let mut row = row.clone();
            row.insert("sync_generation".into(), json!(generation));
            let value = self.merge_order(&key, &row, seq, time, true);
            self.orders.insert(key, value);
        }
        for (key, order) in &mut self.orders {
            if order["symbol"].as_str() == Some(symbol)
                && !present.contains(key)
                && !self
                    .order_change_seq
                    .get(key)
                    .is_some_and(|changed| *changed > start)
                && !terminal(order)
            {
                order["confirmed_book"] = Value::Bool(false);
                order["confirmation_state"] = json!("absent");
            }
        }
        Ok(())
    }

    fn scope(&self, data: &Map<String, Value>, rows: &[Value]) -> BTreeSet<String> {
        let scope = data.get("scope");
        let values = scope
            .and_then(Value::as_array)
            .or_else(|| scope.and_then(|value| value["symbols"].as_array()));
        let mut symbols: BTreeSet<String> = values
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
            .map(str::to_owned)
            .collect();
        let account = scope.is_some_and(|value| value["kind"] == "account");
        let legacy_wildcard = scope.is_some_and(Value::is_array) && symbols.remove("*");
        if account || legacy_wildcard {
            symbols.extend(
                self.orders
                    .values()
                    .filter_map(|value| value["symbol"].as_str())
                    .map(str::to_owned),
            );
            symbols.extend(
                self.observed_positions
                    .values()
                    .filter_map(|value| value["symbol"].as_str())
                    .map(str::to_owned),
            );
            symbols.extend(self.quotes.keys().cloned());
            symbols.extend(self.connections.keys().cloned());
            symbols.extend(self.sync_generation.keys().cloned());
            symbols.extend(self.position_scopes.keys().cloned());
            symbols.extend(self.position_basis.keys().filter_map(|key| {
                serde_json::from_str::<Value>(key)
                    .ok()
                    .and_then(|value| value[0].as_str().map(str::to_owned))
            }));
            symbols.extend(
                rows.iter()
                    .filter_map(|value| value["symbol"].as_str())
                    .map(str::to_owned),
            );
        }
        symbols
    }

    fn invalidate_orders(&mut self, symbol: &str, reason: &str) {
        for order in self
            .orders
            .values_mut()
            .filter(|order| order["symbol"].as_str() == Some(symbol))
        {
            order["confirmed_book"] = Value::Bool(false);
            if !terminal(order) {
                order["confirmation_state"] = json!(reason);
            }
        }
    }

    fn connection(
        &mut self,
        data: &Map<String, Value>,
        seq: u64,
        time: u64,
    ) -> Result<(), AnalysisError> {
        let symbols = self.scope(data, &[]);
        let generation = data.get("sync_generation").and_then(Value::as_u64);
        let disconnected = data.get("connected").and_then(Value::as_bool) == Some(false)
            || matches!(
                text(data, "status").as_deref(),
                Some("disconnected" | "unavailable" | "invalidated")
            );
        for symbol in symbols {
            if !self.accept_generation(&symbol, generation) {
                continue;
            }
            self.connections
                .insert(symbol.clone(), annotate(data, seq, time));
            if disconnected {
                self.invalidate_orders(&symbol, "disconnected");
                self.synchronized.insert(symbol.clone(), false);
                if let Some(quote) = self.quotes.get_mut(&symbol) {
                    quote["book_valid"] = Value::Bool(false);
                }
            }
        }
        Ok(())
    }

    fn account_observation(
        &mut self,
        data: &Map<String, Value>,
        seq: u64,
        time: u64,
    ) -> Result<(), AnalysisError> {
        let rows = data
            .get("positions")
            .and_then(Value::as_array)
            .ok_or_else(|| failure("positions must be an array"))?;
        let scope = self.scope(data, rows);
        let complete = data.get("scope_complete").and_then(Value::as_bool) == Some(true);
        let present: BTreeSet<String> = rows.iter().filter_map(position_key).collect();
        if complete {
            for (key, position) in &mut self.observed_positions {
                if position["symbol"]
                    .as_str()
                    .is_some_and(|symbol| scope.contains(symbol))
                    && !present.contains(key)
                {
                    for field in [
                        "quantity",
                        "today_quantity",
                        "yesterday_quantity",
                        "available_quantity",
                        "frozen_quantity",
                    ] {
                        if position.get(field).is_some() {
                            position[field] = json!(0);
                            if let Some(quality) = position
                                .get_mut("field_quality")
                                .and_then(Value::as_object_mut)
                            {
                                quality.insert(field.into(), json!("known"));
                            }
                        }
                    }
                    position["observed_seq"] = json!(seq);
                    position["observed_at_us"] = json!(time);
                    position["observation_kind"] = json!("absent_in_complete_scope");
                    self.position_basis
                        .insert(key.clone(), json!({"status":"unverified","proof":null}));
                }
            }
        }
        for (index, row) in rows.iter().enumerate() {
            let value = row
                .as_object()
                .ok_or_else(|| failure("position row must be an object"))?;
            let symbol = required_text(value, "symbol")?;
            if !scope.contains(&symbol) {
                return Err(failure("position row falls outside observation scope"));
            }
            let key = position_key(row).unwrap_or_else(|| {
                compound(&symbol, &format!("unknown-observation:{seq}:{index}"))
            });
            merge_map(
                &mut self.observed_positions,
                key.clone(),
                annotate(value, seq, time),
            );
            let basis = row
                .get("position_basis")
                .or_else(|| data.get("position_basis"))
                .cloned()
                .unwrap_or(Value::Null);
            self.position_basis.insert(key, normalize_basis(basis));
        }
        for symbol in scope {
            self.position_scopes.insert(
                symbol,
                json!({"scope_complete":complete,"observed_seq":seq,"observed_at_us":time}),
            );
        }
        self.rebuild_positions();
        Ok(())
    }

    fn rebuild_positions(&mut self) {
        let mut derived = BTreeMap::new();
        for (key, observation) in &self.observed_positions {
            let basis = self.position_basis.get(key).unwrap_or(&Value::Null);
            let Some(data) = observation.as_object() else {
                continue;
            };
            let mut value = observation.clone();
            let mut reason = None;
            if basis["status"] != "aligned"
                || number(data, "quantity").is_none()
                || !matches!(
                    text(data, "position_side").as_deref(),
                    Some("net" | "long" | "short")
                )
            {
                reason = Some("unverified_position_basis");
            } else {
                for (trade_key, record) in &self.trade_accounting.own_records {
                    if record["data"]["symbol"] != observation["symbol"] {
                        continue;
                    }
                    match covered(&basis["proof"], trade_key, &record["data"]) {
                        Some(true) => continue,
                        Some(false) => {}
                        None => {
                            reason = Some("unknown_business_coverage");
                            break;
                        }
                    }
                    if !apply_position_trade(&mut value, record) {
                        reason = Some("incomplete_trade_semantics");
                        break;
                    }
                }
                if self.trade_accounting.pending.values().any(|pending| {
                    if pending["market"] == true {
                        return false;
                    }
                    let observation = &pending["observation"];
                    let trade = observation.get("data").unwrap_or(observation);
                    trade["symbol"] == data["symbol"]
                        && trade.get("trade_key").and_then(Value::as_str).is_none()
                }) {
                    reason = Some("unidentified_trade");
                }
            }
            if let Some(reason) = reason {
                value["quantity"] = Value::Null;
                value["status"] = json!("unknown");
                value["reason"] = json!(reason);
            } else {
                value["status"] = json!("derived");
            }
            for field in ["available_quantity", "frozen_quantity"] {
                if value.get(field).is_some() {
                    value[field] = Value::Null;
                }
            }
            derived.insert(key.clone(), value);
        }
        self.derived_positions = derived;
    }
}

fn failure(message: &str) -> AnalysisError {
    AnalysisError::new("REPLAY_MESSAGE", message)
}

fn safe_integer(value: &Value) -> Result<u64, AnalysisError> {
    value
        .as_u64()
        .filter(|value| *value <= JS_SAFE_INTEGER)
        .ok_or_else(|| failure("expected a nonnegative JS-safe integer"))
}

fn identity(value: Option<&Value>) -> Option<String> {
    match value? {
        Value::String(value) if !value.is_empty() => Some(value.clone()),
        Value::Number(value) if value.is_u64() || value.is_i64() => Some(value.to_string()),
        _ => None,
    }
}

fn text(data: &Map<String, Value>, field: &str) -> Option<String> {
    if !known(data, field) {
        return None;
    }
    data.get(field)
        .and_then(Value::as_str)
        .map(str::to_lowercase)
}

fn required_text(data: &Map<String, Value>, field: &str) -> Result<String, AnalysisError> {
    data.get(field)
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .map(str::to_owned)
        .ok_or_else(|| failure(&format!("missing {field}")))
}

fn known(data: &Map<String, Value>, field: &str) -> bool {
    !matches!(
        data.get("field_quality")
            .and_then(|quality| quality[field].as_str()),
        Some("missing" | "invalid")
    )
}

fn number(data: &Map<String, Value>, field: &str) -> Option<f64> {
    if !known(data, field) {
        return None;
    }
    data.get(field)
        .and_then(Value::as_f64)
        .filter(|value| value.is_finite())
}

fn annotate(data: &Map<String, Value>, seq: u64, time: u64) -> Value {
    let mut value = data.clone();
    value.insert("observed_seq".into(), json!(seq));
    value.insert("observed_at_us".into(), json!(time));
    Value::Object(value)
}

fn merge_known(target: &mut Map<String, Value>, incoming: &Map<String, Value>) {
    for (field, value) in incoming {
        if field == "field_quality" {
            let existing = target.entry(field.clone()).or_insert_with(|| json!({}));
            if let (Some(existing), Some(incoming)) = (existing.as_object_mut(), value.as_object())
            {
                existing.extend(incoming.clone());
            }
        } else if !value.is_null() || !target.contains_key(field) {
            target.insert(field.clone(), value.clone());
        }
    }
}

fn retain_known_trade_fields(
    target: &mut Map<String, Value>,
    previous: &Map<String, Value>,
    incoming: &Map<String, Value>,
) {
    for (field, value) in previous {
        if !value.is_null() && known(previous, field) && !known(incoming, field) {
            target.insert(field.clone(), value.clone());
            let quality = target.entry("field_quality").or_insert_with(|| json!({}));
            if let Some(quality) = quality.as_object_mut() {
                quality.insert(field.clone(), json!("known"));
            }
        }
    }
}

fn merge_trade_record(
    previous: Option<&Value>,
    data: &Map<String, Value>,
    seq: u64,
    time: u64,
) -> Value {
    let mut record = previous.cloned().unwrap_or_else(|| {
        json!({
            "data":{},"first_capture_seq":seq,"first_time_us":time,"conflict":false
        })
    });
    let old_data = record["data"].as_object().cloned().unwrap_or_default();
    let mut merged = old_data.clone();
    merge_known(&mut merged, data);
    retain_known_trade_fields(&mut merged, &old_data, data);
    for field in [
        "symbol",
        "price",
        "quantity",
        "direction",
        "position_side",
        "offset",
    ] {
        if let (Some(old), Some(new)) = (old_data.get(field), data.get(field)) {
            let comparable =
                !old.is_null() && !new.is_null() && known(&old_data, field) && known(data, field);
            if comparable && !same_fact(field, old, new) {
                record["conflict"] = Value::Bool(true);
                merged.insert(field.into(), old.clone());
            }
        }
    }
    if previous.is_none() || trade_fact_changed(&old_data, &merged) {
        record["last_fact_seq"] = json!(seq);
    }
    record["data"] = Value::Object(merged);
    record["last_observed_seq"] = json!(seq);
    record["last_observed_time_us"] = json!(time);
    record
}

fn unassociated_trade(data: &Value) -> bool {
    identity(data.get("order_id")).is_none()
        || (identity(data.get("execution_id")).is_none()
            && !data.as_object().is_some_and(known_external))
}

fn order_set_basis(
    data: &Map<String, Value>,
    seq: u64,
    recording_id: &str,
) -> Option<(u64, u64, u64)> {
    if data.get("scope_complete")?.as_bool() != Some(true)
        || data.get("synchronized")?.as_bool() != Some(true)
    {
        return None;
    }
    let generation = data
        .get("sync_generation")?
        .as_u64()
        .filter(|value| *value <= JS_SAFE_INTEGER)?;
    let basis = data.get("query_basis")?.as_object()?;
    if basis
        .get("recording_id")
        .is_some_and(|value| value.as_str() != Some(recording_id))
    {
        return None;
    }
    if basis
        .get("sync_generation")
        .is_some_and(|value| value.as_u64() != Some(generation))
    {
        return None;
    }
    let start = basis
        .get("start_seq")?
        .as_u64()
        .filter(|value| *value < seq)?;
    let revision = basis
        .get("request_revision")?
        .as_u64()
        .filter(|value| *value > 0 && *value <= JS_SAFE_INTEGER)?;
    Some((generation, start, revision))
}

fn same_fact(field: &str, left: &Value, right: &Value) -> bool {
    if matches!(field, "price" | "quantity") {
        return left.as_f64() == right.as_f64();
    }
    if matches!(field, "direction" | "position_side" | "offset") {
        return left
            .as_str()
            .zip(right.as_str())
            .is_some_and(|(left, right)| left.eq_ignore_ascii_case(right));
    }
    left == right
}

fn trade_fact_changed(previous: &Map<String, Value>, current: &Map<String, Value>) -> bool {
    [
        "symbol",
        "price",
        "quantity",
        "direction",
        "position_side",
        "offset",
        "execution_id",
        "order_id",
        "origin",
    ]
    .iter()
    .any(|field| {
        let (previous, current) = (previous.get(*field), current.get(*field));
        match (previous, current) {
            (Some(previous), Some(current)) => !same_fact(field, previous, current),
            (None, None) => false,
            _ => true,
        }
    })
}

fn merge_map(map: &mut BTreeMap<String, Value>, key: String, value: Value) {
    let mut merged = map
        .get(&key)
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    if let Some(value) = value.as_object() {
        merge_known(&mut merged, value);
    }
    map.insert(key, Value::Object(merged));
}

fn compound(symbol: &str, identity: &str) -> String {
    json!([symbol, identity]).to_string()
}

fn topic_symbol(
    topic: &str,
    prefix: &str,
    data: &Map<String, Value>,
) -> Result<String, AnalysisError> {
    let symbol = topic
        .strip_prefix(prefix)
        .filter(|value| !value.is_empty())
        .ok_or_else(|| failure("topic lacks symbol"))?;
    if data
        .get("symbol")
        .and_then(Value::as_str)
        .is_some_and(|value| value != symbol)
    {
        return Err(failure("topic and payload symbol differ"));
    }
    Ok(symbol.into())
}

fn trade_amount(record: &Value) -> Option<(f64, f64)> {
    if record["conflict"].as_bool() == Some(true) {
        return None;
    }
    let data = record["data"].as_object()?;
    let quantity = number(data, "quantity").filter(|value| *value > 0.0)?;
    let price = number(data, "price")?;
    (quantity * price).is_finite().then_some((quantity, price))
}

fn empty_total(complete: bool) -> Value {
    json!({"quantity":0.0,"notional":0.0,"complete":complete})
}

fn add_total(total: &mut Value, field: &str, amount: f64) {
    let sum = total[field]
        .as_f64()
        .map(|previous| previous + amount)
        .filter(|value| value.is_finite());
    total[field] = sum.map_or(Value::Null, |value| json!(value));
    if sum.is_none() {
        total["complete"] = Value::Bool(false);
    }
}

fn incomplete_total(total: &mut Value, symbol: Option<&str>) {
    total["complete"] = Value::Bool(false);
    if let Some(symbols) = total["by_symbol"].as_object_mut() {
        for (key, value) in symbols {
            if symbol.is_none_or(|symbol| key == symbol) {
                value["complete"] = Value::Bool(false);
            }
        }
    }
}

fn known_external(data: &Map<String, Value>) -> bool {
    matches!(text(data, "origin").as_deref(), Some("manual" | "external"))
}

fn terminal(order: &Value) -> bool {
    order["terminal"].as_bool() == Some(true)
        || matches!(
            order["status"].as_str(),
            Some("filled" | "canceled" | "cancelled" | "rejected" | "expired")
        )
}

fn command_ownership(value: &Value) -> Option<(String, String, String)> {
    let data = value.as_object()?;
    if text(data, "operation").as_deref() != Some("place_order") {
        return None;
    }
    if ["symbol", "order_id", "execution_id"]
        .iter()
        .any(|field| !known(data, field))
    {
        return None;
    }
    Some((
        data.get("symbol")?.as_str()?.to_owned(),
        identity(data.get("order_id"))?,
        identity(data.get("execution_id"))?,
    ))
}

fn matches_order(data: &Value, symbol: &str, order_id: &str) -> bool {
    data["symbol"].as_str() == Some(symbol)
        && identity(data.get("order_id")).as_deref() == Some(order_id)
}

fn supplement_owner(value: &mut Value, execution_id: &str, seq: u64, time: u64) -> bool {
    let Some(data) = value.as_object_mut() else {
        return false;
    };
    if (identity(data.get("execution_id")).is_some() && known(data, "execution_id"))
        || known_external(data)
    {
        return false;
    }
    data.insert("execution_id".into(), json!(execution_id));
    data.insert("origin".into(), json!("execution"));
    data.insert("association_seq".into(), json!(seq));
    data.insert("association_time_us".into(), json!(time));
    let quality = data.entry("field_quality").or_insert_with(|| json!({}));
    if let Some(quality) = quality.as_object_mut() {
        quality.insert("execution_id".into(), json!("known"));
        quality.insert("origin".into(), json!("known"));
    }
    true
}

fn position_key(row: &Value) -> Option<String> {
    Some(compound(
        row["symbol"].as_str()?,
        row["position_side"].as_str()?,
    ))
}

fn normalize_basis(mut basis: Value) -> Value {
    let proof = &basis["proof"];
    let valid = match proof["kind"].as_str() {
        Some("included_trade_keys") => {
            proof["complete"] == true
                && proof["included_trade_keys"]
                    .as_array()
                    .is_some_and(|keys| keys.iter().all(|key| key.as_str().is_some()))
        }
        Some("source_watermark") => {
            proof["source"]
                .as_str()
                .is_some_and(|source| !source.is_empty())
                && proof["through"].as_u64().is_some()
        }
        _ => false,
    };
    if basis["status"] != "aligned" || !valid {
        basis = json!({"status":"unverified","proof":null});
    }
    basis
}

fn covered(proof: &Value, key: &str, data: &Value) -> Option<bool> {
    match proof["kind"].as_str() {
        Some("included_trade_keys") => proof["included_trade_keys"]
            .as_array()
            .map(|keys| keys.iter().any(|value| value.as_str() == Some(key))),
        Some("source_watermark") if data["source"] == proof["source"] => data["source_seq"]
            .as_u64()
            .zip(proof["through"].as_u64())
            .map(|(sequence, through)| sequence <= through),
        _ => None,
    }
}

fn apply_position_trade(position: &mut Value, record: &Value) -> bool {
    let Some((quantity, _)) = trade_amount(record) else {
        return false;
    };
    let Some(data) = record["data"].as_object() else {
        return false;
    };
    let (Some(side), Some(trade_side), Some(direction)) = (
        position["position_side"].as_str().map(str::to_lowercase),
        text(data, "position_side"),
        text(data, "direction"),
    ) else {
        return false;
    };
    if !matches!(side.as_str(), "net" | "long" | "short") {
        return false;
    }
    if !matches!(trade_side.as_str(), "net" | "long" | "short") {
        return false;
    }
    if trade_side != side {
        return true;
    }
    let offset = text(data, "offset");
    let delta = if side == "net" {
        match direction.as_str() {
            "buy" => quantity,
            "sell" => -quantity,
            _ => return false,
        }
    } else {
        let opening = offset.as_deref() == Some("open");
        let closing = matches!(
            offset.as_deref(),
            Some("close" | "closetoday" | "closeyesterday" | "close_today" | "close_yesterday")
        );
        if !opening && !closing {
            return false;
        }
        let expected_buy = (side == "long") == opening;
        if (direction == "buy") != expected_buy || !matches!(direction.as_str(), "buy" | "sell") {
            return false;
        }
        if opening {
            quantity
        } else {
            -quantity
        }
    };
    let Some(before) = position["quantity"].as_f64() else {
        return false;
    };
    let after = before + delta;
    if !after.is_finite() || (side != "net" && after < -1e-9) {
        return false;
    }
    position["quantity"] = json!(after);
    for (field, affected) in [
        (
            "today_quantity",
            matches!(
                offset.as_deref(),
                Some("open" | "closetoday" | "close_today")
            ),
        ),
        (
            "yesterday_quantity",
            matches!(
                offset.as_deref(),
                Some("closeyesterday" | "close_yesterday")
            ),
        ),
    ] {
        if let Some(before) = position[field].as_f64() {
            if affected {
                let after = before + delta;
                position[field] = if after >= -1e-9 {
                    json!(after)
                } else {
                    Value::Null
                };
            } else if offset.as_deref() == Some("close") {
                position[field] = Value::Null;
            }
        }
    }
    true
}

#[cfg(test)]
mod tests {
    use super::*;

    fn message(seq: u64, topic: &str, data: Value) -> Value {
        json!({"schema_version":1,"recording_id":"r","capture_seq":seq,"time_us":100+seq,"topic":topic,"data":data})
    }

    fn trade(key: &str, quantity: f64) -> Value {
        json!({"symbol":"A","trade_key":key,"identity_quality":"stable","order_id":"O","execution_id":"E",
            "quantity":quantity,"price":10,"direction":"BUY","position_side":"NET","offset":"OPEN"})
    }

    fn order(id: &str, remaining: f64) -> Value {
        json!({"symbol":"A","order_id":id,"sync_generation":1,"status":"working","quantity":10,
            "filled_quantity":10.0-remaining,"remaining_quantity":remaining,"confirmed_book":true})
    }

    #[test]
    fn checkpoint_keeps_trade_identity_and_association_accounting() {
        let mut state = Projection::new("r");
        state
            .apply(&message(
                1,
                "/execution",
                json!({"execution_id":"E","event":"started"}),
            ))
            .unwrap();
        state.apply(&message(2, "/trade", trade("T", 4.0))).unwrap();
        let mut restored = Projection::restore(state.snapshot().unwrap()).unwrap();
        restored
            .apply(&message(3, "/trade", trade("T", 4.0)))
            .unwrap();
        restored
            .apply(&message(4, "/trade", trade("U", 2.0)))
            .unwrap();
        let value = restored.snapshot().unwrap();
        assert_eq!(
            value["trade_accounting"]["execution_totals"]["E"]["by_symbol"]["A"]["quantity"],
            6.0
        );
        assert_eq!(
            value["trade_accounting"]["own_seen_keys"],
            json!(["T", "U"])
        );
        restored.apply(&message(5,"/trade",json!({"trade_key":"T","identity_quality":"stable","execution_id":"F","order_id":"O"}))).unwrap();
        let value = restored.snapshot().unwrap();
        assert_eq!(
            value["trade_accounting"]["execution_totals"]["E"]["by_symbol"]["A"]["quantity"],
            2.0
        );
        assert_eq!(
            value["trade_accounting"]["execution_totals"]["F"]["by_symbol"]["A"]["quantity"],
            4.0
        );
        assert!(value["trade_accounting"]["execution_totals"]["E"]
            .get("quantity")
            .is_none());
    }

    #[test]
    fn late_trade_does_not_blindly_add_to_an_account_observation() {
        let mut state = Projection::new("r");
        state
            .apply(&message(
                1,
                "/account_observation",
                json!({"scope":["A"],"scope_complete":true,
            "positions":[{"symbol":"A","position_side":"NET","quantity":4}]}),
            ))
            .unwrap();
        state.apply(&message(2, "/trade", trade("T", 4.0))).unwrap();
        let key = compound("A", "NET");
        let value = state.snapshot().unwrap();
        assert_eq!(value["observed_positions"][&key]["quantity"], 4);
        assert!(value["derived_positions"][&key]["quantity"].is_null());
        state.apply(&message(3,"/account_observation",json!({"scope":["A"],"scope_complete":true,
            "position_basis":{"status":"aligned","proof":{"kind":"included_trade_keys","complete":true,"included_trade_keys":["T"]}},
            "positions":[{"symbol":"A","position_side":"NET","quantity":4}]}))).unwrap();
        state.apply(&message(4, "/trade", trade("U", 2.0))).unwrap();
        assert_eq!(
            state.snapshot().unwrap()["derived_positions"][&key]["quantity"],
            6.0
        );
    }

    #[test]
    fn query_prefix_cannot_overwrite_concurrent_orders_or_fills() {
        let mut state = Projection::new("r");
        state
            .apply(&message(1, "/order", order("A", 10.0)))
            .unwrap();
        state
            .apply(&message(2, "/order", order("B", 10.0)))
            .unwrap();
        state.apply(&message(3, "/order", order("A", 6.0))).unwrap();
        state.apply(&message(4,"/order_set",json!({"scope":["A"],"scope_complete":true,"synchronized":true,
            "sync_generation":1,"query_basis":{"start_seq":1,"request_revision":1},"orders":[order("A",10.0)]}))).unwrap();
        let value = state.snapshot().unwrap();
        assert_eq!(
            value["orders"][compound("A", "A")]["remaining_quantity"],
            6.0
        );
        assert_eq!(value["orders"][compound("A", "B")]["confirmed_book"], true);
        let mut state = Projection::restore(value).unwrap();
        state.apply(&message(5,"/order_set",json!({"scope":["A"],"scope_complete":true,"synchronized":true,
            "sync_generation":1,"query_basis":{"start_seq":3,"request_revision":3},"orders":[order("B",10.0)]}))).unwrap();
        state.apply(&message(6,"/order_set",json!({"scope":["A"],"scope_complete":true,"synchronized":true,
            "sync_generation":1,"query_basis":{"start_seq":3,"request_revision":2},"orders":[order("A",10.0)]}))).unwrap();
        let value = state.snapshot().unwrap();
        assert_eq!(
            value["orders"][compound("A", "A")]["confirmation_state"],
            "absent"
        );
        assert_eq!(value["orders"][compound("A", "B")]["confirmed_book"], true);
    }

    #[test]
    fn partial_positions_and_old_generation_do_not_clear_or_revive() {
        let mut state = Projection::new("r");
        state.apply(&message(1,"/account_observation",json!({"scope":["A","B"],"scope_complete":true,
            "positions":[{"symbol":"A","position_side":"LONG","quantity":2},{"symbol":"B","position_side":"LONG","quantity":3}]}))).unwrap();
        state
            .apply(&message(
                2,
                "/account_observation",
                json!({"scope":["A"],"scope_complete":false,"positions":[]}),
            ))
            .unwrap();
        state
            .apply(&message(3, "/order", order("A", 10.0)))
            .unwrap();
        state
            .apply(&message(
                4,
                "/connection",
                json!({"scope":["A"],"sync_generation":2,"connected":false}),
            ))
            .unwrap();
        state
            .apply(&message(5, "/order", order("A", 10.0)))
            .unwrap();
        let value = state.snapshot().unwrap();
        assert_eq!(
            value["observed_positions"][compound("A", "LONG")]["quantity"],
            2
        );
        assert_eq!(
            value["observed_positions"][compound("B", "LONG")]["quantity"],
            3
        );
        assert_eq!(value["orders"][compound("A", "A")]["confirmed_book"], false);
    }

    #[test]
    fn quality_and_weak_identity_never_become_precise_facts() {
        let mut state = Projection::new("r");
        let initial = state.snapshot().unwrap();
        assert_eq!(initial["state_through_seq"], 0);
        assert!(initial["time_us"].is_null());
        assert_eq!(initial["trade_accounting"]["own_complete"], false);
        state
            .apply(&message(
                1,
                "/execution",
                json!({"execution_id":"E","event":"started"}),
            ))
            .unwrap();
        let mut value = trade("T", 4.0);
        value["field_quality"] = json!({"quantity":"invalid"});
        state.apply(&message(2, "/trade", value)).unwrap();
        let mut weak = trade("weak", 5.0);
        weak["identity_quality"] = json!("weak");
        weak["trade_key"] = Value::Null;
        state.apply(&message(3, "/trade", weak)).unwrap();
        let value = state.snapshot().unwrap();
        assert_eq!(
            value["trade_accounting"]["execution_totals"]["E"]["by_symbol"]["A"]["quantity"],
            0.0
        );
        assert_eq!(
            value["trade_accounting"]["execution_totals"]["E"]["complete"],
            false
        );
        assert_eq!(value["trade_accounting"]["own_seen_keys"], json!([]));
    }

    #[test]
    fn trade_reduces_order_remaining_without_adding_the_order_cumulative_again() {
        let mut state = Projection::new("r");
        state
            .apply(&message(1, "/order", order("O", 10.0)))
            .unwrap();
        state.apply(&message(2, "/trade", trade("T", 4.0))).unwrap();
        let key = compound("A", "O");
        let value = state.snapshot().unwrap();
        assert_eq!(value["orders"][&key]["filled_quantity"], 4.0);
        assert_eq!(value["orders"][&key]["remaining_quantity"], 6.0);
        assert_eq!(value["orders"][&key]["remaining_quantity_complete"], true);
        let mut restored = Projection::restore(value).unwrap();
        restored
            .apply(&message(3, "/order", order("O", 6.0)))
            .unwrap();
        restored
            .apply(&message(4, "/trade", trade("T", 4.0)))
            .unwrap();
        let value = restored.snapshot().unwrap();
        assert_eq!(value["orders"][&key]["filled_quantity"], 4.0);
        assert_eq!(value["orders"][&key]["remaining_quantity"], 6.0);
        assert_eq!(value["order_change_seq"][&key], 3);
    }

    #[test]
    fn execution_targets_and_totals_keep_each_symbols_units_separate() {
        let mut state = Projection::new("r");
        state
            .apply(&message(
                1,
                "/execution",
                json!({"execution_id":"E","phase":"started"}),
            ))
            .unwrap();
        state
            .apply(&message(
                2,
                "/execution",
                json!({"execution_id":"E","phase":"target","symbol":"A","target_quantity":10}),
            ))
            .unwrap();
        state
            .apply(&message(
                3,
                "/execution",
                json!({"execution_id":"E","phase":"target","symbol":"B","target_quantity":20}),
            ))
            .unwrap();
        state.apply(&message(4, "/trade", trade("T", 4.0))).unwrap();
        let mut other = trade("U", 2.0);
        other["symbol"] = json!("B");
        state.apply(&message(5, "/trade", other)).unwrap();
        let value = state.snapshot().unwrap();
        assert_eq!(
            value["executions"]["E"]["by_symbol"]["A"]["target_quantity"],
            10
        );
        assert_eq!(
            value["executions"]["E"]["by_symbol"]["B"]["target_quantity"],
            20
        );
        let total = &value["trade_accounting"]["execution_totals"]["E"];
        assert_eq!(total["by_symbol"]["A"]["quantity"], 4.0);
        assert_eq!(total["by_symbol"]["B"]["quantity"], 2.0);
        assert!(total.get("quantity").is_none());
        assert!(total.get("notional").is_none());
    }

    #[test]
    fn a_source_watermark_does_not_classify_a_trade_without_its_source_sequence() {
        let mut state = Projection::new("r");
        state.apply(&message(1,"/account_observation",json!({"scope":["A"],"scope_complete":true,
            "position_basis":{"status":"aligned","proof":{"kind":"source_watermark","source":"matching-engine","through":5}},
            "positions":[{"symbol":"A","position_side":"NET","quantity":4}]}))).unwrap();
        let mut value = trade("T", 4.0);
        value["source"] = json!("matching-engine");
        state.apply(&message(2, "/trade", value)).unwrap();
        let key = compound("A", "NET");
        assert!(state.snapshot().unwrap()["derived_positions"][&key]["quantity"].is_null());
        state
            .apply(&message(
                3,
                "/trade",
                json!({"trade_key":"T","identity_quality":"stable","source_seq":6}),
            ))
            .unwrap();
        assert_eq!(
            state.snapshot().unwrap()["derived_positions"][&key]["quantity"],
            8.0
        );
    }

    #[test]
    fn canonical_account_scope_applies_to_existing_symbols_with_an_empty_symbol_list() {
        let mut state = Projection::new("r");
        state
            .apply(&message(1, "/order", order("O", 10.0)))
            .unwrap();
        state
            .apply(&message(
                2,
                "/account_observation",
                json!({
                    "scope":{"kind":"account","symbols":[]},"scope_complete":true,
                    "positions":[{"symbol":"A","position_side":"LONG","quantity":3},
                        {"symbol":"B","position_side":"LONG","quantity":5}]
                }),
            ))
            .unwrap();
        state
            .apply(&message(
                3,
                "/quote/B",
                json!({"symbol":"B","book_valid":true}),
            ))
            .unwrap();
        state
            .apply(&message(
                4,
                "/connection",
                json!({
                    "scope":{"kind":"account","symbols":[]},"sync_generation":2,"connected":false
                }),
            ))
            .unwrap();
        let value = state.snapshot().unwrap();
        assert_eq!(value["orders"][compound("A", "O")]["confirmed_book"], false);
        assert_eq!(value["quotes"]["B"]["book_valid"], false);
        state
            .apply(&message(
                5,
                "/order_set",
                json!({
                    "scope":{"kind":"account","symbols":[]},"scope_complete":true,
                    "synchronized":true,"sync_generation":2,
                    "query_basis":{"start_seq":4,"request_revision":1},"orders":[order("O",10.0)]
                }),
            ))
            .unwrap();
        state
            .apply(&message(
                6,
                "/order_set",
                json!({
                    "scope":{"kind":"account","symbols":[]},"scope_complete":true,
                    "synchronized":true,"sync_generation":2,
                    "query_basis":{"start_seq":5,"request_revision":2},"orders":[]
                }),
            ))
            .unwrap();
        assert_eq!(
            state.snapshot().unwrap()["orders"][compound("A", "O")]["confirmation_state"],
            "absent"
        );
        state
            .apply(&message(
                7,
                "/account_observation",
                json!({
                    "scope":{"kind":"symbols","symbols":["A"]},"scope_complete":true,"positions":[]
                }),
            ))
            .unwrap();
        assert_eq!(
            state.snapshot().unwrap()["observed_positions"][compound("B", "LONG")]["quantity"],
            5
        );
        state
            .apply(&message(
                8,
                "/account_observation",
                json!({
                    "scope":{"kind":"account","symbols":[]},"scope_complete":true,"positions":[]
                }),
            ))
            .unwrap();
        let value = state.snapshot().unwrap();
        assert_eq!(
            value["observed_positions"][compound("A", "LONG")]["quantity"],
            0
        );
        assert_eq!(
            value["observed_positions"][compound("B", "LONG")]["quantity"],
            0
        );
    }

    #[test]
    fn a_late_place_command_associates_a_trade_once_without_backdating_or_confirming_orders() {
        let mut state = Projection::new("r");
        state
            .apply(&message(
                1,
                "/execution",
                json!({"execution_id":"E","event":"started","symbols":["A"]}),
            ))
            .unwrap();
        let mut unassociated = trade("T", 4.0);
        unassociated["execution_id"] = Value::Null;
        unassociated["origin"] = json!("unknown");
        state.apply(&message(2, "/trade", unassociated)).unwrap();
        let before = state.snapshot().unwrap();
        assert_eq!(
            before["trade_accounting"]["execution_totals"]["E"]["by_symbol"]["A"]["quantity"],
            0.0
        );
        assert!(before["trade_accounting"]["own_records"]["T"]["data"]["execution_id"].is_null());
        let mapping = json!({"command_id":"C","operation":"place_order","phase":"result","execution_id":"E","symbol":"A","order_id":"O"});
        state
            .apply(&message(3, "/command", mapping.clone()))
            .unwrap();
        state.apply(&message(4, "/command", mapping)).unwrap();
        state
            .apply(&message(
                5,
                "/command",
                json!({"command_id":null,"phase":"native_error","success":false}),
            ))
            .unwrap();
        let value = state.snapshot().unwrap();
        assert_eq!(
            value["trade_accounting"]["execution_totals"]["E"]["by_symbol"]["A"]["quantity"],
            4.0
        );
        assert_eq!(
            value["trade_accounting"]["own_records"]["T"]["first_capture_seq"],
            2
        );
        assert_eq!(
            value["trade_accounting"]["own_records"]["T"]["first_time_us"],
            102
        );
        assert_eq!(
            value["trade_accounting"]["own_records"]["T"]["last_observed_seq"],
            2
        );
        assert!(value["orders"].as_object().unwrap().is_empty());
        assert!(value["commands"].get("unknown-command:5").is_some());
        let restored = Projection::restore(before).unwrap().snapshot().unwrap();
        assert!(restored["trade_accounting"]["own_records"]["T"]["data"]["execution_id"].is_null());
    }

    #[test]
    fn cancel_context_does_not_take_ownership_of_an_unassociated_external_order() {
        let mut state = Projection::new("r");
        let mut unassociated = trade("T", 4.0);
        unassociated["execution_id"] = Value::Null;
        state.apply(&message(1, "/trade", unassociated)).unwrap();
        state
            .apply(&message(
                2,
                "/command",
                json!({"command_id":"C","operation":"cancel_order","phase":"result",
            "execution_id":"E","symbol":"A","order_id":"O","success":true}),
            ))
            .unwrap();
        assert!(
            state.snapshot().unwrap()["trade_accounting"]["own_records"]["T"]["data"]
                ["execution_id"]
                .is_null()
        );
    }
}
