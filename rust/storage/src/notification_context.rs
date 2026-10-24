//! 唯一的执行通知投影；正常执行、预览和补发共用，不打开数据库。

use crate::business_facts_ops::encode::is_truthy as truthy;
use crate::error::AnalysisError;
use crate::json_codec::number_value;
use serde_json::{json, Map, Value};

pub(crate) fn build(request: &Value) -> Result<Value, AnalysisError> {
    let text = |field| {
        request.get(field).and_then(Value::as_str).ok_or_else(|| {
            AnalysisError::new("NOTIFICATION_INPUT", format!("{field} must be text"))
        })
    };
    let now = text("now")?;
    let account = json!({"name":text("account_mark")?});
    let result = &request["result"];
    let inputs = &result["inputs"];
    let operations = array(&request["trade_operations"]);
    let mut context =
        result_context(now, &account, result, inputs, operations).ok_or_else(|| {
            AnalysisError::new(
                "NOTIFICATION_INPUT",
                "invalid execution notification result",
            )
        })?;
    let audit = &inputs["extra"]["audit"];
    for (key, source) in [
        ("id", "execution_id"),
        ("kind", "execution_kind"),
        ("trigger_source", "trigger_source"),
    ] {
        context["execution"][key] = audit[source]
            .as_str()
            .map_or(Value::Null, |value| json!(value));
    }
    let is_test = request["is_test"].as_bool().unwrap_or(false);
    context["execution"]["notified_at"] = context["default_feishu_variables"]["dt"].clone();
    context["execution"]["channel_type"] = result["channel_type"].clone();
    context["execution"]["is_test"] = json!(is_test);
    context["is_test"] = json!(is_test);
    context["event_type"] = json!("execution.finished");
    context["event_id"] = context["execution"]["id"]
        .as_str()
        .filter(|id| !id.is_empty())
        .map_or(Value::Null, |id| json!(format!("execution:{id}")));
    Ok(context)
}

/// 历史展示直接使用持久化字段，不构造会添加当前时间的执行模型。
pub(crate) fn preview(
    detail: &Value,
    account_mark: &str,
    now: &str,
    created_at: &str,
) -> Result<Value, AnalysisError> {
    let captured = &detail["result"]["extra"]["server_notification_context"];
    if captured
        .as_object()
        .is_some_and(|context| !context.is_empty())
    {
        return Ok(captured.clone());
    }
    let mut result = fill_missing_fields(&detail["result"])?;
    if !result.is_object() {
        return Err(AnalysisError::new(
            "NOTIFICATION_INPUT",
            "invalid execution notification result",
        ));
    }
    let inputs = result.get("inputs").unwrap_or(&detail["input"]).clone();
    result["inputs"] = inputs;
    let mut request = json!({"account_mark":account_mark,"now":now,"is_test":true,"trade_operations":[],"result":null});
    request["result"] = result;
    let mut context = build(&request)?;
    context["execution"]["notified_at"] = json!(created_at);
    Ok(context)
}

fn fill_missing_fields(value: &Value) -> Result<Value, AnalysisError> {
    let mut normalized = crate::business_facts_ops::result_head::normalize_legacy_result(value)?;
    if let (Some(source), Some(target)) = (value.as_object(), normalized.as_object_mut()) {
        target.extend(source.clone());
        if let Some(symbols) = target
            .get_mut("symbol_results")
            .and_then(Value::as_object_mut)
        {
            for symbol in symbols.values_mut() {
                *symbol = fill_missing_fields(symbol)?;
            }
        }
    }
    Ok(normalized)
}

fn successful(value: &Value) -> bool {
    matches!(value.as_str(), Some("SUCCEEDED" | "NOOP"))
}

fn outcome(status: &Value) -> Value {
    json!(match status.as_str() {
        Some("SUCCEEDED" | "NOOP") => "completed",
        Some("PARTIAL") => "not_reached",
        Some("BLOCKED") => "blocked",
        Some("FAILED") => "error",
        _ => "unknown",
    })
}

fn result_metadata(result: &Value, default_status: &str) -> Value {
    let status = result.get("status").cloned().unwrap_or_else(|| {
        json!(match result["outcome"].as_str() {
            Some("completed") => "SUCCEEDED",
            Some("not_reached") => "PARTIAL",
            Some("error") => "FAILED",
            Some("blocked") => "BLOCKED",
            _ => default_status,
        })
    });
    let error = if result.get("error").is_none() && !successful(&status) {
        result
            .get("outcome_reason")
            .or_else(|| result.get("msg"))
            .or_else(|| result["memory"].get("message"))
    } else {
        result.get("error")
    }
    .cloned()
    .unwrap_or(Value::Null);
    let mut reason = result["reason_code"].clone();
    if result.get("reason_code").is_none() {
        reason = result["memory"]["symbol_decision_reason_code"].clone();
        if matches!(reason.as_str(), Some("CLOSED" | "CTP.SESSION.CLOSED"))
            || (status == "BLOCKED"
                && matches!(
                    error.as_str(),
                    Some("非交易时段" | "当前不在交易时段" | "当前不在交易时间")
                ))
        {
            reason = json!("COMMON.SESSION.CLOSED")
        }
    }
    json!({"status":status,"success":successful(&status),"error":error,"reason_code":reason,"outcome":result.get("outcome").filter(|value| !value.is_null()).cloned().unwrap_or_else(|| outcome(&status)),"outcome_reason":result.get("outcome_reason").cloned().unwrap_or(error)})
}

fn empty_context(row: &Value, account: &Value) -> Value {
    json!({
        "account":{"id":account["id"],"name":account["name"],"mark":account["name"]},"execution":{},"strategy":{},"assets":{},"targets":{},"positions":null,"orders":[],"trades":[],"symbols":[],
        "summary":{"symbol_count":0,"position_count":null,"order_count":0,"filled_order_count":0,"active_order_count":0,"trade_count":0,"trade_value":0.0,"succeeded_symbol_count":0,"failed_symbol_count":0},
        "default_feishu_variables":{"account_mark":account["name"],"dt":row["finished_at"],"algorithm":row.get("algorithm").filter(|value| truthy(value)).cloned().unwrap_or(json!("Unknown")),"total_assets":"未获取","available_cash":"未获取","market_value":"未获取","positions":[],"trades":[]}
    })
}

fn redact(value: &Value) -> Value {
    match value {
        Value::Object(values) => Value::Object(
            values
                .iter()
                .filter(|(key, _)| {
                    !["key", "secret", "token", "password", "credential"]
                        .iter()
                        .any(|marker| key.to_lowercase().contains(marker))
                })
                .map(|(key, value)| (key.clone(), redact(value)))
                .collect(),
        ),
        Value::Array(values) => Value::Array(values.iter().map(redact).collect()),
        _ => value.clone(),
    }
}

fn array(value: &Value) -> &[Value] {
    value.as_array().map_or(&[], Vec::as_slice)
}

fn projected(value: &Value, defaults: Value, keys: &[&str]) -> Value {
    let mut result = defaults;
    for key in keys {
        if let Some(value) = value.get(key) {
            result[*key] = value.clone()
        }
    }
    result
}

fn float_fields(value: &mut Value, keys: &[&str]) {
    for key in keys {
        if let Some(number) = number(&value[*key]) {
            value[*key] = number_value(number)
        }
    }
}

fn number(value: &Value) -> Option<f64> {
    value
        .as_f64()
        .or_else(|| match value {
            Value::Number(value) => value.to_string().parse::<f64>().ok(),
            _ => None,
        })
        .or_else(|| value.as_str().and_then(|value| value.parse::<f64>().ok()))
        .or_else(|| value.as_bool().map(|value| if value { 1.0 } else { 0.0 }))
}

fn projected_order(order: &Value, now: &str) -> Value {
    let mut result = projected(
        order,
        json!({"filled_volume":0.0,"avg_price":0.0,"value_conversion":null,"create_time":now}),
        &[
            "order_id",
            "symbol",
            "direction",
            "order_type",
            "volume",
            "price",
            "status",
            "filled_volume",
            "avg_price",
            "value_conversion",
            "create_time",
            "update_time",
        ],
    );
    let volume = number(&order["volume"]).unwrap_or(0.0);
    let filled = number(&order["filled_volume"]).unwrap_or(0.0);
    float_fields(
        &mut result,
        &["volume", "price", "filled_volume", "avg_price"],
    );
    result["remaining_volume"] = number_value((volume - filled).max(0.0));
    result["filled_ratio"] = number_value(if volume > 0.0 { filled / volume } else { 0.0 });
    result
}

fn projected_trade(trade: &Value) -> Value {
    let mut result = projected(
        trade,
        json!({"symbol":"","order_id":"","value_conversion":null}),
        &[
            "trade_id",
            "symbol",
            "order_id",
            "trade_time",
            "trade_volume",
            "trade_price",
            "trade_value",
            "value_conversion",
        ],
    );
    float_fields(&mut result, &["trade_volume", "trade_price", "trade_value"]);
    result
}

fn symbol_context(symbol: &str, result: &Value) -> Value {
    let mut context = result_metadata(result, "SUCCEEDED");
    for key in ["target_volume", "sizing", "first_tick"] {
        context[key] = result[key].clone()
    }
    context["symbol"] = json!(symbol);
    context["algorithm"] = result.get("algorithm").cloned().unwrap_or(json!(""));
    for (target, source) in [("order_count", "orders"), ("trade_count", "trades")] {
        context[target] = json!(array(&result[source]).len())
    }
    if let Some(tick) = context["first_tick"].as_object_mut() {
        tick.remove("extra");
    }
    context
}

fn ratio(value: &Value, total: Option<f64>) -> Value {
    match (number(value), total) {
        (Some(value), Some(total)) if total > 0.0 => number_value(value / total),
        _ => Value::Null,
    }
}

fn asset_context(result: &Value, inputs: &Value, target: &Value) -> (Value, Value) {
    let asset = &result["account_assets"];
    let usable = asset.is_object()
        && !matches!(
            asset["source"].as_str(),
            Some("assumed" | "error" | "unavailable")
        );
    if !usable {
        return (
            json!({"total_assets":null,"available_cash":null,"market_value":null,"currency":null,"update_time":null,"source":"unavailable","cash_rate":null,"position_rate":null}),
            Value::Null,
        );
    }
    let total = number(&asset["total_asset"]);
    let positions: Vec<_> = array(&asset["positions"])
        .iter()
        .map(|position| {
            let mut value = projected(
                position,
                json!({"avg_price":null}),
                &[
                    "symbol",
                    "volume",
                    "available_volume",
                    "market_value",
                    "direction",
                    "avg_price",
                ],
            );
            float_fields(
                &mut value,
                &["volume", "available_volume", "market_value", "avg_price"],
            );
            let symbol = position["symbol"].as_str().unwrap_or("");
            value["target_volume"] = target.get(symbol).cloned().unwrap_or(json!(0));
            value["target_weight"] = inputs["curr_target"][symbol].clone();
            value["rate"] = ratio(&position["market_value"], total);
            value
        })
        .collect();
    let mut context = json!({"total_assets":null,"available_cash":null,"market_value":null,"currency":"CNY","update_time":null,"source":"real","cash_rate":null,"position_rate":null});
    for (target, source) in [
        ("total_assets", "total_asset"),
        ("available_cash", "available_cash"),
        ("market_value", "market_value"),
        ("currency", "currency"),
        ("update_time", "update_time"),
        ("source", "source"),
    ] {
        if let Some(value) = asset.get(source) {
            context[target] = value.clone();
        }
    }
    context["cash_rate"] = ratio(&asset["available_cash"], total);
    context["position_rate"] = ratio(&asset["market_value"], total);
    let positions = Value::Array(positions);
    float_fields(
        &mut context,
        &["total_assets", "available_cash", "market_value"],
    );
    (context, positions)
}

fn formatted_number(value: f64, places: usize) -> String {
    if value.is_nan() {
        "nan".into()
    } else {
        format!("{value:.places$}")
    }
}

fn decimal(value: &Value, places: usize, missing: &str) -> String {
    number(value)
        .map(|value| formatted_number(value, places))
        .unwrap_or_else(|| missing.into())
}

fn legacy_variables(
    account: &Value,
    now: &str,
    assets: &Value,
    positions: &Value,
    orders: &[Value],
    trades: &[Value],
    inputs: &Value,
    operations: &[Value],
) -> Value {
    let total = number(&assets["total_assets"]);
    let usable = !positions.is_null();
    let legacy_positions = positions.as_array().map(|positions| positions.iter().filter(|position| number(&position["market_value"]).map_or(true,|value| !(value<=0.0))).map(|position| {
        json!({"symbol":position["symbol"],"direction":position["direction"],"market_value":decimal(&position["market_value"],2,"暂无有效行情"),"volume":decimal(&position["volume"],4,"0.0000"),"target_volume":decimal(&position["target_volume"],4,"0.0000"),"rate":match (number(&position["market_value"]),total) {(Some(value),Some(total)) if total>0.0=>format!("{}%",formatted_number(value/total*100.0,2)),_=>"—".into()}})
    }).collect::<Vec<_>>()).unwrap_or_else(|| vec![json!({"symbol":"未获取","direction":"—","market_value":"未获取","volume":"—","target_volume":"—","rate":"—"})]);
    let legacy_trades: Vec<_> = trades.iter().enumerate().map(|(index,trade)| {
        let order = orders.iter().rev().find(|order| order["symbol"]==trade["symbol"] && order["order_id"]==trade["order_id"]);
        json!({"symbol":trade["symbol"],"dt":trade["trade_time"],"operate":operations.get(index).and_then(Value::as_str).unwrap_or_else(|| order.map(|order| if order["direction"]=="BUY" {"买入"} else {"卖出"}).unwrap_or("-")),"volume":decimal(&trade["trade_volume"],4,"0.0000"),"price":decimal(&trade["trade_price"],4,"0.0000"),"order_id":trade["order_id"],"trade_id":trade["trade_id"]})
    }).collect();
    json!({"account_mark":account["name"],"dt":now.get(..19).unwrap_or("").replace('T'," "),"algorithm":inputs["algorithm"].get("method").cloned().unwrap_or(json!("Unknown")),"total_assets":decimal(&assets["total_assets"],2,"未获取"),"available_cash":decimal(&assets["available_cash"],2,"未获取"),"market_value":decimal(&assets["market_value"],2,if usable {"暂无有效行情"} else {"未获取"}),"positions":legacy_positions,"trades":legacy_trades})
}

fn complete_metadata(
    metadata: &mut Value,
    projected_symbols: &[Value],
    symbols: &Map<String, Value>,
) {
    if metadata["error"].is_null() && !successful(&metadata["status"]) {
        metadata["error"] = projected_symbols
            .iter()
            .find(|symbol| !successful(&symbol["status"]) && !symbol["error"].is_null())
            .map(|symbol| symbol["error"].clone())
            .unwrap_or(Value::Null)
    }
    if metadata["reason_code"].is_null() && metadata["status"] == "BLOCKED" {
        let mut failed = projected_symbols
            .iter()
            .filter(|symbol| !successful(&symbol["status"]));
        if let Some(first) = failed
            .clone()
            .next()
            .filter(|symbol| symbol["reason_code"].is_string())
        {
            let same = failed.all(|symbol| symbol["reason_code"] == first["reason_code"]);
            let has_orders = symbols.values().any(|symbol| {
                !successful(&symbol["status"])
                    && (truthy(&symbol["orders"]) || truthy(&symbol["trades"]))
            });
            if same && !(first["reason_code"] == "COMMON.SESSION.CLOSED" && has_orders) {
                metadata["reason_code"] = first["reason_code"].clone();
            }
        }
    }
    if metadata["outcome_reason"].is_null() {
        metadata["outcome_reason"] = projected_symbols
            .iter()
            .find(|symbol| {
                symbol["outcome"] == metadata["outcome"] && truthy(&symbol["outcome_reason"])
            })
            .map(|symbol| symbol["outcome_reason"].clone())
            .unwrap_or(Value::Null)
    }
}

fn result_context(
    now: &str,
    account: &Value,
    result: &Value,
    inputs: &Value,
    operations: &[Value],
) -> Option<Value> {
    let asset = &result["account_assets"];
    let mut metadata = result_metadata(result, "");
    if number(&asset["total_asset"]).is_none()
        || number(&asset["available_cash"]).is_none()
        || !asset
            .get("market_value")
            .is_some_and(|value| value.is_null() || number(value).is_some())
        || !result["channel_type"].is_string()
        || !matches!(
            metadata["status"].as_str(),
            Some("SUCCEEDED" | "NOOP" | "BLOCKED" | "PARTIAL" | "FAILED")
        )
    {
        return None;
    }
    let empty = Map::new();
    let symbols = result["symbol_results"].as_object().unwrap_or(&empty);
    let mut projected_symbols = Vec::new();
    let mut orders = Vec::new();
    let mut trades = Vec::new();
    let mut targets = Map::new();
    for (symbol, result) in symbols {
        orders.extend(
            array(&result["orders"])
                .iter()
                .map(|order| projected_order(order, now)),
        );
        trades.extend(array(&result["trades"]).iter().map(projected_trade));
        projected_symbols.push(symbol_context(symbol, result));
        if !result["target_volume"].is_null() {
            targets.insert(symbol.clone(), result["target_volume"].clone());
        }
    }
    let target = Value::Object(targets);
    let (assets, positions) = asset_context(result, inputs, &target);
    complete_metadata(&mut metadata, &projected_symbols, symbols);
    metadata["execution_time"] = result.get("execution_time").cloned().unwrap_or(json!(0.0));
    let mut account_context = inputs["feishu_account"]
        .as_object()
        .cloned()
        .unwrap_or_default();
    account_context.insert("mark".into(), account["name"].clone());
    let succeeded = projected_symbols
        .iter()
        .filter(|symbol| successful(&symbol["status"]))
        .count();
    let defaults = legacy_variables(
        account, now, &assets, &positions, &orders, &trades, inputs, operations,
    );
    let mut context = json!({"account":null,"execution":null,
        "strategy":{"algorithm":null,"symbol_algorithms":null,"trade_rules":null,"forbidden_symbols":null,"risk_symbols":null},
        "assets":null,"targets":{"current":null,"previous":null,"target_volume":null},"positions":null,
        "summary":{"symbol_count":symbols.len(),"position_count":positions.as_array().map(Vec::len),"order_count":orders.len(),"filled_order_count":orders.iter().filter(|order| matches!(order["status"].as_str(),Some("已成交"|"已撤销"|"已拒绝"|"已过期"))).count(),"active_order_count":orders.iter().filter(|order| matches!(order["status"].as_str(),Some("待成交"|"已报"|"部分成交"))).count(),"trade_count":trades.len(),"trade_value":null,"succeeded_symbol_count":succeeded,"failed_symbol_count":symbols.len()-succeeded},
        "orders":null,"trades":null,"symbols":null,"default_feishu_variables":null,"is_test":false});
    // 直接移动 Value，避免 serde_json::to_value 拒绝 Python 的非有限数。
    context["account"] = Value::Object(account_context);
    context["execution"] = metadata;
    for key in ["algorithm", "symbol_algorithms", "trade_rules"] {
        context["strategy"][key] = redact(&inputs.get(key).cloned().unwrap_or(json!({})));
    }
    for key in ["forbidden_symbols", "risk_symbols"] {
        context["strategy"][key] = inputs.get(key).cloned().unwrap_or(json!([]));
    }
    context["targets"]["current"] = inputs.get("curr_target").cloned().unwrap_or(json!({}));
    context["targets"]["previous"] = inputs.get("last_target").cloned().unwrap_or(json!({}));
    context["targets"]["target_volume"] = target;
    context["assets"] = assets;
    context["positions"] = positions;
    context["summary"]["trade_value"] = number_value(
        trades
            .iter()
            .map(|trade| number(&trade["trade_value"]).unwrap_or(0.0))
            .sum::<f64>(),
    );
    context["orders"] = Value::Array(orders);
    context["trades"] = Value::Array(trades);
    context["symbols"] = Value::Array(projected_symbols);
    context["default_feishu_variables"] = defaults;
    Some(context)
}

pub(crate) fn execution_context(
    row: &Value,
    account: &Value,
    mut detail: Option<Value>,
    now: &str,
) -> Value {
    let captured = detail
        .as_mut()
        .and_then(|detail| detail.pointer_mut("/result/extra/server_notification_context"))
        .filter(|context| {
            context
                .as_object()
                .is_some_and(|context| !context.is_empty())
        });
    let mut context = captured
        .map(Value::take)
        .or_else(|| {
            detail.as_ref().and_then(|detail| {
                result_context(
                    now,
                    account,
                    &detail["result"],
                    detail["result"].get("inputs").unwrap_or(&detail["input"]),
                    &[],
                )
            })
        })
        .unwrap_or_else(|| empty_context(row, account));
    if !context["execution"].is_object() {
        context["execution"] = json!({});
    }
    let execution = context["execution"].as_object_mut().unwrap();
    for (key, value) in [
        ("id", row["execution_id"].clone()),
        ("kind", row["kind"].clone()),
        ("trigger_source", row["trigger_source"].clone()),
        ("notified_at", row["finished_at"].clone()),
        ("is_test", json!(false)),
        ("channel_type", account["trade_channel"].clone()),
    ] {
        execution.insert(key.into(), value);
    }
    for (key, fallback) in [
        ("status", row["status"].clone()),
        ("success", json!(row["status"] == "SUCCEEDED")),
        ("execution_time", json!(0.0)),
        ("outcome_reason", row["error"].clone()),
        ("outcome", Value::Null),
        ("reason_code", Value::Null),
    ] {
        if !execution.contains_key(key) {
            execution.insert(key.into(), fallback);
        }
    }
    if truthy(&row["error"]) {
        execution.insert("error".into(), row["error"].clone());
    } else if !execution.contains_key("error") {
        execution.insert("error".into(), Value::Null);
    }
    context["event_id"] = json!(format!(
        "execution:{}",
        row["execution_id"].as_str().unwrap_or("")
    ));
    context["event_type"] = json!("execution.finished");
    context
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn historical_preview_keeps_explicit_errors_and_order_times() {
        for update in [
            None,
            Some(Value::Null),
            Some(json!("2026-10-20T09:00:02.123456+08:00")),
        ] {
            let mut order = json!({"order_id":"o1","symbol":"A","direction":"BUY","volume":2,"price":1,"create_time":"original"});
            if let Some(update) = &update {
                order["update_time"] = update.clone();
            }
            let detail = json!({"input":null,"result":{"status":"PARTIAL","error":"RuntimeError: original", "inputs":null,
                "channel_type":"ctp","account_assets":{"total_asset":0,"available_cash":0,"market_value":null,"positions":[]},
                "symbol_results":{"A":{"status":"PARTIAL","error":"RuntimeError: symbol","orders":[order]}}}});
            let context = preview(&detail, "account", "2026-10-20T09:00:10", "original").unwrap();
            assert_eq!(context["execution"]["error"], "RuntimeError: original");
            assert_eq!(context["symbols"][0]["error"], "RuntimeError: symbol");
            assert_eq!(context["orders"][0].get("update_time"), update.as_ref());
            assert_eq!(context["orders"][0]["create_time"], "original");
            assert_eq!(detail["result"]["error"], "RuntimeError: original");
        }
    }

    #[test]
    fn historical_preview_distinguishes_missing_and_null_input() {
        let mut detail = json!({"input":{"curr_target":{"A":0.0}},"result":{"outcome":"completed","channel_type":"ctp",
            "account_assets":{"total_asset":0,"available_cash":0,"market_value":null,"positions":[]}}});
        assert_eq!(
            preview(&detail, "account", "now", "original").unwrap()["targets"]["current"],
            json!({"A":0.0})
        );
        detail["result"]["inputs"] = Value::Null;
        let context = preview(&detail, "account", "now", "original").unwrap();
        assert_eq!(context["targets"]["current"], json!({}));
        assert_eq!(context["assets"]["total_assets"], 0.0);
        assert_eq!(context["positions"], json!([]));
        assert!(preview(&json!({"result":null}), "account", "now", "original").is_err());
    }
    const NOW: &str = "2026-10-09T09:30:00+08:00";

    #[test]
    fn notification_keeps_capture_order_unknown_assets_and_the_intent_error() {
        let row = json!({"execution_id":"execution","account_id":1,"kind":"rebalance","trigger_source":"scheduler","status":"FAILED","finished_at":NOW,"error":"native\ncallback failed","algorithm":"market"});
        let account = json!({"id":1,"name":"current","trade_channel":"ctp"});
        let captured = json!({"positions":null,"orders":[],"trades":[],"assets":{"total_assets":0.0,"market_value":null},"execution":{"status":"PARTIAL","success":false,"execution_time":0.0,"error":"captured"}});
        let detail = json!({"result":{"extra":{"server_notification_context":captured}}});
        let before = detail.clone();
        let context = execution_context(&row, &account, Some(detail.clone()), NOW);
        assert_eq!(detail, before);
        assert_eq!(context["assets"], captured["assets"]);
        assert_eq!(context["positions"], Value::Null);
        assert_eq!(context["execution"]["status"], "PARTIAL");
        assert_eq!(context["execution"]["execution_time"], 0.0);
        assert_eq!(context["execution"]["error"], row["error"]);
        assert_eq!(
            context.as_object().unwrap().keys().next().unwrap(),
            "positions"
        );
        let early = execution_context(&row, &account, None, NOW);
        assert_eq!(early["assets"], json!({}));
        assert_eq!(early["positions"], Value::Null);
        assert_eq!(early["summary"]["position_count"], Value::Null);
    }

    #[test]
    fn rebuilt_notification_preserves_order_time_and_redacts_strategy_credentials() {
        let row = json!({"execution_id":"execution","kind":"rebalance","trigger_source":"scheduler","status":"FAILED","finished_at":NOW,"error":null});
        let account = json!({"id":1,"name":"test","trade_channel":"ctp"});
        let detail = json!({"input":{"curr_target":{"rb":0.0},"algorithm":{"method":"market","api_key":"secret","nested":{"token":"secret","safe":0}}},"result":{"channel_type":"ctp","status":"PARTIAL","error":"RuntimeError: callback\nfailed","execution_time":0.0,"account_assets":{"total_asset":0.0,"available_cash":0.0,"market_value":null,"positions":[],"source":"real"},"symbol_results":{"rb":{"status":"PARTIAL","target_volume":0,"orders":[{"order_id":"o","symbol":"rb","direction":"BUY","volume":1.0,"filled_volume":0.0,"update_time":null,"status":"已撤销"}],"trades":[{"trade_id":"t","symbol":"rb","order_id":"o","trade_volume":1.0,"trade_price":2.0,"trade_value":2.0,"trade_time":NOW}]}}}});
        let context = execution_context(&row, &account, Some(detail), NOW);
        assert_eq!(
            context["execution"]["error"],
            "RuntimeError: callback\nfailed"
        );
        assert_eq!(context["orders"][0]["update_time"], Value::Null);
        assert_eq!(context["assets"]["total_assets"], 0.0);
        assert_eq!(context["assets"]["market_value"], Value::Null);
        assert_eq!(context["targets"]["current"]["rb"], 0.0);
        assert_eq!(context["summary"]["trade_count"], 1);
        assert_eq!(
            context["strategy"]["algorithm"],
            json!({"method":"market","nested":{"safe":0}})
        );
    }

    #[test]
    fn rebuilding_preserves_symbol_order_without_mutating_the_facts() {
        let account = json!({"id":1,"name":"test","trade_channel":"ctp"});
        let mut detail = json!({"input":{},"result":{"channel_type":"ctp","status":"SUCCEEDED","account_assets":{"total_asset":1.0,"available_cash":1.0,"market_value":0.0},"symbol_results":{}}});
        for symbol in ["z", "a"] {
            detail["result"]["symbol_results"][symbol] = json!({
                "target_volume":1,
                "orders":[{"symbol":symbol,"order_id":symbol}],
                "trades":[{"symbol":symbol,"trade_id":symbol}]
            });
        }
        let before = detail.clone();
        let context =
            result_context(NOW, &account, &detail["result"], &detail["input"], &[]).unwrap();
        assert_eq!(detail, before);
        for collection in ["symbols", "orders", "trades"] {
            assert_eq!(
                array(&context[collection])
                    .iter()
                    .map(|value| &value["symbol"])
                    .collect::<Vec<_>>(),
                vec![&json!("z"), &json!("a")]
            );
        }
        assert_eq!(
            context["targets"]["target_volume"]
                .as_object()
                .unwrap()
                .keys()
                .map(String::as_str)
                .collect::<Vec<_>>(),
            vec!["z", "a"]
        );
        assert_eq!(context["positions"], json!([]));
    }
}
