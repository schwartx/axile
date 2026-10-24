//! 补发需求、intent 关联与通知在业务事务内一起持久化。

use std::path::Path;

use chrono::{DateTime, NaiveDateTime};
use rusqlite::{params, Connection, OptionalExtension, Params};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

use crate::business_facts_ops::encode::is_truthy as truthy;
use crate::error::AnalysisError;

fn database(error: rusqlite::Error) -> AnalysisError {
    AnalysisError::new("DATABASE_SUPPLEMENT", error.to_string())
}

fn invalid(message: impl Into<String>) -> AnalysisError {
    AnalysisError::new("INVALID_REQUEST", message)
}

fn text<'a>(value: &'a Value, key: &str) -> Result<&'a str, AnalysisError> {
    value[key]
        .as_str()
        .ok_or_else(|| invalid(format!("{key} must be a string")))
}

fn integer(value: &Value, key: &str) -> Result<i64, AnalysisError> {
    value[key]
        .as_i64()
        .ok_or_else(|| invalid(format!("{key} must be an integer")))
}

fn rows(
    connection: &Connection,
    sql: &str,
    args: impl Params,
    table: &str,
) -> Result<Vec<Value>, AnalysisError> {
    let mut statement = connection.prepare(sql).map_err(database)?;
    let mut cursor = statement.query(args).map_err(database)?;
    let mut values = Vec::new();
    while let Some(row) = cursor.next().map_err(database)? {
        values.push(Value::Object(
            crate::business_detail::decode(row, table).map_err(AnalysisError::from)?,
        ));
    }
    Ok(values)
}

fn load(
    connection: &Connection,
    table: &str,
    column: &str,
    value: &Value,
) -> Result<Option<Value>, AnalysisError> {
    Ok(
        crate::business_detail::load(connection, table, column, value)
            .map_err(AnalysisError::from)?
            .into_iter()
            .next()
            .map(Value::Object),
    )
}

fn intent(connection: &Connection, execution_id: &str) -> Result<Option<Value>, AnalysisError> {
    let mut value = load(
        connection,
        "execution_intent",
        "execution_id",
        &json!(execution_id),
    )?;
    if let Some(payload) = value.as_mut().and_then(|row| row.get_mut("payload")) {
        if let Some(encoded) = payload.as_str() {
            *payload = serde_json::from_str(encoded).map_err(|error| invalid(error.to_string()))?;
        }
    }
    Ok(value)
}

fn write(
    path: &str,
    task: impl FnOnce(&Connection) -> Result<Value, AnalysisError>,
) -> Result<Value, AnalysisError> {
    let connection = crate::writer_scope::open_writer(Path::new(path))?;
    crate::writer_scope::immediate(&connection, || task(&connection))
}

fn read(
    path: &str,
    task: impl FnOnce(&Connection) -> Result<Value, AnalysisError>,
) -> Result<Value, AnalysisError> {
    crate::with_read_snapshot(Path::new(path), task)
}

fn moment(value: &str) -> Result<i64, AnalysisError> {
    DateTime::parse_from_rfc3339(value)
        .map(|value| value.timestamp_micros())
        .or_else(|_| {
            NaiveDateTime::parse_from_str(value, "%Y-%m-%dT%H:%M:%S%.f")
                .map(|value| value.and_utc().timestamp_micros())
        })
        .map_err(|error| invalid(format!("invalid supplement time: {error}")))
}

fn account_schedule(
    connection: &Connection,
    account_id: i64,
) -> Result<Option<Value>, AnalysisError> {
    let row = connection.query_row(
        "SELECT a.trade_channel,a.portfolio_id,s.schedule FROM account a JOIN account_settings s ON s.account_id=a.id WHERE a.id=?1",
        [account_id],
        |row| Ok((row.get::<_, String>(0)?, row.get::<_, Option<i64>>(1)?, row.get::<_, String>(2)?)),
    ).optional().map_err(database)?;
    row.map(|(channel, portfolio, schedule)| {
        let schedule: Value =
            serde_json::from_str(&schedule).map_err(|error| invalid(error.to_string()))?;
        Ok(json!({"trade_channel":channel,"portfolio_id":portfolio,"schedule":schedule}))
    })
    .transpose()
}

fn fingerprint_wire(value: &Value) -> Result<String, AnalysisError> {
    Ok(match value {
        Value::String(value) => serde_json::to_string(value)
            .map_err(|error| invalid(error.to_string()))?
            .chars()
            .map(|character| {
                if character < '\u{7f}' {
                    character.to_string()
                } else {
                    character
                        .encode_utf16(&mut [0; 2])
                        .iter()
                        .map(|unit| format!("\\u{unit:04x}"))
                        .collect()
                }
            })
            .collect(),
        Value::Array(values) => format!(
            "[{}]",
            values
                .iter()
                .map(fingerprint_wire)
                .collect::<Result<Vec<_>, _>>()?
                .join(", ")
        ),
        Value::Object(values) => {
            let mut keys: Vec<_> = values.keys().collect();
            keys.sort();
            format!(
                "{{{}}}",
                keys.into_iter()
                    .map(|key| Ok(format!(
                        "{}: {}",
                        fingerprint_wire(&json!(key))?,
                        fingerprint_wire(&values[key])?
                    )))
                    .collect::<Result<Vec<_>, AnalysisError>>()?
                    .join(", ")
            )
        }
        _ => value.to_string(),
    })
}

fn fingerprint(account: &Value) -> Result<String, AnalysisError> {
    let value = json!([
        account["schedule"]["cron_expr"],
        account["schedule"]["supplement"],
        account["trade_channel"],
        account["portfolio_id"]
    ]);
    Ok(format!(
        "{:x}",
        Sha256::digest(fingerprint_wire(&value)?.as_bytes())
    ))
}

fn enabled(account: &Value) -> bool {
    truthy(&account["schedule"]["is_started"])
        && truthy(&account["schedule"]["supplement"])
        && truthy(&account["portfolio_id"])
}

pub(crate) fn steps_for_group(
    connection: &Connection,
    group_id: &str,
) -> Result<Vec<Value>, AnalysisError> {
    rows(
        connection,
        "SELECT * FROM supplement_step WHERE group_id=?1 ORDER BY \"index\"",
        [group_id],
        "supplement_step",
    )
}

fn linked_steps(connection: &Connection, execution_id: &str) -> Result<Vec<Value>, AnalysisError> {
    rows(
        connection,
        "SELECT * FROM supplement_step WHERE execution_id=?1 ORDER BY rowid",
        [execution_id],
        "supplement_step",
    )
}

fn runnable_group(
    connection: &Connection,
    step: &Value,
    request: &Value,
    pending: bool,
) -> Result<Option<Value>, AnalysisError> {
    let Some(group) = load(connection, "supplement_group", "id", &step["group_id"])? else {
        return Ok(None);
    };
    if group["status"] != "active" || (pending && step["status"] != "pending") {
        return Ok(None);
    }
    let Some(account) = account_schedule(connection, integer(&group, "account_id")?)? else {
        return Ok(None);
    };
    if !enabled(&account)
        || fingerprint(&account)? != text(&group, "fingerprint")?
        || moment(text(request, "now")?)? >= moment(text(&group, "expires_at")?)?
    {
        return Ok(None);
    }
    let channel = text(&account, "trade_channel")?;
    Ok(matches!(
        request["calendar_status_by_channel"][channel].as_str(),
        Some("available_open" | "not_required")
    )
    .then_some(group))
}

pub fn create_group(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    write(path, |connection| {
        let account_id = integer(request, "account_id")?;
        let Some(account) = account_schedule(connection, account_id)? else {
            return Ok(Value::Null);
        };
        let group = &request["group"];
        if !enabled(&account) || fingerprint(&account)? != text(group, "fingerprint")? {
            return Ok(Value::Null);
        }
        let planned = request["steps"]
            .as_array()
            .ok_or_else(|| invalid("steps must be an array"))?;
        if planned.is_empty() || integer(group, "effective_count")? != planned.len() as i64 - 1 {
            return Err(invalid("invalid supplement plan"));
        }
        let changed = connection.execute(
            "INSERT INTO supplement_group(id,account_id,fingerprint,base_scheduled_at,expires_at,configured_count,effective_count,status,end_reason,cancel_requested_at) VALUES (?1,?2,?3,?4,?5,?6,?7,'active',NULL,NULL) ON CONFLICT(account_id,fingerprint,base_scheduled_at) DO NOTHING",
            params![text(group,"id")?,account_id,text(group,"fingerprint")?,text(group,"base_scheduled_at")?,text(group,"expires_at")?,integer(group,"configured_count")?,integer(group,"effective_count")?],
        ).map_err(database)?;
        if changed == 0 {
            return Ok(Value::Null);
        }
        for (index, step) in planned.iter().enumerate() {
            if integer(step, "index")? != index as i64 || step["group_id"] != group["id"] {
                return Err(invalid("invalid supplement step identity"));
            }
            connection.execute("INSERT INTO supplement_step(id,group_id,\"index\",scheduled_at,status,execution_id) VALUES (?1,?2,?3,?4,'pending',NULL)", params![text(step,"id")?,text(group,"id")?,index as i64,text(step,"scheduled_at")?]).map_err(database)?;
        }
        Ok(
            json!({"group":load(connection,"supplement_group","id",&group["id"])? ,"steps":steps_for_group(connection,text(group,"id")?)?}),
        )
    })
}

pub fn step(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    read(path, |connection| {
        let Some(step) = load(
            connection,
            "supplement_step",
            "id",
            &json!(text(request, "step_id")?),
        )?
        else {
            return Ok(Value::Null);
        };
        let Some(group) = load(connection, "supplement_group", "id", &step["group_id"])? else {
            return Ok(Value::Null);
        };
        let account = account_schedule(connection, integer(&group, "account_id")?)?;
        Ok(
            json!({"step":step,"group":group,"channel":account.as_ref().map(|account| &account["trade_channel"])}),
        )
    })
}

pub fn execution_steps(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    read(path, |connection| {
        Ok(json!(linked_steps(
            connection,
            text(request, "execution_id")?
        )?))
    })
}

pub fn execution_requests(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    read(path, |connection| {
        let execution_id = text(request, "execution_id")?;
        let steps = linked_steps(connection, execution_id)?;
        let row = intent(connection, execution_id)?;
        let account = row
            .as_ref()
            .map(|row| account_schedule(connection, integer(row, "account_id")?))
            .transpose()?
            .flatten();
        Ok(
            json!({"steps":steps,"channel":account.as_ref().map(|account| &account["trade_channel"])}),
        )
    })
}

pub(crate) fn attach_step(
    connection: &Connection,
    execution_id: &str,
    step_id: Option<&str>,
    request: &Value,
) -> Result<(), AnalysisError> {
    let row = intent(connection, execution_id)?
        .ok_or_else(|| invalid("execution intent does not exist"))?;
    let Some(step_id) = step_id else {
        if row["kind"] == "rebalance" {
            let mut payload = row["payload"].clone();
            payload
                .as_object_mut()
                .ok_or_else(|| invalid("intent payload must be an object"))?
                .insert("independent_request".into(), json!(true));
            connection
                .execute(
                    "UPDATE execution_intent SET payload=?2 WHERE execution_id=?1",
                    params![execution_id, payload.to_string()],
                )
                .map_err(database)?;
        }
        return Ok(());
    };
    let group = match load(connection, "supplement_step", "id", &json!(step_id))? {
        Some(step) => runnable_group(connection, &step, request, true)?,
        None => None,
    }
    .ok_or_else(|| AnalysisError::new("SUPPLEMENT_NOT_RUNNABLE", "补发需求已取消或过期"))?;
    if group["account_id"] != row["account_id"] {
        return Err(invalid("supplement and intent accounts differ"));
    }
    connection
        .execute(
            "UPDATE supplement_step SET execution_id=?2,status='queued' WHERE id=?1",
            params![step_id, execution_id],
        )
        .map_err(database)?;
    Ok(())
}

fn revoke_cancelled_intents(
    connection: &Connection,
    account_id: i64,
    reason: &str,
    now: &str,
) -> Result<(), AnalysisError> {
    let queued = rows(
        connection,
        "SELECT * FROM execution_intent WHERE account_id=?1 AND status='QUEUED'",
        [account_id],
        "execution_intent",
    )?;
    for row in queued {
        let execution_id = text(&row, "execution_id")?;
        let row = intent(connection, execution_id)?.unwrap();
        if truthy(&row["payload"]["independent_request"]) {
            continue;
        }
        let steps = linked_steps(connection, execution_id)?;
        if !steps.is_empty() && steps.iter().all(|step| step["status"] == "cancelled") {
            connection.execute("UPDATE execution_intent SET status='TERMINATED',finished_at=?2,cancel_requested_at=?2,cancel_reason=?3 WHERE execution_id=?1 AND status='QUEUED'",params![execution_id,now,reason]).map_err(database)?;
        }
    }
    Ok(())
}

pub(crate) fn supplement_context(group: &Value, step: &Value) -> Value {
    json!({"group_id":group["id"],"base_scheduled_at":group["base_scheduled_at"],"scheduled_at":step["scheduled_at"],"index":step["index"],"configured_count":group["configured_count"],"effective_count":group["effective_count"],"is_last":step["index"]==group["effective_count"],"group_status":group["status"],"end_reason":group["end_reason"]})
}

pub(crate) fn settle_groups(
    connection: &Connection,
    account_id: i64,
    now: &str,
) -> Result<(), AnalysisError> {
    let groups = rows(connection,"SELECT * FROM supplement_group WHERE account_id=?1 AND status IN ('active','cancelling') ORDER BY rowid",[account_id],"supplement_group")?;
    for mut group in groups {
        let group_id = text(&group, "id")?.to_owned();
        let steps = steps_for_group(connection, &group_id)?;
        if steps.iter().any(|step| {
            matches!(
                step["status"].as_str(),
                Some("pending" | "queued" | "running")
            )
        }) {
            continue;
        }
        if group["status"] == "active" {
            group["status"] = json!("completed")
        } else {
            group["status"] = json!("cancelled");
            let event_id = format!("supplement:{group_id}:cancelled");
            let last = rows(connection,"SELECT n.* FROM notification_event n WHERE n.account_id=?1 AND EXISTS (SELECT 1 FROM supplement_step s WHERE s.group_id=?2 AND n.id='execution:'||s.execution_id) ORDER BY n.sequence DESC LIMIT 1",params![account_id,group_id],"notification_event")?.into_iter().next();
            let snapshot = last.as_ref().filter(|last| {
                steps.iter().any(|step| {
                    !step["execution_id"].is_null()
                        && step["execution_id"] == last["context"]["execution"]["id"]
                })
            });
            let cancelled: Vec<_> = steps
                .iter()
                .filter(|step| step["status"] == "cancelled")
                .map(|step| {
                    let mut context = supplement_context(&group, step);
                    context["status"] = step["status"].clone();
                    context["execution_id"] = step["execution_id"].clone();
                    context
                })
                .collect();
            crate::notifications::enqueue_in_connection(
                connection,
                &json!({"id":event_id,"account_id":account_id,"event_type":"supplement.cancelled","created_at":now,"context":{"event_id":event_id,"event_type":"supplement.cancelled","execution":null,"is_test":false,"supplement":{"group_id":group_id,"base_scheduled_at":group["base_scheduled_at"],"reason":group["end_reason"],"cancel_requested_at":group["cancel_requested_at"],"steps":cancelled},"last_execution":snapshot.map(|last| &last["context"]),"last_execution_at":snapshot.map(|last| &last["created_at"])}}),
            )?;
        }
        connection
            .execute(
                "UPDATE supplement_group SET status=?2 WHERE id=?1",
                params![group_id, text(&group, "status")?],
            )
            .map_err(database)?;
    }
    Ok(())
}

pub(crate) fn cancel_groups_in_connection(
    connection: &Connection,
    account_id: i64,
    reason: &str,
    group_id: Option<&str>,
    now: &str,
) -> Result<bool, AnalysisError> {
    let groups = rows(connection,"SELECT * FROM supplement_group WHERE account_id=?1 AND status='active' AND (?2 IS NULL OR id=?2)",params![account_id,group_id],"supplement_group")?;
    for group in &groups {
        let id = text(group, "id")?;
        connection.execute("UPDATE supplement_group SET status='cancelling',end_reason=?2,cancel_requested_at=?3 WHERE id=?1",params![id,reason,now]).map_err(database)?;
        connection.execute("UPDATE supplement_step SET status='cancelled' WHERE group_id=?1 AND (status='pending' OR (status='queued' AND EXISTS (SELECT 1 FROM execution_intent i WHERE i.execution_id=supplement_step.execution_id AND i.status='QUEUED')))",[id]).map_err(database)?;
    }
    revoke_cancelled_intents(connection, account_id, reason, now)?;
    settle_groups(connection, account_id, now)?;
    Ok(!groups.is_empty())
}

pub fn cancel_groups(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    write(path, |connection| {
        Ok(json!(cancel_groups_in_connection(
            connection,
            integer(request, "account_id")?,
            text(request, "reason")?,
            request["group_id"].as_str(),
            text(request, "now")?
        )?))
    })
}

pub fn validate_requests(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    write(path, |connection| {
        let execution_id = text(request, "execution_id")?;
        let steps = linked_steps(connection, execution_id)?;
        if steps.is_empty() {
            return Ok(json!({"valid":true,"account_id":null}));
        }
        let row = intent(connection, execution_id)?
            .ok_or_else(|| invalid("execution intent does not exist"))?;
        let account_id = integer(&row, "account_id")?;
        let mut valid = truthy(&row["payload"]["independent_request"]);
        let mut invalid_groups = Vec::new();
        for step in steps {
            let current = step["status"] != "cancelled"
                && runnable_group(connection, &step, request, false)?.is_some();
            valid |= current;
            connection
                .execute(
                    "UPDATE supplement_step SET status=?2 WHERE id=?1",
                    params![
                        text(&step, "id")?,
                        if current { "queued" } else { "cancelled" }
                    ],
                )
                .map_err(database)?;
            if !current {
                invalid_groups.push(text(&step, "group_id")?.to_owned())
            }
        }
        for group_id in invalid_groups {
            cancel_groups_in_connection(
                connection,
                account_id,
                "schedule_interrupted",
                Some(&group_id),
                text(request, "now")?,
            )?;
        }
        Ok(json!({"valid":valid,"account_id":account_id}))
    })
}

fn unstarted(connection: &Connection, step: &Value) -> Result<bool, AnalysisError> {
    let row = step["execution_id"]
        .as_str()
        .map(|id| intent(connection, id))
        .transpose()?
        .flatten();
    Ok(row.as_ref().map_or(true, |row| row["status"] == "QUEUED"))
}

pub fn expire_group(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    write(path, |connection| {
        let Some(group) = load(
            connection,
            "supplement_group",
            "id",
            &json!(text(request, "group_id")?),
        )?
        else {
            return Ok(Value::Null);
        };
        if group["status"] != "active" {
            return Ok(Value::Null);
        }
        let steps = steps_for_group(connection, text(&group, "id")?)?;
        let mut unfinished = false;
        for step in steps {
            unfinished |= step["status"] == "pending"
                || (step["status"] == "queued" && unstarted(connection, &step)?);
        }
        if !unfinished {
            return Ok(Value::Null);
        }
        let account_id = integer(&group, "account_id")?;
        let changed = cancel_groups_in_connection(
            connection,
            account_id,
            "schedule_interrupted",
            Some(text(&group, "id")?),
            text(request, "now")?,
        )?;
        Ok(json!({"account_id":account_id,"changed":changed}))
    })
}

pub fn recover_supplements(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    write(path, |connection| {
        recover_in_connection(connection, request)
    })
}

fn recover_in_connection(connection: &Connection, request: &Value) -> Result<Value, AnalysisError> {
    let now = text(request, "now")?;
    let groups = rows(
        connection,
        "SELECT * FROM supplement_group WHERE status IN ('active','cancelling') ORDER BY rowid",
        [],
        "supplement_group",
    )?;
    let mut scheduled = Vec::new();
    let mut accounts = Vec::new();
    for group in groups {
        let account_id = integer(&group, "account_id")?;
        if !accounts.contains(&account_id) {
            accounts.push(account_id)
        }
        let group_id = text(&group, "id")?;
        let mut last_missed = false;
        for step in steps_for_group(connection, group_id)? {
            if !matches!(step["status"].as_str(), Some("pending" | "queued"))
                || moment(text(&step, "scheduled_at")?)? > moment(now)?
            {
                continue;
            }
            last_missed |= step["index"] == group["effective_count"];
            if unstarted(connection, &step)? {
                connection
                    .execute(
                        "UPDATE supplement_step SET status='cancelled' WHERE id=?1",
                        [text(&step, "id")?],
                    )
                    .map_err(database)?;
            }
        }
        revoke_cancelled_intents(connection, account_id, "schedule_interrupted", now)?;
        if group["status"] == "active"
            && (last_missed || moment(text(&group, "expires_at")?)? <= moment(now)?)
        {
            cancel_groups_in_connection(
                connection,
                account_id,
                "schedule_interrupted",
                Some(group_id),
                now,
            )?;
        } else if group["status"] == "active" {
            scheduled.push(json!({"group":group,"steps":steps_for_group(connection,group_id)?.into_iter().filter(|step| step["status"]=="pending").collect::<Vec<_>>()}));
        } else {
            settle_groups(connection, account_id, now)?;
        }
    }
    Ok(json!({"groups":scheduled,"account_ids":accounts}))
}

fn session_closed_all(result: &Value) -> bool {
    if result["reason_code"] != "COMMON.SESSION.CLOSED"
        || result["status"] != "BLOCKED"
        || result["task_status"] == "TERMINATED"
        || truthy(&result["orders"])
        || truthy(&result["trades"])
    {
        return false;
    }
    let Some(symbols) = result["symbol_results"].as_object() else {
        return result.get("symbol_results").is_none();
    };
    symbols.values().all(|symbol| {
        symbol["status"] == "BLOCKED"
            && symbol["reason_code"] == "COMMON.SESSION.CLOSED"
            && !truthy(&symbol["orders"])
            && !truthy(&symbol["trades"])
    })
}

fn finish_steps(
    connection: &Connection,
    execution_id: &str,
    steps: &[Value],
) -> Result<Map<String, Value>, AnalysisError> {
    connection.execute("UPDATE supplement_step SET status='finished' WHERE execution_id=?1 AND status!='cancelled'",[execution_id]).map_err(database)?;
    let mut groups = Map::new();
    for step in steps {
        let Some(mut group) = load(connection, "supplement_group", "id", &step["group_id"])? else {
            continue;
        };
        let id = text(&group, "id")?.to_owned();
        if group["status"] == "active"
            && steps_for_group(connection, &id)?
                .iter()
                .all(|step| matches!(step["status"].as_str(), Some("finished" | "cancelled")))
        {
            connection
                .execute(
                    "UPDATE supplement_group SET status='completed' WHERE id=?1",
                    [&id],
                )
                .map_err(database)?;
            group["status"] = json!("completed");
        }
        groups.insert(id, group);
    }
    Ok(groups)
}

fn enqueue_execution_finished(
    connection: &Connection,
    row: &Value,
    account: &Value,
    steps: &[Value],
    groups: &Map<String, Value>,
    record: Option<i64>,
    now: &str,
) -> Result<(Option<Value>, bool), AnalysisError> {
    let execution_id = text(row, "execution_id")?;
    let account_id = integer(row, "account_id")?;
    let ran = !row["started_at"].is_null() || row["status"] == "FAILED";
    let event_id = format!("execution:{execution_id}");
    let existing = if ran {
        load(connection, "notification_event", "id", &json!(event_id))?
    } else {
        None
    };
    let needs_result = (ran && existing.is_none()) || row["status"] == "SKIPPED";
    let detail = record
        .filter(|_| needs_result)
        .map(|record_id| crate::notifications::execution_detail(connection, account_id, record_id))
        .transpose()?;
    let session_closed = row["status"] == "SKIPPED"
        && detail
            .as_ref()
            .is_some_and(|detail| session_closed_all(&detail["result"]));
    if !ran {
        return Ok((None, session_closed));
    }
    if let Some(event) = existing {
        if event["account_id"] != row["account_id"] || event["event_type"] != "execution.finished" {
            return Err(invalid("notification and intent identities differ"));
        }
        return Ok((Some(event), session_closed));
    }
    let mut context = crate::notification_context::execution_context(row, account, detail, now);
    context["execution"]["supplements"] = json!(steps
        .iter()
        .filter(|step| step["status"] != "cancelled")
        .filter_map(|step| groups
            .get(step["group_id"].as_str().unwrap_or(""))
            .map(|group| supplement_context(group, step)))
        .collect::<Vec<_>>());
    let mut notification = json!({"id":event_id,"account_id":account_id,"event_type":"execution.finished","context":null,"created_at":now});
    notification["context"] = context;
    let event = crate::notifications::enqueue_in_connection(connection, &notification)?;
    Ok((Some(event), session_closed))
}

/// 补发终态的步骤、通知、引用与组结算沿用同一个调用方事务。
pub(crate) fn finish_supplement_execution(
    connection: &Connection,
    row: &Value,
    request: &Value,
) -> Result<(), AnalysisError> {
    let execution_id = text(row, "execution_id")?;
    let steps = linked_steps(connection, execution_id)?;
    if steps.is_empty() {
        return Ok(());
    }
    let account_id = integer(row, "account_id")?;
    let account = connection.query_row("SELECT id,name,trade_channel FROM account WHERE id=?1",[account_id],|row| Ok(json!({"id":row.get::<_,i64>(0)?,"name":row.get::<_,String>(1)?,"trade_channel":row.get::<_,String>(2)?}))).optional().map_err(database)?;
    let Some(account) = account else {
        return Ok(());
    };
    let now = text(request, "now")?;
    let groups = finish_steps(connection, execution_id, &steps)?;
    let record = connection
        .query_row(
            "SELECT id FROM executerecord WHERE execution_id=?1",
            [execution_id],
            |row| row.get::<_, i64>(0),
        )
        .optional()
        .map_err(database)?;
    let (_, session_closed) =
        enqueue_execution_finished(connection, row, &account, &steps, &groups, record, now)?;
    if session_closed {
        for group_id in groups.keys() {
            cancel_groups_in_connection(
                connection,
                account_id,
                "COMMON.SESSION.CLOSED",
                Some(group_id),
                now,
            )?;
        }
    }
    settle_groups(connection, account_id, now)
}

#[cfg(test)]
mod tests {
    use super::*;

    const NOW: &str = "2026-10-09T09:30:00+08:00";
    const SCHEDULE: &str = r#"{"is_started":true,"cron_expr":"*/15 * * * *","supplement":{"count":2,"interval_minutes":1}}"#;

    fn fixture() -> Connection {
        let connection = Connection::open_in_memory().unwrap();
        connection.execute_batch(
            "PRAGMA foreign_keys=ON;
             CREATE TABLE account(id INTEGER PRIMARY KEY,name TEXT,trade_channel TEXT,portfolio_id INTEGER);
             CREATE TABLE account_settings(account_id INTEGER PRIMARY KEY,schedule JSON);
             CREATE TABLE supplement_group(id TEXT PRIMARY KEY,account_id INTEGER,fingerprint TEXT,base_scheduled_at TEXT,expires_at TEXT,configured_count INTEGER,effective_count INTEGER,status TEXT,end_reason TEXT,cancel_requested_at TEXT,UNIQUE(account_id,fingerprint,base_scheduled_at));
             CREATE TABLE supplement_step(id TEXT PRIMARY KEY,group_id TEXT,\"index\" INTEGER,scheduled_at TEXT,status TEXT,execution_id TEXT,UNIQUE(group_id,\"index\"));
             CREATE TABLE execution_intent(id INTEGER PRIMARY KEY,execution_id TEXT UNIQUE,account_id INTEGER,kind TEXT,trigger_source TEXT,status TEXT,payload JSON,algorithm TEXT,started_at TEXT,finished_at TEXT,error TEXT,cancel_requested_at TEXT,cancel_reason TEXT);
             CREATE TABLE notification_event(sequence INTEGER PRIMARY KEY,id TEXT UNIQUE,account_id INTEGER REFERENCES account(id),created_at TEXT,event_type TEXT,context JSON,status TEXT,finished_at TEXT,error TEXT);
             CREATE TABLE executerecord(id INTEGER PRIMARY KEY,account_id INTEGER,execution_id TEXT);
             INSERT INTO account VALUES(1,'test','ctp',1);",
        ).unwrap();
        connection
            .execute("INSERT INTO account_settings VALUES(1,?1)", [SCHEDULE])
            .unwrap();
        let digest = fingerprint(&account_schedule(&connection, 1).unwrap().unwrap()).unwrap();
        connection.execute("INSERT INTO supplement_group VALUES('group',1,?1,?2,'2026-10-09T09:45:00+08:00',2,2,'active',NULL,NULL)",params![digest,NOW]).unwrap();
        for (index, time) in [
            NOW,
            "2026-10-09T09:31:00+08:00",
            "2026-10-09T09:32:00+08:00",
        ]
        .iter()
        .enumerate()
        {
            connection
                .execute(
                    "INSERT INTO supplement_step VALUES(?1,'group',?2,?3,'pending',NULL)",
                    params![format!("step-{index}"), index as i64, time],
                )
                .unwrap();
        }
        connection.execute("INSERT INTO execution_intent(execution_id,account_id,kind,trigger_source,status,payload) VALUES('execution',1,'rebalance','scheduler','QUEUED','{}')",[]).unwrap();
        connection
    }

    fn request() -> Value {
        json!({"now":NOW,"calendar_status_by_channel":{"ctp":"available_open"}})
    }

    #[test]
    fn fingerprint_preserves_existing_python_deduplication_keys() {
        let connection = fixture();
        assert_eq!(
            fingerprint(&account_schedule(&connection, 1).unwrap().unwrap()).unwrap(),
            "976b25430f5eecc1a70d71023c8a7b1490665f0965e65164538e2c95536f355a"
        );
        assert_eq!(
            fingerprint_wire(&json!(["渠道🦀",{"b":2,"a":1}])).unwrap(),
            r#"["\u6e20\u9053\ud83e\udd80", {"a": 1, "b": 2}]"#
        );
    }

    #[test]
    fn attaching_rechecks_schedule_and_does_not_commit_its_callers_transaction() {
        let connection = fixture();
        connection.execute_batch("BEGIN IMMEDIATE").unwrap();
        attach_step(&connection, "execution", Some("step-0"), &request()).unwrap();
        connection.execute_batch("ROLLBACK").unwrap();
        assert_eq!(
            load(&connection, "supplement_step", "id", &json!("step-0"))
                .unwrap()
                .unwrap()["status"],
            "pending"
        );
        connection.execute("UPDATE account_settings SET schedule=json_set(schedule,'$.is_started',json('false'))",[]).unwrap();
        let error = attach_step(&connection, "execution", Some("step-0"), &request()).unwrap_err();
        assert_eq!(error.code, "SUPPLEMENT_NOT_RUNNABLE");
        assert!(load(&connection, "supplement_step", "id", &json!("step-0"))
            .unwrap()
            .unwrap()["execution_id"]
            .is_null());
    }

    #[test]
    fn cancelled_supplement_preserves_a_shared_independent_intent() {
        for independent in [false, true] {
            let connection = fixture();
            attach_step(&connection, "execution", Some("step-0"), &request()).unwrap();
            if independent {
                attach_step(&connection, "execution", None, &request()).unwrap();
            }
            assert!(
                cancel_groups_in_connection(&connection, 1, "account_stopped", None, NOW).unwrap()
            );
            assert!(
                !cancel_groups_in_connection(&connection, 1, "account_stopped", None, NOW).unwrap()
            );
            assert_eq!(
                intent(&connection, "execution").unwrap().unwrap()["status"],
                if independent { "QUEUED" } else { "TERMINATED" }
            );
            assert_eq!(
                load(&connection, "supplement_group", "id", &json!("group"))
                    .unwrap()
                    .unwrap()["status"],
                "cancelled"
            );
            assert_eq!(
                rows(
                    &connection,
                    "SELECT * FROM notification_event",
                    [],
                    "notification_event"
                )
                .unwrap()
                .len(),
                1
            );
        }
    }

    #[test]
    fn failed_cancel_rolls_back_steps_intent_and_outbox_together() {
        let connection = fixture();
        attach_step(&connection, "execution", Some("step-0"), &request()).unwrap();
        let result = crate::writer_scope::immediate(&connection, || {
            cancel_groups_in_connection(&connection, 1, "account_stopped", None, NOW)?;
            Err::<(), _>(invalid("injected account save failure"))
        });
        assert!(result.is_err());
        assert_eq!(
            intent(&connection, "execution").unwrap().unwrap()["status"],
            "QUEUED"
        );
        assert_eq!(
            load(&connection, "supplement_step", "id", &json!("step-0"))
                .unwrap()
                .unwrap()["status"],
            "queued"
        );
        assert_eq!(
            load(&connection, "supplement_group", "id", &json!("group"))
                .unwrap()
                .unwrap()["status"],
            "active"
        );
        assert!(rows(
            &connection,
            "SELECT * FROM notification_event",
            [],
            "notification_event"
        )
        .unwrap()
        .is_empty());
    }

    #[test]
    fn running_cancel_waits_for_finish_and_enqueues_execution_before_cancellation() {
        let connection = fixture();
        attach_step(&connection, "execution", Some("step-0"), &request()).unwrap();
        connection
            .execute(
                "UPDATE execution_intent SET status='RUNNING',started_at=?1",
                [NOW],
            )
            .unwrap();
        cancel_groups_in_connection(&connection, 1, "terminated", None, NOW).unwrap();
        assert_eq!(
            load(&connection, "supplement_group", "id", &json!("group"))
                .unwrap()
                .unwrap()["status"],
            "cancelling"
        );
        assert!(rows(
            &connection,
            "SELECT * FROM notification_event",
            [],
            "notification_event"
        )
        .unwrap()
        .is_empty());
        connection
            .execute(
                "UPDATE execution_intent SET status='TERMINATED',finished_at=?1",
                [NOW],
            )
            .unwrap();
        let row = intent(&connection, "execution").unwrap().unwrap();
        finish_supplement_execution(&connection, &row, &request()).unwrap();
        let events = rows(
            &connection,
            "SELECT * FROM notification_event ORDER BY sequence",
            [],
            "notification_event",
        )
        .unwrap();
        assert_eq!(
            events
                .iter()
                .map(|event| &event["event_type"])
                .collect::<Vec<_>>(),
            vec![&json!("execution.finished"), &json!("supplement.cancelled")]
        );
        assert_eq!(
            events[0]["context"]["execution"]["supplements"][0]["group_status"],
            "cancelling"
        );
        assert_eq!(events[1]["context"]["last_execution"], events[0]["context"]);
    }

    #[test]
    fn unstarted_preserves_missing_intents_and_historical_payloads() {
        let connection = fixture();
        for id in [Value::Null, json!("missing"), json!("execution")] {
            assert!(unstarted(&connection, &json!({"execution_id":id})).unwrap());
        }
        connection
            .execute("UPDATE execution_intent SET payload='[]'", [])
            .unwrap();
        assert!(unstarted(&connection, &json!({"execution_id":"execution"})).unwrap());
        for status in [
            "RUNNING",
            "TERMINATING",
            "SUCCEEDED",
            "FAILED",
            "TERMINATED",
        ] {
            connection
                .execute("UPDATE execution_intent SET status=?1", [status])
                .unwrap();
            assert!(!unstarted(&connection, &json!({"execution_id":"execution"})).unwrap());
        }
        connection
            .execute("UPDATE execution_intent SET payload='broken'", [])
            .unwrap();
        assert_eq!(
            unstarted(&connection, &json!({"execution_id":"execution"}))
                .unwrap_err()
                .code,
            "INVALID_REQUEST"
        );
    }

    #[test]
    fn recovery_skips_an_intermediate_miss_and_cancels_an_expired_last_step_once() {
        let connection = fixture();
        connection
            .execute(
                "UPDATE supplement_step SET status='finished' WHERE id='step-0'",
                [],
            )
            .unwrap();
        let recovered =
            recover_in_connection(&connection, &json!({"now":"2026-10-09T09:31:30+08:00"}))
                .unwrap();
        assert_eq!(recovered["groups"][0]["steps"].as_array().unwrap().len(), 1);
        assert_eq!(recovered["groups"][0]["steps"][0]["id"], "step-2");
        assert_eq!(
            load(&connection, "supplement_step", "id", &json!("step-1"))
                .unwrap()
                .unwrap()["status"],
            "cancelled"
        );
        assert_eq!(
            load(&connection, "supplement_group", "id", &json!("group"))
                .unwrap()
                .unwrap()["status"],
            "active"
        );
        assert!(rows(
            &connection,
            "SELECT * FROM notification_event",
            [],
            "notification_event"
        )
        .unwrap()
        .is_empty());
        let recovered =
            recover_in_connection(&connection, &json!({"now":"2026-10-09T09:33:00+08:00"}))
                .unwrap();
        assert!(recovered["groups"].as_array().unwrap().is_empty());
        assert_eq!(
            load(&connection, "supplement_group", "id", &json!("group"))
                .unwrap()
                .unwrap()["status"],
            "cancelled"
        );
        recover_in_connection(&connection, &json!({"now":"2026-10-09T09:33:00+08:00"})).unwrap();
        assert_eq!(
            rows(
                &connection,
                "SELECT * FROM notification_event",
                [],
                "notification_event"
            )
            .unwrap()
            .len(),
            1
        );
    }
}
