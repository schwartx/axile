use super::*;
const DAILY_FIELDS: &[(&str, &str)] = &[
    ("value", "value"),
    ("cost", "cost"),
    ("loss_bp", "lossBp"),
    ("coverage", "coverage"),
    ("covered", "covered"),
    ("count", "count"),
    ("amount_complete", "amountComplete"),
    ("fee_covered", "feeCovered"),
    ("covered_value", "coveredValue"),
    ("estimated", "estimated"),
];
const TRANSACTION_FIELDS: &[(&str, &str)] = &[
    ("symbol", "symbol"),
    ("side", "side"),
    ("quantity", "quantity"),
    ("price", "price"),
    ("reference", "reference"),
    ("reference_source", "referenceSource"),
    ("time", "time"),
    ("end_time", "endTime"),
    ("time_estimated", "timeEstimated"),
    ("before", "before"),
    ("target", "target"),
];
use crate::legacy_execution::{RAW_FIELDS, SYMBOL_FIELDS};

fn children(
    connection: &Connection,
    table: &str,
    id: &str,
    record_id: i64,
    order: &str,
) -> Result<Vec<Object>> {
    rows(
        connection,
        &format!("SELECT * FROM {table} WHERE snapshot_id=? AND record_id=? ORDER BY {order}"),
        rusqlite::params![id, record_id],
    )
}
fn selected(row: &Object, names: &[(&str, &str)]) -> Object {
    names
        .iter()
        .map(|(column, public)| ((*public).into(), value(row, column).clone()))
        .collect()
}
fn simple(row: &Object, names: &[&str]) -> Value {
    Value::Object(
        names
            .iter()
            .map(|name| ((*name).into(), value(row, name).clone()))
            .collect(),
    )
}
fn truth(value: &Value) -> bool {
    value
        .as_bool()
        .unwrap_or_else(|| value.as_i64().is_some_and(|number| number != 0))
}
fn summary(connection: &Connection, id: &str, record_id: i64) -> Result<Value> {
    let (mut count, mut valued, mut covered, mut fee_covered) = (0_i64, 0_i64, 0_i64, 0_i64);
    let (mut amount, mut cost, mut covered_amount, mut weighted) =
        (0.0_f64, 0.0_f64, 0.0_f64, 0.0_f64);
    let mut fees = Object::new();
    for row in children(connection, "performance_cost_trade", id, record_id, "id")? {
        count += 1;
        if let Some(trade_value) = stored_float(&row, "value")? {
            valued += 1;
            amount += trade_value;
            if let Some(fee) = stored_float(&row, "cost")? {
                covered += 1;
                covered_amount += trade_value;
                cost += fee;
                weighted += trade_value * value_or_error(&row, "loss_bp")?;
            }
        }
        if let (Some(fee), Some(currency)) = (
            stored_float(&row, "fee")?,
            value(&row, "fee_currency")
                .as_str()
                .filter(|currency| !currency.is_empty()),
        ) {
            let total = fees.get(currency).and_then(Value::as_f64).unwrap_or(0.0) + fee;
            if !total.is_finite() {
                return Err(error("Out of range float values are not JSON compliant"));
            }
            fees.insert(currency.into(), Value::from(total));
            fee_covered += 1;
        }
    }
    if (valued > 0 && !amount.is_finite())
        || (covered > 0 && !cost.is_finite())
        || (covered_amount > 0.0 && !(weighted / covered_amount).is_finite())
        || (valued == count && amount > 0.0 && !(covered_amount / amount).is_finite())
    {
        return Err(error("Out of range float values are not JSON compliant"));
    }
    Ok(
        serde_json::json!({"value":if valued>0{Some(amount)}else{None},"cost":if covered>0{Some(cost)}else{None},"lossBp":if covered_amount>0.0{Some(weighted/covered_amount)}else{None},"coverage":if valued==count&&amount>0.0{Some(covered_amount/amount)}else{None},"covered":covered,"count":count,"amountComplete":valued==count,"fees":fees,"feeCovered":fee_covered}),
    )
}
fn value_or_error(row: &Object, name: &str) -> Result<f64> {
    stored_float(row, name)?.ok_or_else(|| error(format!("Missing historical number: {name}")))
}
fn stored_float(row: &Object, name: &str) -> Result<Option<f64>> {
    match value(row, name) {
        Value::Null => Ok(None),
        Value::Number(number) => number
            .as_f64()
            .or_else(|| number.to_string().parse::<f64>().ok())
            .map(Some)
            .ok_or_else(|| error(format!("Invalid historical number: {name}"))),
        _ => Err(error(format!("Invalid historical number: {name}"))),
    }
}
fn strict_json(value: &Value) -> Result<String> {
    let mut pending = vec![value];
    while let Some(value) = pending.pop() {
        match value {
            Value::Number(number) => {
                let spelling = number.to_string();
                let integer = spelling
                    .bytes()
                    .all(|byte| byte.is_ascii_digit() || byte == b'-');
                if !integer && number.as_f64().is_none() {
                    return Err(error("Out of range float values are not JSON compliant"));
                }
            }
            Value::Array(items) => pending.extend(items),
            Value::Object(items) => pending.extend(items.values()),
            _ => {}
        }
    }
    serde_json::to_string(&crate::business_facts_ops::encode::audit_value(value))
        .map_err(|failure| error(failure.to_string()))
}
fn execution_payload(connection: &Connection, row: &Object) -> Result<Value> {
    let id = text(row, "snapshot_id")?;
    let record_id = integer(row, "record_id")?;
    let mask = integer(row, "raw_present")?;
    let mut raw = RAW_FIELDS
        .iter()
        .enumerate()
        .filter(|(index, _)| mask & (1 << index) != 0)
        .map(|(_, name)| ((*name).into(), value(row, &format!("raw_{name}")).clone()))
        .collect::<Object>();
    if truth(value(row, "has_symbol_results")) {
        let mut symbols = Object::new();
        for symbol in children(
            connection,
            "performance_cost_execution_symbol",
            id,
            record_id,
            "symbol",
        )? {
            let mask = integer(&symbol, "present")?;
            symbols.insert(
                text(&symbol, "symbol")?.into(),
                Value::Object(
                    SYMBOL_FIELDS
                        .iter()
                        .enumerate()
                        .filter(|(index, _)| mask & (1 << index) != 0)
                        .map(|(_, name)| ((*name).into(), value(&symbol, name).clone()))
                        .collect(),
                ),
            );
        }
        raw.insert("symbol_results".into(), Value::Object(symbols));
    }
    let fee_rows = children(
        connection,
        "performance_cost_transaction_fee",
        id,
        record_id,
        "transaction_idx,currency",
    )?;
    let mut transactions = Vec::new();
    for transaction in children(
        connection,
        "performance_cost_transaction",
        id,
        record_id,
        "idx",
    )? {
        let mut summary = selected(&transaction, &DAILY_FIELDS[..8]);
        summary.insert(
            "amountComplete".into(),
            Value::Bool(truth(value(&summary, "amountComplete"))),
        );
        let fees = fee_rows
            .iter()
            .filter(|fee| value(fee, "transaction_idx") == value(&transaction, "idx"))
            .map(|fee| {
                Ok((
                    text(fee, "currency")?.to_owned(),
                    value(fee, "value").clone(),
                ))
            })
            .collect::<Result<Object>>()?;
        summary.insert("fees".into(), Value::Object(fees));
        let mut item = selected(&transaction, TRANSACTION_FIELDS);
        item.insert(
            "timeEstimated".into(),
            Value::Bool(truth(value(&item, "timeEstimated"))),
        );
        item.insert("summary".into(), Value::Object(summary));
        transactions.push(Value::Object(item));
    }
    let positions = children(
        connection,
        "performance_cost_position",
        id,
        record_id,
        "idx",
    )?
    .iter()
    .map(|row| simple(row, &["symbol", "quantity", "direction"]))
    .collect::<Vec<_>>();
    let attempts = children(connection, "performance_cost_attempt", id, record_id, "idx")?
        .iter()
        .map(|row| simple(row, &["symbol", "planned", "filled", "reason"]))
        .collect::<Vec<_>>();
    Ok(
        serde_json::json!({"key":value(row,"key"),"record":{"id":record_id,"execution_id":value(row,"execution_id"),"created_at":value(row,"created_at"),"is_success":value(row,"is_success"),"raw_result":raw},"noop":truth(value(row,"noop")),"tradeCount":value(row,"trade_count"),"durationSec":value(row,"duration_sec"),"symbolCount":value(row,"symbol_count"),"positionCount":value(row,"position_count"),"positions":if truth(value(row,"positions_present")){Value::Array(positions)}else{Value::Null},"transactions":transactions,"reason":value(row,"reason"),"attempts":attempts}),
    )
}
pub(super) fn cost_payloads(connection: &Connection) -> Result<()> {
    let mut after = None;
    loop {
        let ids = snapshot_ids(connection, after.as_deref())?;
        if ids.is_empty() {
            break;
        }
        after = ids.last().cloned();
        for id in ids {
            let versions=one(connection,"SELECT cost_schema_version,result_schema_version FROM performance_snapshot WHERE id=?",[&id])?;
            if integer(&versions, "cost_schema_version")? < 2
                && integer(&versions, "result_schema_version")? < 2
            {
                continue;
            }
            let mut cursor = None;
            loop {
                let batch=rows(connection,"SELECT * FROM performance_cost_execution WHERE snapshot_id=?1 AND (?2 IS NULL OR record_id>?2) ORDER BY record_id LIMIT 100",rusqlite::params![id,cursor])?;
                if batch.is_empty() {
                    break;
                }
                cursor = Some(integer(batch.last().unwrap(), "record_id")?);
                for row in batch {
                    let mut payload = json_value(value(&row, "payload"))?;
                    if !payload.as_object().is_some_and(|map| !map.is_empty()) {
                        payload = execution_payload(connection, &row)?;
                    }
                    let map = payload
                        .as_object_mut()
                        .ok_or_else(|| error("Legacy cost execution payload must be an object"))?;
                    if !map.contains_key("summary") {
                        map.insert(
                            "summary".into(),
                            summary(connection, &id, integer(&row, "record_id")?)?,
                        );
                    }
                    connection.execute("UPDATE performance_cost_execution SET payload=? WHERE snapshot_id=? AND record_id=?",rusqlite::params![strict_json(&payload)?,id,integer(&row,"record_id")?]).map_err(sql)?;
                }
            }
            let mut cursor = None;
            loop {
                let batch=rows(connection,"SELECT * FROM performance_cost_trade WHERE snapshot_id=?1 AND (?2 IS NULL OR id>?2) ORDER BY id LIMIT 100",rusqlite::params![id,cursor])?;
                if batch.is_empty() {
                    break;
                }
                cursor = Some(integer(batch.last().unwrap(), "id")?);
                for row in batch {
                    if json_value(value(&row, "payload"))?
                        .as_object()
                        .is_some_and(|map| !map.is_empty())
                    {
                        continue;
                    }
                    let mut payload = selected(
                        &row,
                        &[
                            ("symbol", "symbol"),
                            ("day", "day"),
                            ("time", "time"),
                            ("estimated", "timeEstimated"),
                            ("side", "side"),
                            ("quantity", "quantity"),
                            ("price", "price"),
                            ("reference", "reference"),
                            ("reference_source", "referenceSource"),
                            ("value", "value"),
                            ("cost", "cost"),
                            ("loss_bp", "lossBp"),
                            ("fee", "fee"),
                            ("fee_currency", "feeCurrency"),
                        ],
                    );
                    payload.insert(
                        "timeEstimated".into(),
                        Value::Bool(truth(value(&payload, "timeEstimated"))),
                    );
                    connection.execute("UPDATE performance_cost_trade SET payload=? WHERE snapshot_id=? AND id=?",rusqlite::params![strict_json(&Value::Object(payload))?,id,integer(&row,"id")?]).map_err(sql)?;
                }
            }
            connection.execute("UPDATE performance_snapshot SET cost_schema_version=1 WHERE id=? AND cost_schema_version>=2",[&id]).map_err(sql)?;
        }
    }
    Ok(())
}

pub(super) fn results(connection: &Connection) -> Result<()> {
    let mut after = None;
    loop {
      let ids = snapshot_ids(connection, after.as_deref())?;
      if ids.is_empty() { break; }
      after = ids.last().cloned();
      for id in ids {
        let version: i64 = connection.query_row("SELECT result_schema_version FROM performance_snapshot WHERE id=?1", [&id], |row| row.get(0)).map_err(sql)?;
        if version != 2 { continue; }
        let mut ranges = Object::new();
        for range in ["30", "90", "all"] {
            ranges.insert(range.into(), crate::result_reader::read(connection, &id, range, false).map_err(error)?);
        }
        let settings = crate::result_reader::settings(connection, &id).map_err(error)?;
        connection.execute("UPDATE performance_snapshot SET ranges=?1,settings=?2 WHERE id=?3",
            rusqlite::params![strict_json(&Value::Object(ranges))?, strict_json(&settings)?, id]).map_err(sql)?;
      }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn shared_cost_fields_preserve_public_names_nulls_and_scalar_types() {
        let transaction = serde_json::json!({
            "symbol":"A", "side":"buy", "quantity":1.5, "price":2.0,
            "reference":null, "reference_source":"tick", "time":0, "end_time":null,
            "time_estimated":1, "before":null, "target":4.0,
        });
        assert_eq!(
            Value::Object(selected(
                transaction.as_object().unwrap(),
                TRANSACTION_FIELDS
            )),
            serde_json::json!({
                "symbol":"A", "side":"buy", "quantity":1.5, "price":2.0,
                "reference":null, "referenceSource":"tick", "time":0, "endTime":null,
                "timeEstimated":1, "before":null, "target":4.0,
            })
        );
        let summary = serde_json::json!({
            "value":null, "cost":0.0, "loss_bp":null, "coverage":0.5,
            "covered":1, "count":2, "amount_complete":true, "fee_covered":0,
            "covered_value":3.0, "estimated":1,
        });
        assert_eq!(
            Value::Object(selected(summary.as_object().unwrap(), &DAILY_FIELDS[..8])),
            serde_json::json!({
                "value":null, "cost":0.0, "lossBp":null, "coverage":0.5,
                "covered":1, "count":2, "amountComplete":true, "feeCovered":0,
            })
        );
    }

    #[test]
    fn legacy_cost_restore_refuses_nonfinite_values_before_serializing() {
        assert!(strict_json(&json::parse("{\"value\":NaN}").unwrap()).is_err());
        assert!(strict_json(&json::parse("{\"value\":1e400}").unwrap()).is_err());
        assert_eq!(
            strict_json(&json::parse("123456789012345678901234567890").unwrap()).unwrap(),
            "123456789012345678901234567890"
        );
        assert_eq!(
            strict_json(&json::parse("1.234567890123456789").unwrap()).unwrap(),
            "1.2345678901234567"
        );
    }
}
