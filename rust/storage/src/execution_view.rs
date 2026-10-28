//! 从冻结事实临时组装执行详情；不保存摘要或对账副本。
use serde_json::{json, Value};
use std::collections::HashMap;

const EPS: f64 = 1e-9;

fn number(value: &Value) -> Option<f64> {
    value
        .as_f64()
        .or_else(|| value.as_str()?.parse().ok())
        .filter(|v| v.is_finite())
}

fn array(value: &Value) -> &[Value] {
    value.as_array().map(Vec::as_slice).unwrap_or(&[])
}

fn text(value: &Value) -> &str {
    value.as_str().unwrap_or("")
}

fn source(assets: &Value) -> &str {
    if !assets.is_object() {
        "unavailable"
    } else {
        assets["source"]
            .as_str()
            .filter(|s| !s.is_empty())
            .unwrap_or("real")
    }
}

fn degraded(assets: &Value) -> bool {
    matches!(source(assets), "assumed" | "error" | "unavailable")
}

fn equity(assets: &Value) -> Option<f64> {
    (!degraded(assets))
        .then(|| number(&assets["total_asset"]))
        .flatten()
}

fn positions(assets: &Value) -> HashMap<&str, f64> {
    let mut quantities = HashMap::new();
    for position in array(&assets["positions"]) {
        let symbol = text(&position["symbol"]);
        if symbol.is_empty() {
            continue;
        }
        let volume = number(&position["volume"]).unwrap_or(0.0).abs();
        let direction = text(&position["direction"]);
        let sign = if direction.contains('空') || direction.to_lowercase().contains("short") {
            -1.0
        } else {
            1.0
        };
        *quantities.entry(symbol).or_default() += sign * volume;
    }
    quantities
}

fn observed(assets: &Value, positions: &HashMap<&str, f64>, symbol: &str) -> Option<f64> {
    if degraded(assets) || !assets["positions"].is_array() {
        return None;
    }
    let quantity = positions.get(symbol).copied().unwrap_or(0.0);
    quantity.is_finite().then_some(quantity)
}

fn converted(value: Option<f64>, conversion: &Value) -> Option<f64> {
    if !conversion.is_object() {
        return value;
    }
    Some(value? * number(&conversion["rate"]).filter(|rate| *rate > 0.0)?)
}

fn side(direction: &Value) -> &'static str {
    let direction = text(direction).to_uppercase();
    if direction.contains("SELL") || direction.contains('卖') {
        "sell"
    } else if direction.contains("BUY") || direction.contains('买') {
        "buy"
    } else {
        "none"
    }
}

fn filled(orders: &Value) -> (f64, Option<f64>, f64, Option<f64>) {
    let (mut quantity, mut value, mut native) = (0.0, Some(0.0), 0.0);
    for order in array(orders).iter().filter(|o| o.is_object()) {
        let sign = if side(&order["direction"]) == "sell" {
            -1.0
        } else {
            1.0
        };
        let volume = sign * number(&order["filled_volume"]).unwrap_or(0.0).abs();
        let amount = volume * number(&order["avg_price"]).unwrap_or(0.0);
        quantity += volume;
        native += amount;
        value = value
            .zip(converted(Some(amount), &order["value_conversion"]))
            .map(|(a, b)| a + b);
    }
    let average = (quantity.abs() > EPS).then(|| native.abs() / quantity.abs());
    (quantity, value, native, average)
}

fn fee(trade: &Value) -> (f64, Option<&str>) {
    (
        number(&trade["extra"]["commission"]).unwrap_or(0.0),
        trade["extra"]["commission_asset"]
            .as_str()
            .filter(|s| !s.is_empty()),
    )
}

fn order_id(value: &Value) -> String {
    match &value["order_id"] {
        Value::String(id) => id.clone(),
        Value::Number(id) => id.to_string(),
        _ => String::new(),
    }
}

fn orders_tree(result: &Value) -> Vec<Value> {
    let mut by_order: HashMap<String, Vec<Value>> = HashMap::new();
    for trade in array(&result["trades"]).iter().filter(|t| t.is_object()) {
        let (fee, asset) = fee(trade);
        let native = number(&trade["trade_value"]);
        let conversion = &trade["value_conversion"];
        by_order.entry(order_id(trade)).or_default().push(json!({
            "price":number(&trade["trade_price"]),"volume":number(&trade["trade_volume"]),
            "value":converted(native,conversion),"native_value":native,
            "price_currency":conversion["price_currency"],"value_currency":conversion["value_currency"],
            "time":trade["trade_time"],"fee":fee,"fee_asset":asset
        }));
    }
    array(&result["orders"])
        .iter()
        .filter(|o| o.is_object())
        .map(|order| {
            let id = order_id(order);
            json!({
                "order_id":id,"side":side(&order["direction"]),
                "order_type":text(&order["order_type"]).rsplit('.').next().unwrap_or(""),
                "price":number(&order["price"]),"avg_price":number(&order["avg_price"]),
                "value_conversion":order["value_conversion"],"volume":number(&order["volume"]),
                "filled_volume":number(&order["filled_volume"]),"status":text(&order["status"]),
                "client_order_id":order["extra"]["client_order_id"].as_str(),
                "trades":by_order.get(&id).cloned().unwrap_or_default()
            })
        })
        .collect()
}

fn liquidity(
    average: Option<f64>,
    bid: Option<f64>,
    ask: Option<f64>,
    sell: bool,
) -> Option<&'static str> {
    let (average, bid, ask) = (
        average?,
        bid.filter(|v| *v > 0.0)?,
        ask.filter(|v| *v > 0.0)?,
    );
    let tolerance = if ask > bid {
        (ask - bid) / 2.0
    } else {
        ask * 1e-4
    };
    if sell && average >= ask - tolerance || !sell && average <= bid + tolerance {
        Some("passive")
    } else if sell && average <= bid + tolerance || !sell && average >= ask - tolerance {
        Some("aggressive")
    } else {
        Some("mixed")
    }
}

fn tca(result: &Value, average: Option<f64>, sell: bool) -> Value {
    let orders = array(&result["orders"]);
    let trades = array(&result["trades"]);
    let tick = &result["first_tick"];
    let bid = number(&tick["bid_price"]);
    let ask = number(&tick["ask_price"]);
    let mid = bid
        .zip(ask)
        .filter(|(b, a)| *b > 0.0 && *a > 0.0)
        .map(|(b, a)| (b + a) / 2.0)
        .or_else(|| number(&tick["last_price"]));
    let slippage = average
        .zip(mid)
        .filter(|(_, m)| *m > 0.0)
        .map(|(a, m)| (a - m) / m * 1e4 * if sell { 1.0 } else { -1.0 });
    let (mut ordered, mut filled) = (0.0, 0.0);
    for order in orders.iter().filter(|o| o.is_object()) {
        ordered += number(&order["volume"]).unwrap_or(0.0).abs();
        filled += number(&order["filled_volume"]).unwrap_or(0.0).abs();
    }
    let (mut fees, mut asset) = (0.0, None);
    for trade in trades.iter().filter(|t| t.is_object()) {
        let (amount, currency) = fee(trade);
        fees += amount;
        asset = asset.or(currency);
    }
    json!({"n_orders":orders.len(),"n_trades":trades.len(),"arrival_bid":bid,"arrival_ask":ask,
        "arrival_mid":mid,"slippage_bps":slippage,"liquidity":liquidity(average,bid,ask,sell),
        "fill_ratio":(ordered>EPS).then(|| filled/ordered),"fee":fees,"fee_asset":asset})
}

fn symbol_row(
    symbol: &str,
    result: &Value,
    before: Option<f64>,
    after: Option<f64>,
    currency: &Value,
) -> Value {
    let target = number(&result["target_volume"]);
    let (filled, value, native, average) = filled(&result["orders"]);
    let moved = after.zip(before).map(|(a, b)| a - b);
    let (ratio, reached) = match target.zip(after) {
        Some((target, after)) if target.abs() <= EPS => {
            let reached = after.abs() <= before.unwrap_or(0.0).abs().max(1.0) * 0.01;
            (reached.then_some(1.0), Some(reached))
        }
        Some((target, after)) => (
            Some(after / target),
            Some((after - target).abs() <= target.abs() * 0.01),
        ),
        None => (None, None),
    };
    let sizing = result["sizing"].as_object();
    let conversion = &result["sizing"]["value_conversion"];
    let currency_or = |key: &str| {
        conversion[key]
            .as_str()
            .filter(|s| !s.is_empty())
            .map_or_else(|| currency.clone(), |s| json!(s))
    };
    json!({"symbol":symbol,"status":result["status"],"error":result["error"],
        "outcome":result["outcome"],"outcome_reason":result["outcome_reason"],"final_volume":result["final_volume"],
        "target":target,"filled":filled,"filled_value":value,"value_currency":currency_or("value_currency"),
        "filled_native_value":native,"native_currency":currency_or("price_currency"),"avg_price":average,
        "before":before,"after":after,"moved":moved,"drift":moved.map(|m|m-filled),
        "attained_ratio":ratio,"reached":reached,"sizing":sizing,"tca":tca(result,average,filled<0.0),
        "orders":orders_tree(result)})
}

pub(crate) fn reconciliation(result: &Value, before: &Value) -> Value {
    let after = &result["account_assets"];
    let before_positions = positions(before);
    let after_positions = positions(after);
    let mut symbols = Vec::new();
    if let Some(results) = result["symbol_results"].as_object() {
        for (symbol, value) in results.iter().filter(|(_, v)| v.is_object()) {
            let before_qty = observed(before, &before_positions, symbol);
            let after_qty = observed(after, &after_positions, symbol)
                .or_else(|| number(&value["final_volume"]));
            symbols.push(symbol_row(
                symbol,
                value,
                before_qty,
                after_qty,
                &after["currency"],
            ));
        }
    }
    json!({"account":{"equity_before":equity(before),"equity_after":equity(after),
        "source_before":source(before),"source_after":source(after)},"symbols":symbols})
}

pub(crate) fn build(result: &Value, before: &Value) -> Value {
    let before = if before.is_null() {
        &result["before_account_assets"]
    } else {
        before
    };
    let before = if before.as_object().is_some_and(|o| o.is_empty()) {
        &Value::Null
    } else {
        before
    };
    let mut view = json!({"success":result["success"],"execution_time":result["execution_time"],
        "summary":{},"outcome_symbols":[],"reconciliation":reconciliation(result,before)});
    for key in [
        "status",
        "error",
        "reason_code",
        "outcome",
        "outcome_reason",
        "symbol_results",
    ] {
        if let Some(value) = result.get(key) {
            view[key] = value.clone();
        }
    }
    if let Some(memory) = result["memory"].as_object() {
        let memory: serde_json::Map<String, Value> = memory
            .iter()
            .filter(|(key, v)| {
                matches!(key.as_str(), "message" | "symbol_decision_reason_code") && v.is_string()
            })
            .map(|(k, v)| (k.clone(), v.clone()))
            .collect();
        if !memory.is_empty() {
            view["memory"] = json!(memory);
        }
    }
    if let Some(symbols) = result["symbol_results"]
        .as_object()
        .filter(|s| !s.is_empty())
    {
        let failed = symbols
            .values()
            .filter(|v| matches!(text(&v["status"]), "BLOCKED" | "PARTIAL" | "FAILED"))
            .count();
        let noop = symbols.values().filter(|v| v["status"] == "NOOP").count();
        view["summary"] = json!({"symbols_total":symbols.len(),"symbols_succeeded":symbols.len()-failed,"symbols_failed":failed,"symbols_noop":noop});
        view["outcome_symbols"] = json!(symbols
            .iter()
            .filter(|(_, v)| matches!(text(&v["outcome"]), "not_reached" | "blocked"))
            .map(|(k, _)| k)
            .collect::<Vec<_>>());
    }
    view
}
