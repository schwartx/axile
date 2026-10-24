//! 离线维护与回放报告；表、过滤条件、备份和事务都留在原生层。

use std::collections::BTreeMap;
use std::fs::OpenOptions;
use std::path::{Path, PathBuf};
use std::time::Duration;

use rusqlite::{params, Connection, OptionalExtension};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

use crate::error::AnalysisError;

fn invalid(message: impl Into<String>) -> AnalysisError {
    AnalysisError::new("MAINTENANCE_INVALID", message)
}

fn sql(error: rusqlite::Error) -> AnalysisError {
    AnalysisError::new("MAINTENANCE_SQL", error.to_string())
}

fn io(error: std::io::Error) -> AnalysisError {
    AnalysisError::new("MAINTENANCE_IO", error.to_string())
}

fn account_id(request: &Value) -> Result<i64, AnalysisError> {
    request
        .get("account_id")
        .and_then(Value::as_i64)
        .ok_or_else(|| invalid("缺少 account_id"))
}

fn text<'a>(value: &'a Value, key: &str) -> Result<&'a str, AnalysisError> {
    value
        .get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| invalid(format!("缺少字符串字段：{key}")))
}

fn number(value: &Value, key: &str) -> Result<f64, AnalysisError> {
    value
        .get(key)
        .and_then(Value::as_f64)
        .filter(|number| number.is_finite())
        .ok_or_else(|| invalid(format!("缺少有限数值字段：{key}")))
}

fn shift_time(value: &str, seconds: i64) -> Result<String, AnalysisError> {
    use chrono::{DateTime, Duration, NaiveDateTime, Timelike};
    fn display(value: NaiveDateTime) -> String {
        let mut result = value.format("%Y-%m-%dT%H:%M:%S").to_string();
        if value.nanosecond() != 0 {
            result.push_str(&format!(".{:06}", value.nanosecond() / 1000));
        }
        result
    }
    if let Ok(parsed) = DateTime::parse_from_rfc3339(value) {
        let shifted = parsed
            .checked_add_signed(Duration::seconds(seconds))
            .ok_or_else(|| invalid("日期超出范围"))?;
        return Ok(format!(
            "{}{}",
            display(shifted.naive_local()),
            shifted.format("%:z")
        ));
    }
    let normalized = value.replacen(' ', "T", 1);
    let parsed = NaiveDateTime::parse_from_str(&normalized, "%Y-%m-%dT%H:%M:%S%.f")
        .map_err(|error| invalid(error.to_string()))?;
    Ok(display(
        parsed
            .checked_add_signed(Duration::seconds(seconds))
            .ok_or_else(|| invalid("日期超出范围"))?,
    ))
}

fn dummy_assets(
    template: &Value,
    cash: f64,
    volumes: &Map<String, Value>,
    averages: &BTreeMap<String, f64>,
    prices: &BTreeMap<String, f64>,
) -> Value {
    let mut assets = template.clone();
    let positions: Vec<Value> = volumes.iter().map(|(symbol,quantity)| {
        let quantity = quantity.as_f64().expect("synthetic quantity");
        let price = prices[symbol];
        let average = averages[symbol];
        json!({"symbol":symbol,"volume":quantity,"available_volume":quantity,"market_value":quantity*price,
            "direction":"多头","avg_price":average,"extra":{"synthetic":true,"unrealized_pnl":quantity*(price-average)}})
    }).collect();
    let market_value: f64 = positions
        .iter()
        .map(|position| position["market_value"].as_f64().expect("position value"))
        .sum();
    assets["positions"] = json!(positions);
    assets["market_value"] = json!(market_value);
    assets["available_cash"] = json!(cash);
    assets["total_asset"] = json!(cash + market_value);
    assets
}

fn dummy_fill(
    key: i64,
    symbol: &str,
    delta: f64,
    mid: f64,
    start: &str,
    fee_asset: &str,
) -> Result<(Vec<Value>, Vec<Value>), AnalysisError> {
    if delta.abs() < 1e-10 {
        return Ok((Vec::new(), Vec::new()));
    }
    let digest = Sha256::digest(format!("{key}:{symbol}"));
    let seed = u32::from_be_bytes(digest[..4].try_into().expect("digest prefix"));
    let improvement = seed % 3 == 0;
    let bps = if improvement {
        -(0.2 + f64::from(seed % 70) / 100.0)
    } else {
        1.1 + f64::from(seed % 290) / 100.0
    };
    let rate = if improvement { 0.0002 } else { 0.0005 };
    let direction = if delta > 0.0 { "BUY" } else { "SELL" };
    let order_id = format!("dummy-{key}-{symbol}");
    let fractions: &[f64] = if seed % 2 == 1 { &[0.4, 0.6] } else { &[1.0] };
    let mut trades = Vec::new();
    for (index, fraction) in fractions.iter().enumerate() {
        let price = mid
            * (1.0 + if delta > 0.0 { 1.0 } else { -1.0 } * (bps + index as f64 * 0.12) / 10000.0);
        let quantity = delta.abs() * fraction;
        trades.push(json!({"trade_id":format!("{order_id}-{index}"),"order_id":order_id,"symbol":symbol,
            "trade_price":price,"trade_volume":quantity,"trade_value":quantity*price,
            "trade_time":shift_time(start,2+i64::from(seed%18)+index as i64)?,
            "extra":{"synthetic":true,"direction":direction,"commission":quantity*price*rate,"commission_asset":fee_asset}}));
    }
    let average = trades
        .iter()
        .map(|trade| trade["trade_value"].as_f64().expect("trade value"))
        .sum::<f64>()
        / delta.abs();
    let price = trades
        .iter()
        .map(|trade| trade["trade_price"].as_f64().expect("trade price"))
        .reduce(if delta > 0.0 { f64::max } else { f64::min })
        .expect("nonempty trades");
    let order = json!({"order_id":order_id,"symbol":symbol,"direction":direction,"order_type":"LIMIT",
        "volume":delta.abs(),"filled_volume":delta.abs(),"remaining_volume":0,"filled_ratio":1,"avg_price":average,
        "price":price,"status":"FILLED","create_time":shift_time(start,1)?,"update_time":trades.last().expect("trade")["trade_time"],
        "extra":{"synthetic":true,"client_order_id":order_id}});
    Ok((vec![order], trades))
}

fn rebuild_dummy(rows: &mut [DummyRow], fee_asset: &str) -> Result<(), AnalysisError> {
    let first = rows.first().ok_or_else(|| invalid("No records"))?;
    let initial_assets = &first.output["account_assets"];
    let initial = initial_assets
        .pointer("/extra/simulation_initial_cash")
        .and_then(Value::as_f64)
        .unwrap_or(number(initial_assets, "total_asset")?);
    let (mut cash, mut volumes, mut averages) = (initial, Map::new(), BTreeMap::new());
    for row in rows {
        if row.input.get("synthetic") != Some(&json!(true))
            || row
                .output
                .pointer("/account_assets/source")
                .and_then(Value::as_str)
                != Some("dummy")
        {
            return Err(invalid("Refusing non-synthetic records"));
        }
        let weights = row
            .input
            .get("curr_target")
            .and_then(Value::as_object)
            .ok_or_else(|| invalid("Missing curr_target"))?
            .clone();
        let values = weights
            .values()
            .map(|value| {
                value
                    .as_f64()
                    .filter(|weight| weight.is_finite() && *weight >= 0.0)
            })
            .collect::<Option<Vec<_>>>()
            .ok_or_else(|| invalid("This repair supports the long-only dummy fixture"))?;
        if values.iter().sum::<f64>() > 0.95 {
            return Err(invalid("This repair supports the long-only dummy fixture"));
        }
        row.input["algorithm"] = json!({"method":"DUMMY-SIMULATED","params":{}});
        let template = row.output["account_assets"].clone();
        let start = text(&template, "update_time")?;
        let symbols = row
            .output
            .get_mut("symbol_results")
            .and_then(Value::as_object_mut)
            .ok_or_else(|| invalid("Missing symbol_results"))?;
        let prices: BTreeMap<String, f64> = symbols
            .iter()
            .map(|(symbol, item)| {
                let tick = &item["first_tick"];
                let mid = (number(tick, "bid_price")? + number(tick, "ask_price")?) / 2.0;
                if mid <= 0.0 || !mid.is_finite() {
                    return Err(invalid("Invalid reference price"));
                }
                Ok((symbol.clone(), mid))
            })
            .collect::<Result<_, AnalysisError>>()?;
        if volumes.keys().any(|symbol| !prices.contains_key(symbol)) {
            return Err(invalid("Missing held-symbol price"));
        }
        let before = dummy_assets(&template, cash, &volumes, &averages, &prices);
        let equity = number(&before, "total_asset")?;
        for (symbol, item) in symbols {
            let price = prices[symbol];
            let weight = weights.get(symbol).and_then(Value::as_f64).unwrap_or(0.0);
            let raw_quantity = equity * weight / price;
            let volume = format!("{raw_quantity:.6}")
                .parse::<f64>()
                .map_err(|error| invalid(error.to_string()))?;
            let old = volumes.get(symbol).and_then(Value::as_f64).unwrap_or(0.0);
            let delta = volume - old;
            let (orders, trades) = dummy_fill(
                row.id,
                symbol,
                delta,
                price,
                &shift_time(start, if delta > 0.0 { 22 } else { 0 })?,
                fee_asset,
            )?;
            let value: f64 = trades
                .iter()
                .map(|trade| trade["trade_value"].as_f64().expect("trade value"))
                .sum();
            let fee: f64 = trades
                .iter()
                .map(|trade| trade["extra"]["commission"].as_f64().expect("trade fee"))
                .sum();
            cash -= value.copysign(delta) + fee;
            let previous =
                averages
                    .get(symbol)
                    .copied()
                    .unwrap_or(if delta > 0.0 { 0.0 } else { price });
            averages.insert(
                symbol.clone(),
                if delta > 0.0 {
                    (old * previous + value) / volume
                } else {
                    previous
                },
            );
            volumes.insert(symbol.clone(), json!(volume));
            let status = if trades.is_empty() {
                "NOOP"
            } else {
                "SUCCEEDED"
            };
            let object = item
                .as_object_mut()
                .ok_or_else(|| invalid("Invalid symbol result"))?;
            object.extend(json!({"orders":orders,"trades":trades,"target_volume":volume,"algorithm":"DUMMY-SIMULATED",
                "status":status,"success":true,"error":null,
                "sizing":{"symbol":symbol,"sizing_mode":"dummy","status":"AVAILABLE","reason_code":"COMMON.SIZING.QUANTIZED",
                    "strategy_weight":weight,"account_weight":weight,"account_multiplier":1,"weight_precision":null,
                    "equity":equity,"reference_price":price,"unit_multiplier":1,"unit_notional":price,"target_notional":equity*weight,
                    "raw_quantity":raw_quantity,"target_quantity":volume,"current_quantity":old,"quantity_step":0.000001,"min_quantity":0.000001,"min_notional":0}}).as_object().expect("object").clone());
        }
        let mut assets = dummy_assets(&template, cash, &volumes, &averages, &prices);
        if number(&assets, "total_asset")? <= 0.0 || cash < 0.0 {
            return Err(invalid("Invalid balance"));
        }
        assets["extra"] =
            json!({"synthetic":true,"simulation_initial_cash":initial,"simulation_version":2});
        row.output.as_object_mut().ok_or_else(||invalid("Invalid result"))?.extend(json!({"account_assets":assets,
            "dummy_before":before,"inputs":row.input,"error":null,"success":true,"execution_time":44,"memory":{"synthetic":true}}).as_object().expect("object").clone());
    }
    Ok(())
}

struct DummyRow {
    id: i64,
    input: Value,
    output: Value,
    execution_id: String,
    created_at: String,
}

fn dummy_position(assets: &Value, symbol: &str) -> f64 {
    assets["positions"]
        .as_array()
        .expect("generated positions")
        .iter()
        .filter(|position| position["symbol"] == symbol)
        .map(|position| position["volume"].as_f64().expect("generated quantity"))
        .sum()
}

fn dummy_reconciliation(output: &Value) -> Value {
    let before = &output["dummy_before"];
    let after = &output["account_assets"];
    let currency = after.get("currency").cloned().unwrap_or(Value::Null);
    let symbols: Vec<Value> = output["symbol_results"].as_object().expect("generated symbols").iter().map(|(symbol,item)| {
        let orders = item["orders"].as_array().expect("generated orders");
        let trades = item["trades"].as_array().expect("generated trades");
        let filled: f64 = orders.iter().map(|order|order["filled_volume"].as_f64().expect("filled quantity")*if order["direction"]=="SELL" {-1.0} else {1.0}).sum();
        let native_value: f64 = orders.iter().map(|order|order["filled_volume"].as_f64().expect("filled quantity")*order["avg_price"].as_f64().expect("average price")*if order["direction"]=="SELL" {-1.0} else {1.0}).sum();
        let average = (filled.abs()>1e-9).then(||native_value.abs()/filled.abs());
        let tick = &item["first_tick"];
        let (bid,ask) = (tick["bid_price"].as_f64(),tick["ask_price"].as_f64());
        let mid = bid.zip(ask).filter(|(bid,ask)|*bid>0.0&&*ask>0.0).map(|(bid,ask)|(bid+ask)/2.0).or_else(||tick["last_price"].as_f64());
        let slippage = average.zip(mid).filter(|(_,mid)|*mid>0.0).map(|(average,mid)|(average-mid)/mid*1e4*if filled<0.0 {1.0} else {-1.0});
        let liquidity = average.zip(bid.zip(ask)).filter(|(_, (bid,ask))|*bid>0.0&&*ask>0.0).map(|(average,(bid,ask))| {
            let tolerance = if ask>bid {(ask-bid)/2.0} else {ask*1e-4};
            if (filled<0.0&&average>=ask-tolerance)||(filled>=0.0&&average<=bid+tolerance) {"passive"}
            else if (filled<0.0&&average<=bid+tolerance)||(filled>=0.0&&average>=ask-tolerance) {"aggressive"} else {"mixed"}
        });
        let ordered: f64 = orders.iter().map(|order|order["volume"].as_f64().expect("order volume").abs()).sum();
        let quantity: f64 = orders.iter().map(|order|order["filled_volume"].as_f64().expect("filled quantity").abs()).sum();
        let fee: f64 = trades.iter().map(|trade|trade["extra"]["commission"].as_f64().expect("fee")).sum();
        let fee_asset = trades.first().map(|trade|trade["extra"]["commission_asset"].clone()).unwrap_or(Value::Null);
        let previous = dummy_position(before,symbol);
        let current = dummy_position(after,symbol);
        let target = item["target_volume"].as_f64().expect("target");
        let reached = if target.abs()<=1e-9 {current.abs()<=previous.abs().max(1.0)*0.01} else {(current-target).abs()<=target.abs()*0.01};
        let attained = if target.abs()<=1e-9 {reached.then_some(1.0)} else {Some(current/target)};
        let tree: Vec<Value> = orders.iter().map(|order| {
            let order_trades: Vec<Value> = trades.iter().filter(|trade|trade["order_id"]==order["order_id"]).map(|trade|json!({
                "price":trade["trade_price"],"volume":trade["trade_volume"],"value":trade["trade_value"],"native_value":trade["trade_value"],
                "price_currency":null,"value_currency":null,"time":trade["trade_time"],"fee":trade["extra"]["commission"],"fee_asset":trade["extra"]["commission_asset"]})).collect();
            json!({"order_id":order["order_id"],"side":if order["direction"]=="SELL" {"sell"} else {"buy"},"order_type":"LIMIT",
                "price":order["price"],"avg_price":order["avg_price"],"value_conversion":null,"volume":order["volume"],"filled_volume":order["filled_volume"],
                "status":"FILLED","client_order_id":order["extra"]["client_order_id"],"trades":order_trades})
        }).collect();
        json!({"symbol":symbol,"status":item["status"],"error":item["error"],"outcome":item.get("outcome"),
            "outcome_reason":item.get("outcome_reason"),"final_volume":item.get("final_volume"),"target":target,
            "filled":filled,"filled_value":native_value,"value_currency":currency,"filled_native_value":native_value,"native_currency":currency,
            "avg_price":average,"before":previous,"after":current,"moved":current-previous,"drift":current-previous-filled,
            "attained_ratio":attained,"reached":reached,"sizing":item["sizing"],"orders":tree,
            "tca":{"n_orders":orders.len(),"n_trades":trades.len(),"arrival_bid":bid,"arrival_ask":ask,"arrival_mid":mid,
                "slippage_bps":slippage,"liquidity":liquidity,"fill_ratio":if ordered>1e-9 {Some(quantity/ordered)} else {None},"fee":fee,"fee_asset":fee_asset}})
    }).collect();
    json!({"account":{"equity_before":before["total_asset"],"equity_after":after["total_asset"],"source_before":"dummy","source_after":"dummy"},"symbols":symbols})
}

fn dummy_evidence(row: &DummyRow) -> Value {
    let symbols = row.output["symbol_results"]
        .as_object()
        .expect("generated symbols");
    let summary = if symbols.is_empty() {
        json!({})
    } else {
        json!({"symbols_total":symbols.len(),"symbols_succeeded":symbols.len(),"symbols_failed":0,
            "symbols_noop":symbols.values().filter(|item|item["status"]=="NOOP").count()})
    };
    json!({"STANDARD_INPUT":{"input":row.input,"synthetic":true},
        "TARGET_SNAPSHOT":{"curr_target":row.input["curr_target"],"last_target":row.input.get("last_target").cloned().unwrap_or_else(||json!({}))},
        "ACCOUNT_SNAPSHOT_BEFORE":{"account_assets":row.output["dummy_before"],"source":"dummy"},
        "ACCOUNT_SNAPSHOT":{"account_assets":row.output["account_assets"],"source":"dummy"},
        "EXECUTION_SUMMARY":{"summary":summary,"success":true,"execution_time":44,"synthetic":true,"reconciliation":dummy_reconciliation(&row.output)}})
}

fn write_dummy_events(
    connection: &Connection,
    account: i64,
    row: &DummyRow,
    channel: &str,
) -> Result<(), AnalysisError> {
    let mut events = vec![
        (row.created_at.clone(), "EXECUTION_STARTED", None, json!({})),
        (row.created_at.clone(), "INPUT_SNAPSHOTTED", None, json!({})),
        (row.created_at.clone(), "TARGET_COMPUTED", None, json!({})),
    ];
    for (symbol, item) in row.output["symbol_results"]
        .as_object()
        .expect("generated symbols")
    {
        let orders = item["orders"].as_array().expect("generated orders");
        events.push((row.created_at.clone(),"SYMBOL_DECISION_MADE",Some(symbol.as_str()),json!({"decision":{"algorithm":"DUMMY-SIMULATED","status":item["status"],"orders_count":orders.len()}})));
        for order in orders {
            events.push((text(order,"create_time")?.into(),"ORDER_SUBMITTED",Some(symbol.as_str()),json!({"order":{"direction":order["direction"],"status":"NEW","order_id":order["order_id"]}})));
            let mut terminal = order.clone();
            terminal["terminal_status"] = json!("FILLED");
            events.push((
                text(order, "update_time")?.into(),
                "ORDER_TERMINAL",
                Some(symbol.as_str()),
                json!({"order":terminal,"exchange":{"client_order_id":order["order_id"]}}),
            ));
        }
    }
    events.push((
        shift_time(&row.created_at, 44)?,
        "EXECUTION_COMPLETED",
        None,
        json!({}),
    ));
    events.sort_by(|left, right| left.0.cmp(&right.0));
    connection
        .execute(
            "DELETE FROM execution_event WHERE execution_id=?1",
            [&row.execution_id],
        )
        .map_err(sql)?;
    let mut statement = connection.prepare("INSERT INTO execution_event(execution_id,account_id,channel,algorithm,schema_version,event_uid,event_type,status,reason_family,reason_code,symbol,ts_local_created,ts_persisted,seq,details,order_id)
        VALUES(?1,?2,?3,'DUMMY-SIMULATED',1,?4,?5,?6,'SYSTEM','DUMMY.SYNTHETIC',?7,?8,?8,?9,?10,?11)").map_err(sql)?;
    for (sequence, (time, kind, symbol, mut details)) in events.into_iter().enumerate() {
        details["synthetic"] = json!(true);
        let order_id = details.pointer("/order/order_id").and_then(Value::as_str);
        statement
            .execute(params![
                row.execution_id,
                account,
                channel,
                format!("{}:sim:{sequence}", row.execution_id),
                kind,
                if matches!(kind, "ORDER_TERMINAL" | "EXECUTION_COMPLETED") {
                    "SUCCESS"
                } else {
                    "INFO"
                },
                symbol,
                time,
                sequence as i64,
                details.to_string(),
                order_id
            ])
            .map_err(sql)?;
    }
    Ok(())
}

/// 仅修复已停止的命名 synthetic 夹具，备份后原子重建所有旧格式证据副本。
pub fn dummy_repair(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let account = account_id(request)?;
    let expected_name = text(request, "expected_name")?;
    let channel = text(request, "channel")?;
    let fee_asset = text(request, "fee_asset")?;
    let database = Path::new(path);
    {
        let reader = crate::storage_connection::open_reader(database)?;
        let legacy: bool = reader.query_row("SELECT COUNT(*)=2 FROM pragma_table_info('executerecord') WHERE name IN ('raw_input','raw_result')",[],|row|row.get(0)).map_err(sql)?;
        if !legacy {
            return Err(invalid("This repair supports only legacy JSON execution records; typed 0037+ databases are unsupported"));
        }
    }
    let connection = crate::storage_connection::open_writer(database, false)?;
    crate::writer_scope::immediate(&connection, || {
        let valid: bool = connection
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM account WHERE id=?1 AND name=?2 AND NOT is_started)",
                params![account, expected_name],
                |row| row.get(0),
            )
            .map_err(sql)?;
        if !valid {
            return Err(invalid(format!("Expected stopped {expected_name} account")));
        }
        let mut statement = connection.prepare("SELECT id,raw_input,raw_result,execution_id,created_at FROM executerecord WHERE account_id=?1 ORDER BY created_at,id").map_err(sql)?;
        let mut rows = statement
            .query_map([account], |row| {
                Ok((
                    row.get::<_, i64>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, String>(3)?,
                    row.get::<_, String>(4)?,
                ))
            })
            .map_err(sql)?
            .map(|row| {
                let (id, input, output, execution_id, created_at) = row.map_err(sql)?;
                Ok(DummyRow {
                    id,
                    input: serde_json::from_str(&input)
                        .map_err(|error| invalid(error.to_string()))?,
                    output: serde_json::from_str(&output)
                        .map_err(|error| invalid(error.to_string()))?,
                    execution_id,
                    created_at,
                })
            })
            .collect::<Result<Vec<_>, AnalysisError>>()?;
        rebuild_dummy(&mut rows, fee_asset)?;
        let target = backup_path(database, request, "before-dummy-repair")?;
        backup(database, &target)?;
        for row in &rows {
            connection
                .execute(
                    "UPDATE executerecord SET raw_input=?1,raw_result=?2 WHERE id=?3",
                    params![row.input.to_string(), row.output.to_string(), row.id],
                )
                .map_err(sql)?;
            connection.execute("UPDATE account_asset_snapshot SET assets=?1 WHERE account_id=?2 AND execution_id=?3",params![row.output["account_assets"].to_string(),account,row.execution_id]).map_err(sql)?;
            connection
                .execute(
                    "DELETE FROM execution_artifact WHERE execution_id=?1",
                    [&row.execution_id],
                )
                .map_err(sql)?;
            for (kind, content) in dummy_evidence(row).as_object().expect("generated evidence") {
                connection.execute("INSERT INTO execution_artifact(execution_id,artifact_type,schema_version,content,created_at) VALUES(?1,?2,2,?3,?4)",params![row.execution_id,kind,content.to_string(),row.created_at]).map_err(sql)?;
            }
            write_dummy_events(&connection, account, row, channel)?;
        }
        Ok(json!([
            target,
            rows.len(),
            rows.last().expect("nonempty repaired records").output["account_assets"]["total_asset"]
        ]))
    })
}

fn backup_path(database: &Path, request: &Value, purpose: &str) -> Result<PathBuf, AnalysisError> {
    let stamp = request
        .get("backup_suffix")
        .and_then(Value::as_str)
        .filter(|stamp| !stamp.is_empty() && stamp.bytes().all(|c| c.is_ascii_alphanumeric()))
        .ok_or_else(|| invalid("缺少有效备份时间"))?;
    let filename = database
        .file_name()
        .and_then(|name| name.to_str())
        .ok_or_else(|| invalid("无效数据库路径"))?;
    for index in 1..10_000 {
        let candidate =
            database.with_file_name(format!("{filename}.{purpose}-{stamp}-{index}.bak"));
        match OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&candidate)
        {
            Ok(_) => return Ok(candidate),
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(error) => return Err(io(error)),
        }
    }
    Err(invalid("无法生成不覆盖已有文件的备份路径"))
}

fn backup(database: &Path, target: &Path) -> Result<(), AnalysisError> {
    let result = (|| {
        let source = crate::storage_connection::open_reader(database)?;
        let mut destination = Connection::open(target).map_err(sql)?;
        let backup = rusqlite::backup::Backup::new(&source, &mut destination).map_err(sql)?;
        backup
            .run_to_completion(128, Duration::from_millis(10), None)
            .map_err(sql)
    })();
    if result.is_err() {
        let _ = std::fs::remove_file(target);
    }
    result
}

fn integrity(connection: &Connection) -> Result<String, AnalysisError> {
    connection
        .query_row("PRAGMA integrity_check", [], |row| row.get(0))
        .map_err(sql)
}

fn derived_executions(performance: &Value, all: &[Value]) -> Option<Vec<Value>> {
    let start = crate::snapshots::timestamp(performance.get("baseline")?.as_str()?)?;
    let end = crate::snapshots::timestamp(performance.get("end")?.as_str()?)?;
    let mut derived = Vec::new();
    for execution in all {
        let Some(created) = execution
            .pointer("/record/created_at")
            .and_then(Value::as_str)
        else {
            continue;
        };
        let when = crate::snapshots::timestamp(created)?;
        if start <= when && when <= end {
            derived.push(execution.clone());
        }
    }
    Some(derived)
}

fn dedupe_changes(
    connection: &Connection,
) -> Result<(Vec<(String, String)>, Value), AnalysisError> {
    let mut statement = connection
        .prepare("SELECT id,ranges FROM performance_snapshot")
        .map_err(sql)?;
    let rows = statement
        .query_map([], |row| {
            let stored = match row.get_ref(1)? {
                rusqlite::types::ValueRef::Text(raw) => {
                    std::str::from_utf8(raw).ok().map(str::to_owned)
                }
                _ => None,
            };
            Ok((row.get::<_, String>(0)?, stored))
        })
        .map_err(sql)?;
    let mut updates = Vec::new();
    let mut counts = json!({"30":0,"90":0});
    for row in rows {
        let (id, Some(stored)) = row.map_err(sql)? else {
            continue;
        };
        let Ok(mut ranges) = serde_json::from_str::<Value>(&stored) else {
            continue;
        };
        let Some(all) = ranges
            .pointer("/all/performance/executions")
            .and_then(Value::as_array)
            .cloned()
        else {
            continue;
        };
        let mut changed = false;
        for key in ["30", "90"] {
            let Some(performance) = ranges
                .get_mut(key)
                .and_then(|range| range.get_mut("performance"))
            else {
                continue;
            };
            if let Some(derived) = derived_executions(performance, &all) {
                if performance.get("executions").is_some_and(|existing| {
                    crate::business_facts_ops::encode::py_eq(existing, &json!(derived))
                }) {
                    performance
                        .as_object_mut()
                        .expect("performance object")
                        .remove("executions");
                    counts[key] = json!(counts[key].as_u64().unwrap_or(0) + 1);
                    changed = true;
                }
            }
        }
        if changed {
            updates.push((
                serde_json::to_string(&ranges).map_err(|error| invalid(error.to_string()))?,
                id,
            ));
        }
    }
    Ok((updates, counts))
}

/// 只删除可无损重建的范围执行列表；应用前持有写锁并备份。
pub fn performance_dedupe(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let database = Path::new(path);
    let apply = request
        .get("apply")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    if apply && request.get("writers_stopped").and_then(Value::as_bool) != Some(true) {
        return Err(invalid(
            "--apply 前必须停止所有写库服务，并显式传入 --writers-stopped",
        ));
    }
    let before = std::fs::metadata(database).map_err(io)?.len();
    let connection = if apply {
        crate::writer_scope::open_writer(database)?
    } else {
        crate::storage_connection::open_reader(database)?
    };
    connection
        .execute_batch(if apply { "BEGIN EXCLUSIVE" } else { "BEGIN" })
        .map_err(sql)?;
    let result = (|| {
        let integrity_before = integrity(&connection)?;
        if integrity_before != "ok" {
            return Err(invalid(format!(
                "拒绝处理完整性异常的数据库：{integrity_before}"
            )));
        }
        let (updates, counts) = dedupe_changes(&connection)?;
        let mut report = json!({"database":path,"mode":if apply {"apply"} else {"dry-run"},
            "snapshot_updates":updates.len(),"ranges":counts,"integrity_check_before":integrity_before,
            "bytes_before":before,"bytes_after":before});
        if apply {
            let target = backup_path(database, request, "before-performance-dedupe")?;
            backup(database, &target)?;
            let mut statement = connection
                .prepare("UPDATE performance_snapshot SET ranges=?1 WHERE id=?2")
                .map_err(sql)?;
            for (ranges, id) in updates {
                statement.execute(params![ranges, id]).map_err(sql)?;
            }
            report["backup"] = json!(target);
        }
        Ok(report)
    })();
    let mut report = match result {
        Ok(report) => {
            if let Err(error) = connection.execute_batch("COMMIT") {
                let _ = connection.execute_batch("ROLLBACK");
                return Err(sql(error));
            }
            report
        }
        Err(error) => {
            let _ = connection.execute_batch("ROLLBACK");
            return Err(error);
        }
    };
    if apply {
        let after_integrity = integrity(&connection)?;
        if after_integrity != "ok" {
            return Err(invalid(format!("更新后完整性检查失败：{after_integrity}")));
        }
        connection.execute_batch("VACUUM").map_err(sql)?;
        connection
            .execute_batch("PRAGMA wal_checkpoint(TRUNCATE)")
            .map_err(sql)?;
        let after = std::fs::metadata(database).map_err(io)?.len();
        report["integrity_check_after"] = json!(after_integrity);
        report["bytes_after"] = json!(after);
        report["bytes_reclaimed"] = json!(i128::from(before) - i128::from(after));
    }
    Ok(report)
}

/// 一次读取回放提交基线或该基线之后的意图、调度跳过结果。
pub fn replay_observe(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let account = account_id(request)?;
    crate::with_read_snapshot(Path::new(path), |connection| {
        let intent: Option<(i64, String)> = connection.query_row(
            "SELECT id,status FROM execution_intent WHERE account_id=?1 ORDER BY id DESC LIMIT 1",
            [account], |row| Ok((row.get(0)?,row.get(1)?))).optional().map_err(sql)?;
        let skip: Option<(i64, String)> = connection.query_row(
            "SELECT id,reason_code FROM schedule_skip WHERE account_id=?1 ORDER BY id DESC LIMIT 1",
            [account], |row| Ok((row.get(0)?,row.get(1)?))).optional().map_err(sql)?;
        let baseline_intent = request
            .get("baseline_intent_id")
            .and_then(Value::as_i64)
            .unwrap_or(i64::MAX);
        let baseline_skip = request
            .get("baseline_skip_id")
            .and_then(Value::as_i64)
            .unwrap_or(i64::MAX);
        let outcome = match &intent {
            Some((id, status)) if *id > baseline_intent => {
                (!matches!(status.as_str(), "QUEUED" | "RUNNING" | "TERMINATING"))
                    .then(|| format!("intent:{status}"))
            }
            _ => skip
                .as_ref()
                .filter(|(id, _)| *id > baseline_skip)
                .map(|(_, reason)| format!("skip:{reason}")),
        };
        Ok(
            json!({"latest_intent_id":intent.as_ref().map_or(0,|(id,_)|*id),
            "latest_skip_id":skip.as_ref().map_or(0,|(id,_)|*id),"outcome":outcome}),
        )
    })
}

fn execution_report(connection: &Connection, account: Option<i64>) -> Result<Value, AnalysisError> {
    let span = connection.query_row(
        "SELECT MIN(created_at),MAX(created_at) FROM executerecord WHERE (?1 IS NULL OR account_id=?1)",
        [account], |row| Ok(json!([row.get::<_,Option<String>>(0)?,row.get::<_,Option<String>>(1)?]))).map_err(sql)?;
    let mut query = connection
        .prepare(
            "SELECT DISTINCT t.map_key FROM executerecord r
         JOIN execution_input i ON i.id=r.input_id AND i.account_id=r.account_id
         JOIN execution_input_target t ON t.input_id=i.id
         WHERE t.role='curr_target' AND t.map_key<>'' AND typeof(t.value) IN ('integer','real')
         AND t.value BETWEEN -1.7976931348623157e308 AND 1.7976931348623157e308
         AND (?1 IS NULL OR r.account_id=?1) ORDER BY t.map_key LIMIT 20",
        )
        .map_err(sql)?;
    let symbols = query
        .query_map([account], |row| row.get::<_, String>(0))
        .map_err(sql)?
        .collect::<rusqlite::Result<Vec<_>>>()
        .map_err(sql)?;
    let mut query = connection.prepare("SELECT status,COUNT(*) FROM executerecord WHERE (?1 IS NULL OR account_id=?1) GROUP BY status").map_err(sql)?;
    let mut statuses = BTreeMap::<String, i64>::new();
    let mut rows = query.query([account]).map_err(sql)?;
    while let Some(row) = rows.next().map_err(sql)? {
        let status = match row.get_ref(0).map_err(sql)? {
            rusqlite::types::ValueRef::Text(raw) if !raw.is_empty() => {
                String::from_utf8_lossy(raw).into_owned()
            }
            _ => "?".into(),
        };
        *statuses.entry(status).or_default() += row.get::<_, i64>(1).map_err(sql)?;
    }
    Ok(json!({"executerecord_span":span,"target_symbols":symbols,"execution_status":statuses}))
}

/// 报告只读取标量事实；未知值原样保留，历史文档不作为回退。
pub fn replay_report(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    if request
        .get("account_id")
        .is_some_and(|value| !value.is_null() && value.as_i64().is_none())
    {
        return Err(invalid("account_id 必须为整数或 null"));
    }
    let account = request.get("account_id").and_then(Value::as_i64);
    crate::with_read_snapshot(Path::new(path), |connection| {
        let mut statement = connection
            .prepare("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
            .map_err(sql)?;
        let tables = statement
            .query_map([], |row| row.get::<_, String>(0))
            .map_err(sql)?
            .collect::<rusqlite::Result<Vec<_>>>()
            .map_err(sql)?;
        let mut counts = Map::new();
        for table in tables {
            let escaped = table.replace('"', "\"\"");
            let count = connection
                .query_row(&format!("SELECT COUNT(*) FROM \"{escaped}\""), [], |row| {
                    row.get::<_, i64>(0)
                })
                .map_err(sql)?;
            counts.insert(table, json!(count));
        }
        let mut report = Map::new();
        if counts
            .get("executerecord")
            .and_then(Value::as_i64)
            .unwrap_or(0)
            > 0
        {
            report.extend(
                execution_report(connection, account)?
                    .as_object()
                    .expect("report object")
                    .clone(),
            );
        }
        if counts
            .get("execution_event")
            .and_then(Value::as_i64)
            .unwrap_or(0)
            > 0
        {
            let mut statement = connection.prepare("SELECT event_type,COUNT(*) FROM execution_event WHERE (?1 IS NULL OR account_id=?1) GROUP BY event_type ORDER BY 2 DESC").map_err(sql)?;
            let pairs = statement
                .query_map([account], |row| {
                    Ok((row.get::<_, String>(0)?, json!(row.get::<_, i64>(1)?)))
                })
                .map_err(sql)?;
            report.insert(
                "event_types".into(),
                Value::Object(
                    pairs
                        .collect::<rusqlite::Result<Map<_, _>>>()
                        .map_err(sql)?,
                ),
            );
        }
        if counts
            .get("account_asset_snapshot")
            .and_then(Value::as_i64)
            .unwrap_or(0)
            > 0
        {
            let span = connection.query_row("SELECT MIN(created_at),MAX(created_at) FROM account_asset_snapshot WHERE (?1 IS NULL OR account_id=?1)",[account], |row|Ok(json!([row.get::<_,Option<String>>(0)?,row.get::<_,Option<String>>(1)?]))).map_err(sql)?;
            report.insert("asset_snapshot_span".into(), span);
        }
        report.insert("tables".into(), Value::Object(counts));
        Ok(Value::Object(report))
    })
}
