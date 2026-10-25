//! 身份、引用及回退顺序保持冻结；纯字段编码复用业务编解码器。

use uuid::Uuid;

use super::*;
use crate::business_detail;
use crate::business_facts_ops::{
    encode, result_extensions, result_head, result_orders, result_symbols, result_trades,
};

const TARGET_FIELDS: &[&str] = &[
    "curr_target",
    "last_target",
    "strategy_target",
    "account_target",
    "sizing_context",
];
const REMOVED: &[(&str, &[&str])] = &[
    ("executerecord", &["raw_input", "raw_result"]),
    ("account_asset_snapshot", &["assets"]),
    (
        "target_weight_snapshot",
        &["raw_weights", "normalized_weights"],
    ),
];
fn identity(kind: &str, id: i64) -> String {
    Uuid::new_v5(
        &Uuid::from_u128(0x4e800ba5_b43d_5ec3_bbb7_f32ede910037),
        format!("{kind}:{id}").as_bytes(),
    )
    .hyphenated()
    .to_string()
}
fn parse(row: &Object, name: &str, table: &str, path: &str) -> Result<Value> {
    json_value(value(row, name)).map_err(|_| {
        error(format!(
            "Cannot migrate {table} id={} path={path}: invalid JSON",
            value(row, "id")
        ))
    })
}
fn select(row: &Object, names: &[&str]) -> Object {
    row.iter()
        .filter(|(name, _)| names.contains(&name.as_str()))
        .map(|(name, item)| (name.clone(), item.clone()))
        .collect()
}
fn except(row: &Object, names: &[&str]) -> Object {
    row.iter()
        .filter(|(name, _)| !names.contains(&name.as_str()))
        .map(|(name, item)| (name.clone(), item.clone()))
        .collect()
}
fn write_error(failure: AnalysisError) -> AnalysisError {
    let code = if failure.code == "BUSINESS_FACTS_SQL" {
        "MIGRATION_SQL"
    } else {
        "MIGRATION_ERROR"
    };
    AnalysisError::caused_by(code, failure)
}
fn update_json(
    connection: &Connection,
    table: &str,
    row: &Object,
    fields: &[(&str, &Value)],
) -> Result<()> {
    let mut encoded = Object::new();
    for (name, payload) in fields {
        let payload =
            serde_json::to_string(payload).map_err(|failure| error(failure.to_string()))?;
        encoded.insert((*name).into(), Value::from(payload));
    }
    update(
        connection,
        table,
        &encoded,
        "id=?",
        &[value(row, "id").clone()],
    )
}
fn each_parent(
    connection: &Connection,
    table: &str,
    mut visitor: impl FnMut(Object) -> Result<()>,
) -> Result<()> {
    let mut after = -1;
    loop {
        let batch = page(connection, table, after)?;
        if batch.is_empty() {
            return Ok(());
        }
        after = integer(batch.last().unwrap(), "id")?;
        for row in batch {
            visitor(row)?;
        }
    }
}
fn insert_input(
    connection: &Connection,
    payload: &Value,
    kind: &str,
    row: &Object,
    execution_id: Option<&str>,
    owner_execution_id: Option<&str>,
) -> Result<String> {
    let id = identity(kind, integer(row, "id")?);
    let batch = encode::encode_inputs(
        &id,
        payload,
        integer(row, "account_id")?,
        execution_id,
        text(row, "created_at")?,
        owner_execution_id
            .filter(|owner| !owner.is_empty())
            .or(execution_id),
    )
    .map_err(error)?;
    crate::business_facts_ops::write_input(connection, &batch).map_err(write_error)?;
    Ok(id)
}
fn load_input(connection: &Connection, id: &str) -> Result<Value> {
    crate::business_facts_ops::load_input_value(connection, id).map_err(|failure| {
        error(if failure.error == "执行输入不存在" {
            "Frozen input identity is missing".into()
        } else {
            failure.error
        })
    })
}
fn result_children(record_id: i64, payload: &Value) -> Result<Vec<(&'static str, encode::Rows)>> {
    let mut result = Vec::new();
    if let Some(map) = payload.as_object() {
        let (collections, symbols, sizings, conversions, ticks, targets) =
            result_symbols::encode_symbols(record_id, payload)?;
        result.extend([
            ("execution_symbol_collection", collections),
            ("execution_symbol_result", symbols),
            ("execution_symbol_sizing", sizings),
            ("execution_symbol_conversion", conversions),
            ("execution_symbol_tick", ticks),
            ("execution_symbol_target", targets),
        ]);
        let (assets, positions, bindings) =
            crate::asset_ops::encode_result_assets(record_id, payload, None).map_err(error)?;
        result.extend([
            ("business_asset_snapshot", assets),
            ("business_asset_position", positions),
            ("business_asset_binding", bindings),
        ]);
        let (collections, orders) = result_orders::encode_orders(record_id, map).map_err(error)?;
        result.extend([
            ("execution_order_collection", collections),
            ("execution_order", orders),
        ]);
        let (collections, trades, conversions) =
            result_trades::encode_trades(record_id, map).map_err(error)?;
        result.extend([
            ("business_trade_collection", collections),
            ("business_trade", trades),
            ("business_trade_conversion", conversions),
        ]);
    }
    result.push((
        "execution_extension",
        vec![result_extensions::encode_result_root_state(record_id, payload).map_err(error)?],
    ));
    Ok(result)
}
pub(super) fn reconstruct_result(connection: &Connection, record: &Object) -> Result<Value> {
    business_detail::read_result_for_migration(connection, integer(record, "account_id")?, integer(record, "id")?).map_err(error)
}

fn record_for_execution(connection: &Connection, execution: &Value) -> Result<Option<Object>> {
    let Some(execution) = execution.as_str() else {
        return Ok(None);
    };
    Ok(rows(
        connection,
        "SELECT * FROM executerecord WHERE execution_id=? LIMIT 1",
        [execution],
    )?
    .into_iter()
    .next())
}
fn backfill_records(connection: &Connection) -> Result<()> {
    each_parent(connection, "executerecord", |row| {
        let record_id = integer(&row, "id")?;
        let raw_input = parse(&row, "raw_input", "executerecord", "/raw_input")?;
        let raw_result = parse(&row, "raw_result", "executerecord", "/raw_result")?;
        let input_id = insert_input(
            connection,
            &raw_input,
            "record-input",
            &row,
            value(&row, "execution_id").as_str(),
            None,
        )
        .map_err(|_| {
            error(format!(
                "Cannot migrate executerecord id={record_id} path=/raw_input"
            ))
        })?;
        let convert = || -> Result<()> {
            let mut result_input_id = None;
            if let Some(input) = raw_result.get("inputs").filter(|input| input.is_object()) {
                let sanitized = encode::sanitize_payload(input).map_err(error)?;
                result_input_id = Some(
                    if encode::py_eq(
                        &encode::canonical_input(&raw_input).map_err(error)?,
                        &encode::canonical_input(&sanitized).map_err(error)?,
                    ) {
                        input_id.clone()
                    } else {
                        insert_input(
                            connection,
                            &sanitized,
                            "record-result-input",
                            &row,
                            None,
                            value(&row, "execution_id").as_str(),
                        )?
                    },
                );
            }
            result_head::write_result_head(
                connection,
                record_id,
                &raw_result,
                value(&row, "is_success").as_i64(),
            )?;
            update(
                connection,
                "executerecord",
                &serde_json::json!({"input_id": input_id, "result_input_id": result_input_id, "facts_complete": true}).as_object().unwrap().clone(),
                "id=?",
                &[Value::from(record_id)],
            )?;
            if let Some(payload) = raw_result.as_object() {
                result_symbols::write_result_symbols(connection, record_id, &raw_result)
                    .map_err(write_error)?;
                crate::asset_ops::write_result_assets(connection, record_id, &raw_result, None)
                    .map_err(write_error)?;
                result_orders::insert_orders(connection, record_id, payload)
                    .map_err(write_error)?;
                result_trades::insert_trades(connection, record_id, payload)
                    .map_err(write_error)?;
            }
            result_extensions::write_result_extensions(connection, record_id, &raw_result)
                .map_err(write_error)?;
            Ok(())
        };
        convert().map_err(|_| {
            error(format!(
                "Cannot migrate executerecord id={record_id} path=/raw_result"
            ))
        })
    })
}
fn marker(view: &str, input: Option<&str>, asset: Option<&str>) -> Object {
    let mut marker = serde_json::json!({"version":1,"view":view})
        .as_object()
        .unwrap()
        .clone();
    if let Some(input) = input {
        marker.insert("input_id".into(), Value::from(input));
    }
    if let Some(asset) = asset {
        marker.insert("asset_id".into(), Value::from(asset));
    }
    marker
}
fn update_artifact(connection: &Connection, row: &Object, mut content: Object) -> Result<()> {
    let marker = content
        .get_mut("business_facts_ref")
        .and_then(Value::as_object_mut)
        .ok_or_else(|| error("Invalid business fact reference"))?;
    marker.insert(
        "legacy_schema_version".into(),
        value(row, "schema_version").clone(),
    );
    connection
        .execute(
            "UPDATE execution_artifact SET content=?,schema_version=3 WHERE id=?",
            rusqlite::params![
                serde_json::to_string(&content).map_err(|e| error(e.to_string()))?,
                integer(row, "id")?
            ],
        )
        .map_err(sql)?;
    Ok(())
}
fn backfill_artifacts(connection: &Connection) -> Result<()> {
    each_parent(connection, "execution_artifact", |row| {
        if !text(&row, "artifact_type")?.eq_ignore_ascii_case("standard_input") {
            return Ok(());
        }
        let content = parse(&row, "content", "execution_artifact", "/content")?;
        let Some(content) = content
            .as_object()
            .filter(|map| map.len() == 1 && map.contains_key("input"))
        else {
            return Ok(());
        };
        let record = record_for_execution(connection, value(&row, "execution_id"))?;
        let mut input_id = record
            .as_ref()
            .and_then(|record| value(record, "input_id").as_str())
            .map(str::to_owned);
        let sanitized = encode::sanitize_payload(value(content, "input")).map_err(error)?;
        let same = match input_id.as_deref() {
            Some(input) => encode::py_eq(
                &load_input(connection, input)?,
                &encode::canonical_input(&sanitized).map_err(error)?,
            ),
            None => false,
        };
        if !same {
            let account=match record.as_ref(){Some(record)=>Some(integer(record,"account_id")?),None=>rows(connection,"SELECT account_id FROM execution_event WHERE execution_id=? ORDER BY id LIMIT 1",[value(&row,"execution_id").as_str()])?.first().and_then(|row|value(row,"account_id").as_i64())};
            let Some(account) = account else {
                return Ok(());
            };
            let mut owner = row.clone();
            owner.insert("account_id".into(), Value::from(account));
            input_id = Some(insert_input(
                connection,
                &sanitized,
                "artifact-input",
                &owner,
                if record.is_some() {
                    None
                } else {
                    value(&row, "execution_id").as_str()
                },
                value(&row, "execution_id").as_str(),
            )?);
        }
        let input = input_id
            .as_deref()
            .ok_or_else(|| error("Frozen input identity is missing"))?;
        if !encode::py_eq(
            &load_input(connection, input)?,
            &encode::canonical_input(&sanitized).map_err(error)?,
        ) {
            return Err(error(format!(
                "Conversion mismatch execution_artifact id={} path=/input",
                integer(&row, "id")?
            )));
        }
        update_artifact(
            connection,
            &row,
            serde_json::json!({"business_facts_ref":marker("input",Some(input),None)})
                .as_object()
                .unwrap()
                .clone(),
        )
    })?;
    backfill_artifact_views(connection)
}
fn artifact_input_owner(connection: &Connection, row: &Object) -> Result<Option<(String, Object)>> {
    if let Some(record) = record_for_execution(connection, value(row, "execution_id"))? {
        let id = text(&record, "input_id")?.to_owned();
        return Ok(Some((id, record)));
    }
    let owner = rows(
        connection,
        "SELECT * FROM execution_input WHERE execution_id=? LIMIT 1",
        [value(row, "execution_id").as_str()],
    )?
    .into_iter()
    .next();
    owner
        .map(|owner| Ok((text(&owner, "id")?.to_owned(), owner)))
        .transpose()
}
fn asset_value(connection: &Connection, id: &str) -> Result<Value> {
    let roots = business_detail::load(
        connection,
        "business_asset_snapshot",
        "asset_id",
        &Value::from(id),
    )
    .map_err(error)?;
    let root = roots
        .first()
        .filter(|_| roots.len() == 1)
        .ok_or_else(|| error("Frozen asset evidence is missing"))?;
    let positions = business_detail::load(
        connection,
        "business_asset_position",
        "asset_id",
        &Value::from(id),
    )
    .map_err(error)?;
    crate::assets::observation_value(root, &positions).map_err(error)
}
fn canonical_asset(value: &Value) -> Result<Value> {
    encode::business_asset_value(value).map_err(error)
}
fn backfill_artifact_views(connection: &Connection) -> Result<()> {
    each_parent(connection, "execution_artifact", |row| {
        let kind = text(&row, "artifact_type")?.to_ascii_lowercase();
        if ![
            "target_snapshot",
            "trade_rules_snapshot",
            "account_snapshot_before",
            "account_snapshot",
        ]
        .contains(&kind.as_str())
        {
            return Ok(());
        }
        let content = parse(&row, "content", "execution_artifact", "/content")?;
        let Some(content) = content
            .as_object()
            .filter(|content| !content.contains_key("business_facts_ref"))
        else {
            return Ok(());
        };
        let Some((mut input_id, owner)) = artifact_input_owner(connection, &row)? else {
            return Ok(());
        };
        let mut updated;
        if ["target_snapshot", "trade_rules_snapshot"].contains(&kind.as_str()) {
            let fields = if kind == "target_snapshot" {
                TARGET_FIELDS
            } else {
                &["trade_rules", "futures_close_policy"]
            };
            let selected = encode::normalize_targets(&Value::Object(select(content, fields)));
            if selected.is_empty() {
                return Ok(());
            }
            let loaded = load_input(connection, &input_id)?;
            let existing = loaded
                .as_object()
                .map(|map| Value::Object(select(map, fields)))
                .unwrap_or(Value::Null);
            if !encode::py_eq(
                &encode::canonical_input(&Value::Object(selected.clone())).map_err(error)?,
                &encode::canonical_input(&existing).map_err(error)?,
            ) {
                let mut owning = row.clone();
                owning.insert("account_id".into(), value(&owner, "account_id").clone());
                input_id = insert_input(
                    connection,
                    &Value::Object(selected.clone()),
                    "artifact-view-input",
                    &owning,
                    None,
                    value(&row, "execution_id").as_str(),
                )?;
            }
            let decoded = load_input(connection, &input_id)?;
            let valid = decoded.as_object().is_some_and(|map| {
                selected.iter().all(|(key, item)| {
                    map.get(key)
                        .is_some_and(|stored| encode::py_eq(stored, item))
                })
            });
            if !valid {
                return Err(error(format!(
                    "Conversion mismatch execution_artifact id={} path=/content",
                    integer(&row, "id")?
                )));
            }
            updated = except(content, fields);
            updated.insert(
                "business_facts_ref".into(),
                Value::Object(marker(
                    if kind == "target_snapshot" {
                        "targets"
                    } else {
                        "rules"
                    },
                    Some(&input_id),
                    None,
                )),
            );
        } else {
            let Some(assets) = content.get("account_assets") else {
                return Ok(());
            };
            let before = kind == "account_snapshot_before";
            let expected_asset = canonical_asset(assets)?;
            let mut asset_id = format!(
                "input:{input_id}:/{}account_assets",
                if before { "before_" } else { "after_" }
            );
            let record = record_for_execution(connection, value(&row, "execution_id"))?;
            let result = record
                .as_ref()
                .map(|record| reconstruct_result(connection, record))
                .transpose()?;
            if !before
                && result
                    .as_ref()
                    .and_then(|result| result.get("account_assets"))
                    .is_some_and(|item| encode::py_eq(item, &expected_asset))
            {
                asset_id = format!(
                    "record:{}:/account_assets",
                    integer(record.as_ref().unwrap(), "id")?
                );
            } else {
                let payload = serde_json::json!({"account_assets":assets});
                let batch = if before {
                    encode::encode_input_assets(&input_id, &payload)
                } else {
                    encode::encode_input_after_assets(&input_id, payload.as_object().unwrap())
                }
                .map_err(error)?;
                crate::asset_ops::write_observation(connection, batch.as_ref())
                    .map_err(write_error)?;
            }
            if !encode::py_eq(&asset_value(connection, &asset_id)?, &expected_asset) {
                return Err(error(format!(
                    "Conversion mismatch execution_artifact id={} path=/account_assets",
                    integer(&row, "id")?
                )));
            }
            updated = except(content, &["account_assets"]);
            updated.insert(
                "business_facts_ref".into(),
                Value::Object(marker(
                    if before {
                        "assets_before"
                    } else {
                        "assets_after"
                    },
                    Some(&input_id),
                    Some(&asset_id),
                )),
            );
        }
        update_artifact(connection, &row, updated)
    })
}
fn backfill_decisions(connection: &Connection) -> Result<()> {
    each_parent(connection, "execution_event", |row| {
        if !text(&row, "event_type")?.eq_ignore_ascii_case("target_computed") {
            return Ok(());
        }
        let details = parse(&row, "details", "execution_event", "/details")?;
        let Some(details) = details.as_object() else {
            return Ok(());
        };
        let Some(decision) = details
            .get("decision")
            .and_then(Value::as_object)
            .filter(|decision| !decision.contains_key("business_facts_ref"))
        else {
            return Ok(());
        };
        let selected = encode::normalize_targets(&Value::Object(select(decision, TARGET_FIELDS)));
        if selected.is_empty() {
            return Ok(());
        }
        let record = record_for_execution(connection, value(&row, "execution_id"))?;
        let mut input_id = record
            .as_ref()
            .and_then(|record| value(record, "input_id").as_str())
            .map(str::to_owned);
        let frozen = input_id
            .as_deref()
            .map(|input| load_input(connection, input))
            .transpose()?;
        let same = frozen
            .as_ref()
            .and_then(Value::as_object)
            .is_some_and(|map| {
                selected
                    .iter()
                    .all(|(key, item)| encode::py_eq(value(map, key), item))
            });
        if !same {
            let mut owner = row.clone();
            owner.insert("created_at".into(), value(&row, "ts_local_created").clone());
            input_id = Some(insert_input(
                connection,
                &Value::Object(selected.clone()),
                "event-decision-input",
                &owner,
                None,
                value(&row, "execution_id").as_str(),
            )?);
        }
        let input = input_id
            .as_deref()
            .ok_or_else(|| error("Frozen input identity is missing"))?;
        let decoded = load_input(connection, input)?;
        if !decoded.as_object().is_some_and(|map| {
            selected
                .iter()
                .all(|(key, item)| encode::py_eq(value(map, key), item))
        }) {
            return Err(error(format!(
                "Conversion mismatch execution_event id={} path=/details/decision",
                integer(&row, "id")?
            )));
        }
        let mut reference = marker("targets", Some(input), None);
        reference.insert(
            "select_fields".into(),
            Value::Array(
                selected
                    .keys()
                    .map(|key| Value::from(key.as_str()))
                    .collect(),
            ),
        );
        let mut decision = except(decision, TARGET_FIELDS);
        decision.insert("business_facts_ref".into(), Value::Object(reference));
        let mut details = details.clone();
        details.insert("decision".into(), Value::Object(decision));
        update_json(
            connection,
            "execution_event",
            &row,
            &[("details", &Value::Object(details))],
        )
    })
}
fn backfill_assets(connection: &Connection) -> Result<()> {
    each_parent(connection, "account_asset_snapshot", |row| {
        let assets = parse(&row, "assets", "account_asset_snapshot", "/assets")?;
        let record = record_for_execution(connection, value(&row, "execution_id"))?;
        let result = record
            .as_ref()
            .map(|record| parse(record, "raw_result", "executerecord", "/raw_result"))
            .transpose()?;
        let expected = canonical_asset(&assets)?;
        let reused = match result
            .as_ref()
            .and_then(|result| result.get("account_assets"))
        {
            Some(assets) => encode::py_eq(&canonical_asset(assets)?, &expected),
            None => false,
        };
        let asset_id = if reused {
            format!(
                "record:{}:/account_assets",
                integer(record.as_ref().unwrap(), "id")?
            )
        } else {
            let batch = encode::encode_snapshot_assets(
                integer(&row, "id")?,
                &serde_json::json!({"account_assets":assets}),
                result.as_ref().and_then(Value::as_object),
            )
            .map_err(error)?;
            crate::asset_ops::write_observation(connection, Some(&batch)).map_err(write_error)?;
            format!("snapshot:{}:/account_assets", integer(&row, "id")?)
        };
        connection
            .execute(
                "UPDATE account_asset_snapshot SET asset_fact_id=? WHERE id=?",
                rusqlite::params![asset_id, integer(&row, "id")?],
            )
            .map_err(sql)?;
        Ok(())
    })
}
fn backfill_targets(connection: &Connection) -> Result<()> {
    each_parent(connection, "target_weight_snapshot", |row| {
        let payload = serde_json::json!({"raw_weights":parse(&row,"raw_weights","target_weight_snapshot","/raw_weights")?,"normalized_weights":parse(&row,"normalized_weights","target_weight_snapshot","/normalized_weights")?});
        let (head, children) =
            crate::target_ops::encode_snapshot_weights(integer(&row, "id")?, &payload)?;
        update(
            connection,
            "target_weight_snapshot",
            head.as_object()
                .ok_or_else(|| error("Invalid target snapshot columns"))?,
            "id=?",
            &[value(&row, "id").clone()],
        )?;
        for child in children {
            insert(
                connection,
                "target_weight_snapshot_weight",
                child
                    .as_object()
                    .ok_or_else(|| error("Invalid target snapshot row"))?,
            )?;
        }
        Ok(())
    })
}
fn verify_rows(
    connection: &Connection,
    record_id: i64,
    expected_rows: &[(&str, encode::Rows)],
) -> Result<bool> {
    use std::collections::BTreeMap;

    for (table, expected) in expected_rows {
        let mut key_columns = rows(
            connection,
            &format!("PRAGMA table_info({})", quote(table)),
            [],
        )?;
        key_columns.retain(|column| value(column, "pk").as_i64().is_some_and(|index| index > 0));
        key_columns.sort_by_key(|column| value(column, "pk").as_i64());
        let keys = key_columns
            .iter()
            .map(|column| text(column, "name").map(str::to_owned))
            .collect::<Result<Vec<_>>>()?;
        let row_key = |row: &Object| -> Result<String> {
            serde_json::to_string(&keys.iter().map(|key| value(row, key)).collect::<Vec<_>>())
                .map_err(|failure| error(failure.to_string()))
        };
        let columns = if *table == "executerecord" {
            expected[0]
                .keys()
                .map(|key| quote(key))
                .collect::<Vec<_>>()
                .join(",")
        } else {
            "*".into()
        };
        let predicate = match *table {
            "executerecord" => "id=?",
            "business_asset_position" => {
                "asset_id IN (SELECT asset_id FROM business_asset_snapshot WHERE record_id=?)"
            }
            _ => "record_id=?",
        };
        let mut statement = connection
            .prepare(&format!(
                "SELECT {columns} FROM {} WHERE {predicate}",
                quote(table)
            ))
            .map_err(sql)?;
        let mut cursor = statement.query([record_id]).map_err(sql)?;
        let mut actual = Vec::new();
        while let Some(row) = cursor.next().map_err(sql)? {
            actual.push(business_detail::decode(row, table).map_err(error)?);
        }
        if actual.len() != expected.len() {
            return Ok(false);
        }
        let actual = actual
            .into_iter()
            .map(|row| Ok((row_key(&row)?, row)))
            .collect::<Result<BTreeMap<_, _>>>()?;
        for row in expected {
            let Some(stored) = actual.get(&row_key(row)?) else {
                return Ok(false);
            };
            if row
                .iter()
                .any(|(key, item)| !encode::py_eq(item, value(stored, key)))
            {
                return Ok(false);
            }
        }
    }
    Ok(true)
}
fn target_weights(connection: &Connection, row: &Object) -> Result<Value> {
    let mut parent = row.clone();
    parent.insert(
        "weights_metadata".into(),
        json_value(value(row, "weights_metadata"))?,
    );
    let children = business_detail::load(
        connection,
        "target_weight_snapshot_weight",
        "snapshot_id",
        value(row, "id"),
    )
    .map_err(error)?
    .into_iter()
    .map(Value::Object)
    .collect::<Vec<_>>();
    crate::target_ops::decode_snapshot_weights(&Value::Object(parent), &children)
}
fn verify_conversion(connection: &Connection) -> Result<()> {
    each_parent(connection, "executerecord", |record| {
        let id = integer(&record, "id")?;
        let expected_input =
            encode::sanitize_payload(&parse(&record, "raw_input", "executerecord", "/raw_input")?)
                .map_err(error)?;
        if !encode::py_eq(
            &encode::canonical_input(&expected_input).map_err(error)?,
            &load_input(connection, text(&record, "input_id")?)?,
        ) {
            return Err(error(format!(
                "Conversion mismatch executerecord id={id} path=/raw_input"
            )));
        }
        let expected = parse(&record, "raw_result", "executerecord", "/raw_result")?;
        if let Some(input) = expected.get("inputs").filter(|input| input.is_object()) {
            if !encode::py_eq(
                &encode::canonical_input(input).map_err(error)?,
                &load_input(connection, text(&record, "result_input_id")?)?,
            ) {
                return Err(error(format!(
                    "Conversion mismatch executerecord id={id} path=/raw_result"
                )));
            }
        }
        let is_success = value(&record, "is_success").as_i64();
        let mut expected_rows = result_children(id, &expected)?;
        let (head, cancellations) = result_head::encode_head(id, &expected, is_success)?;
        expected_rows.extend([
            ("executerecord", vec![head]),
            ("execution_termination_cancel_failure", cancellations),
        ]);
        if !verify_rows(connection, id, &expected_rows)? {
            return Err(error(format!(
                "Conversion mismatch executerecord id={id} path=/raw_result"
            )));
        }
        reconstruct_result(connection, &record)?;
        Ok(())
    })?;
    each_parent(connection, "account_asset_snapshot", |row| {
        let expected =
            canonical_asset(&parse(&row, "assets", "account_asset_snapshot", "/assets")?)?;
        if !encode::py_eq(
            &expected,
            &asset_value(connection, text(&row, "asset_fact_id")?)?,
        ) {
            return Err(error(format!(
                "Conversion mismatch account_asset_snapshot id={} path=/assets",
                integer(&row, "id")?
            )));
        }
        Ok(())
    })?;
    each_parent(connection, "target_weight_snapshot", |row| {
        let id = integer(&row, "id")?;
        let original = serde_json::json!({"raw_weights":parse(&row,"raw_weights","target_weight_snapshot","/raw_weights")?,"normalized_weights":parse(&row,"normalized_weights","target_weight_snapshot","/normalized_weights")?});
        let (expected_head, expected_rows) =
            crate::target_ops::encode_snapshot_weights(id, &original)?;
        let expected = crate::target_ops::decode_snapshot_weights(&expected_head, &expected_rows)?;
        let actual = target_weights(connection, &row)?;
        if !encode::py_eq(&expected, &actual) {
            return Err(error(format!(
                "Conversion mismatch target_weight_snapshot id={id} path=/weights"
            )));
        }
        Ok(())
    })
}
fn pause_triggers(connection: &Connection) -> Result<Vec<Object>> {
    let triggers=rows(connection,"SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name IN ('executerecord','account_asset_snapshot','target_weight_snapshot')",[])?;
    for trigger in &triggers {
        execute(
            connection,
            &format!("DROP TRIGGER {}", quote(text(trigger, "name")?)),
        )?;
    }
    Ok(triggers)
}
fn restore_triggers(connection: &Connection, triggers: &[Object], typed: bool) -> Result<()> {
    for trigger in triggers {
        let name = text(trigger, "name")?;
        if name.starts_with("business_facts_0037_")
            || (typed
                && [
                    "analysis_facts_record_update",
                    "analysis_structured_record_update",
                ]
                .contains(&name))
        {
            continue;
        }
        execute(connection, text(trigger, "sql")?)?;
    }
    Ok(())
}
pub(super) fn upgrade(connection: &Connection) -> Result<()> {
    let paused = pause_triggers(connection)?;
    execute(connection, include_str!("business37.sql"))?;
    for (table, columns) in schema::business_columns() {
        schema::add_columns(connection, table, &columns)?;
    }
    backfill_records(connection)?;
    backfill_artifacts(connection)?;
    backfill_decisions(connection)?;
    backfill_assets(connection)?;
    backfill_targets(connection)?;
    verify_conversion(connection)?;
    for (table, columns) in REMOVED {
        for name in *columns {
            execute(
                connection,
                &format!("ALTER TABLE {} DROP COLUMN {}", quote(table), quote(name)),
            )?;
        }
    }
    restore_triggers(connection, &paused, true)?;
    guards::install_business(connection)
}
pub(super) fn preflight_downgrade(connection: &Connection) -> Result<()> {
    if !rows(connection,"SELECT id FROM execution_input AS i WHERE NOT EXISTS(SELECT 1 FROM executerecord WHERE input_id=i.id OR result_input_id=i.id) AND NOT EXISTS(SELECT 1 FROM execution_artifact WHERE json_valid(content) AND json_extract(content,'$.business_facts_ref.input_id')=i.id) AND NOT EXISTS(SELECT 1 FROM execution_event WHERE json_valid(details) AND json_extract(details,'$.decision.business_facts_ref.input_id')=i.id) LIMIT 1",[])?.is_empty(){return Err(error("Cannot downgrade 0037: unrepresented pending execution input; retain this database or restore its backup"));}
    if !rows(connection,"SELECT asset_id FROM business_asset_snapshot AS a WHERE NOT EXISTS(SELECT 1 FROM business_asset_binding WHERE asset_id=a.asset_id) AND NOT EXISTS(SELECT 1 FROM account_asset_snapshot WHERE asset_fact_id=a.asset_id) AND NOT EXISTS(SELECT 1 FROM execution_artifact WHERE json_valid(content) AND json_extract(content,'$.business_facts_ref.asset_id')=a.asset_id) LIMIT 1",[])?.is_empty(){return Err(error("Cannot downgrade 0037: unrepresented pending asset evidence; retain this database or restore its backup"));}
    Ok(())
}

fn restore_records(connection: &Connection) -> Result<()> {
    each_parent(connection, "executerecord", |record| {
        let raw_input = value(&record, "input_id")
            .as_str()
            .map(|id| load_input(connection, id))
            .transpose()?
            .unwrap_or(Value::Null);
        let result = reconstruct_result(connection, &record)?;
        update_json(
            connection,
            "executerecord",
            &record,
            &[("raw_input", &raw_input), ("raw_result", &result)],
        )
    })
}
fn restore_artifacts(connection: &Connection) -> Result<()> {
    each_parent(connection, "execution_artifact", |row| {
        let content = parse(&row, "content", "execution_artifact", "/content")?;
        let Some(content) = content.as_object() else {
            return Ok(());
        };
        let Some(reference) = content.get("business_facts_ref").and_then(Value::as_object) else {
            return Ok(());
        };
        let view = text(reference, "view")?;
        let fields = if matches!(view, "assets_before" | "assets_after") {
            serde_json::json!({"account_assets":asset_value(connection,text(reference,"asset_id")?)?}).as_object().unwrap().clone()
        } else {
            let input = load_input(connection, text(reference, "input_id")?)?;
            if view == "input" {
                serde_json::json!({"input":input})
                    .as_object()
                    .unwrap()
                    .clone()
            } else {
                let payload = input
                    .as_object()
                    .ok_or_else(|| error("Frozen input view is unavailable"))?;
                select(
                    payload,
                    if view == "targets" {
                        TARGET_FIELDS
                    } else {
                        &["trade_rules", "futures_close_policy"]
                    },
                )
            }
        };
        let mut restored = except(content, &["business_facts_ref"]);
        restored.extend(fields);
        connection
            .execute(
                "UPDATE execution_artifact SET content=?,schema_version=? WHERE id=?",
                rusqlite::params![
                    serde_json::to_string(&restored).map_err(|e| error(e.to_string()))?,
                    value(reference, "legacy_schema_version")
                        .as_i64()
                        .unwrap_or(2),
                    integer(&row, "id")?
                ],
            )
            .map_err(sql)?;
        Ok(())
    })
}
fn restore_decisions(connection: &Connection) -> Result<()> {
    each_parent(connection, "execution_event", |row| {
        let details = parse(&row, "details", "execution_event", "/details")?;
        let Some(details) = details.as_object() else {
            return Ok(());
        };
        let Some(decision) = details.get("decision").and_then(Value::as_object) else {
            return Ok(());
        };
        let Some(reference) = decision
            .get("business_facts_ref")
            .and_then(Value::as_object)
        else {
            return Ok(());
        };
        let failure = || {
            error(format!(
                "Cannot restore execution_event id={} path=/details/decision",
                value(&row, "id")
            ))
        };
        if value(reference, "view") != &Value::from("targets") {
            return Err(failure());
        }
        let payload = load_input(connection, text(reference, "input_id")?)?;
        let payload = payload.as_object().ok_or_else(failure)?;
        let selector = value(reference, "select_fields")
            .as_array()
            .ok_or_else(failure)?;
        let mut restored = except(decision, &["business_facts_ref"]);
        for item in selector {
            let key = item
                .as_str()
                .filter(|key| TARGET_FIELDS.contains(key) && payload.contains_key(*key))
                .ok_or_else(failure)?;
            restored.insert(key.into(), payload[key].clone());
        }
        let mut updated = details.clone();
        updated.insert("decision".into(), Value::Object(restored));
        update_json(
            connection,
            "execution_event",
            &row,
            &[("details", &Value::Object(updated))],
        )
    })
}
fn restore_assets(connection: &Connection) -> Result<()> {
    each_parent(connection, "account_asset_snapshot", |row| {
        update_json(
            connection,
            "account_asset_snapshot",
            &row,
            &[(
                "assets",
                &asset_value(connection, text(&row, "asset_fact_id")?)?,
            )],
        )
    })
}
fn restore_targets(connection: &Connection) -> Result<()> {
    each_parent(connection, "target_weight_snapshot", |row| {
        let mut payload = target_weights(connection, &row)?;
        let invalid = payload
            .get("audit_evidence")
            .and_then(|evidence| evidence.get("invalid_target_values"))
            .and_then(Value::as_object)
            .cloned()
            .unwrap_or_default();
        for role in ["raw_weights", "normalized_weights"] {
            if let Some(invalid) = invalid.get(role).and_then(Value::as_object) {
                for (key, evidence) in invalid {
                    let original = evidence
                        .get("invalid_fields")
                        .and_then(|fields| fields.get("value"))
                        .ok_or_else(|| error("Invalid target weight evidence"))?;
                    payload[role][key] = original.clone();
                }
            }
        }
        update_json(
            connection,
            "target_weight_snapshot",
            &row,
            &[
                ("raw_weights", &payload["raw_weights"]),
                ("normalized_weights", &payload["normalized_weights"]),
            ],
        )
    })
}
pub(super) fn downgrade(connection: &Connection) -> Result<()> {
    preflight_downgrade(connection)?;
    guards::drop_business(connection)?;
    let paused = pause_triggers(connection)?;
    execute(connection,"ALTER TABLE executerecord ADD COLUMN raw_input JSON NOT NULL DEFAULT '{}'; ALTER TABLE executerecord ADD COLUMN raw_result JSON NOT NULL DEFAULT '{}'; ALTER TABLE account_asset_snapshot ADD COLUMN assets JSON NOT NULL DEFAULT '{}'; ALTER TABLE target_weight_snapshot ADD COLUMN raw_weights JSON; ALTER TABLE target_weight_snapshot ADD COLUMN normalized_weights JSON;")?;
    restore_records(connection)?;
    restore_artifacts(connection)?;
    restore_decisions(connection)?;
    restore_assets(connection)?;
    restore_targets(connection)?;
    for (table, columns) in schema::business_columns() {
        schema::drop_columns(connection, table, &columns)?;
    }
    for name in schema::NEW_BUSINESS_TABLES.iter().rev() {
        execute(connection, &format!("DROP TABLE {}", quote(name)))?;
    }
    restore_triggers(connection, &paused, false)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn identities_match_frozen_uuid5_namespace() {
        assert_eq!(
            identity("record-input", 1),
            "4f7d8ab3-96bc-5196-8a52-d5efcfb1744e"
        );
        assert_ne!(
            identity("record-input", 1),
            identity("record-result-input", 1)
        );
    }

    #[test]
    fn document_updates_keep_json_null_separate_from_sql_null() {
        let connection = Connection::open_in_memory().unwrap();
        connection.execute_batch("CREATE TABLE legacy(id INTEGER PRIMARY KEY, first JSON NOT NULL, second JSON NOT NULL); INSERT INTO legacy VALUES(1,'{}','{}')").unwrap();
        let row = serde_json::json!({"id": 1}).as_object().unwrap().clone();
        update_json(
            &connection,
            "legacy",
            &row,
            &[
                ("first", &Value::Null),
                ("second", &serde_json::json!({"nested": null})),
            ],
        )
        .unwrap();
        assert_eq!(
            connection
                .query_row("SELECT first,second FROM legacy", [], |row| Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?
                )))
                .unwrap(),
            ("null".into(), "{\"nested\":null}".into())
        );
    }

    #[test]
    fn before_asset_artifact_references_the_encoded_observation_identity() {
        let connection = Connection::open_in_memory().unwrap();
        connection.execute_batch("PRAGMA foreign_keys=OFF;
            CREATE TABLE account(id INTEGER PRIMARY KEY);
            INSERT INTO account VALUES(1);
            CREATE TABLE executerecord(id INTEGER PRIMARY KEY,account_id INTEGER,execution_id TEXT);
            CREATE TABLE execution_artifact(id INTEGER PRIMARY KEY,execution_id TEXT,artifact_type TEXT,schema_version INTEGER,content JSON,created_at TEXT);
        ").unwrap();
        connection
            .execute_batch(include_str!("business37.sql"))
            .unwrap();
        let input = encode::encode_inputs(
            "frozen-input",
            &serde_json::json!({}),
            1,
            Some("preorder"),
            "2026-01-01T00:00:00",
            None,
        )
        .unwrap();
        crate::business_facts_ops::write_input(&connection, &input).unwrap();
        connection.execute("INSERT INTO execution_artifact VALUES(1,'preorder','ACCOUNT_SNAPSHOT_BEFORE',2,?,'2026-01-01T00:00:00')",[r#"{"account_assets":{"total_asset":9,"positions":[]}}"#]).unwrap();
        backfill_artifact_views(&connection).unwrap();
        let asset=connection.query_row("SELECT json_extract(content,'$.business_facts_ref.asset_id') FROM execution_artifact",[],|row|row.get::<_,String>(0)).unwrap();
        assert_eq!(asset, "input:frozen-input:/before_account_assets");
        assert_eq!(
            asset_value(&connection, &asset).unwrap()["total_asset"].as_f64(),
            Some(9.0)
        );
    }
}
