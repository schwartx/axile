use std::collections::{BTreeMap, HashSet};

use rusqlite::params;
use serde_json::{json, Map};

use super::*;

#[path = "products.rs"]
mod products;

pub(super) fn notification_result(connection: &Connection, upgrade: bool) -> Result<()> {
    if !upgrade {
        sql(
            connection,
            "DELETE FROM account_notification_state WHERE last_success_at IS NULL",
        )?;
    }
    sql(connection, "DROP TRIGGER IF EXISTS account_settings_delete")?;
    let nullable = if upgrade { "" } else { " NOT NULL" };
    let extra = if upgrade {
        ",last_attempt_at TEXT,last_attempt_execution_id TEXT,last_attempt_ok BOOLEAN,last_attempt_error TEXT"
    } else {
        ""
    };
    sql(connection,&format!("CREATE TABLE _alembic_tmp_account_notification_state(account_id INTEGER NOT NULL PRIMARY KEY REFERENCES account(id) ON DELETE CASCADE,last_success_at TEXT{nullable},execution_id TEXT{extra}); INSERT INTO _alembic_tmp_account_notification_state(account_id,last_success_at,execution_id) SELECT account_id,last_success_at,execution_id FROM account_notification_state; DROP TABLE account_notification_state; ALTER TABLE _alembic_tmp_account_notification_state RENAME TO account_notification_state"))?;
    settings::delete_trigger(connection)
}

pub(super) fn control_context(connection: &Connection, upgrade: bool) -> Result<()> {
    let mut tables = vec![
        "account_control_event",
        "account_control_counter_delta",
        "account_control_session",
    ];
    if !upgrade {
        tables.reverse();
    }
    for table in tables {
        if !upgrade {
            sql(connection,&format!("ALTER TABLE {table} DROP COLUMN source; ALTER TABLE {table} DROP COLUMN context_kind"))?;
            continue;
        }
        for column in ["context_kind", "source"] {
            sql(
                connection,
                &format!("ALTER TABLE {table} ADD COLUMN {column} TEXT DEFAULT 'unknown' NOT NULL"),
            )?;
        }
        let mut predicates = Vec::new();
        for model in ["execution_intent", "executerecord", "execution_event"] {
            predicates.push(format!("EXISTS(SELECT 1 FROM {model} e WHERE e.account_id={table}.account_id AND e.execution_id={table}.execution_id)"));
        }
        predicates.push(format!(
            "EXISTS(SELECT 1 FROM execution_artifact e WHERE e.execution_id={table}.execution_id)"
        ));
        sql(
            connection,
            &format!(
                "UPDATE {table} SET context_kind='execution' WHERE {}",
                predicates.join(" OR ")
            ),
        )?;
        for (prefix, source) in [
            ("orders-", "current_orders"),
            ("catalog-", "instrument_catalog"),
        ] {
            connection.execute(&format!("UPDATE {table} SET context_kind='account_operation',source=?1 WHERE context_kind='unknown' AND substr(execution_id,1,{})=?2 AND length(substr(execution_id,{}))=32 AND substr(execution_id,{}) NOT GLOB '*[^0-9a-f]*'",prefix.len(),prefix.len()+1,prefix.len()+1),params![source,prefix]).map_err(db)?;
        }
    }
    Ok(())
}

pub(super) fn connection_history(connection: &Connection) -> Result<()> {
    sql(connection,"ALTER TABLE account_connection_state ADD COLUMN last_success_at TEXT; ALTER TABLE account_connection_state ADD COLUMN last_failure JSON; ALTER TABLE account_connection_state ADD COLUMN manually_disconnected BOOLEAN DEFAULT 0 NOT NULL")?;
    let rows = query_rows(connection, "SELECT account_id,status,observed_at,message,label,error,presentation FROM account_connection_state", [], |row| {
            let mut failure = Map::new();
            for key in ["observed_at", "message", "label", "error", "presentation"] {
                failure.insert(key.into(), value(row, key)?);
            }
            Ok((
                row.get::<_, i64>("account_id")?,
                row.get::<_, String>("status")?,
                failure,
            ))
        })?;
    for (id, status, mut failure) in rows {
        if matches!(status.as_str(), "error" | "cooldown")
            && failure.get("observed_at").is_some_and(truthy)
        {
            for key in ["error", "presentation"] {
                let decoded = decode(&failure[key])?;
                failure.insert(key.into(), decoded);
            }
            connection
                .execute(
                    "UPDATE account_connection_state SET last_failure=?1 WHERE account_id=?2",
                    params![encoded(&Value::Object(failure))?, id],
                )
                .map_err(db)?;
        }
    }
    sql(
        connection,
        "UPDATE account_connection_state SET manually_disconnected=1 WHERE status='disconnected'",
    )
}

fn priority(value: &Value) -> Option<&'static str> {
    match value.as_str() {
        Some("昨今") => Some("yesterday_first"),
        Some("今昨") => Some("today_first"),
        _ => None,
    }
}

fn product(symbol: &str) -> Option<String> {
    let symbol = symbol.rsplit('.').next()?;
    let letters = symbol.bytes().take_while(u8::is_ascii_alphabetic).count();
    let suffix = &symbol[letters..];
    (letters > 0 && (3..=4).contains(&suffix.len()) && suffix.bytes().all(|c| c.is_ascii_digit()))
        .then(|| symbol[..letters].to_ascii_uppercase())
}

fn remove_algorithm_priority(
    execution: &mut Value,
    field: &str,
    id: i64,
) -> Result<Option<String>> {
    let Some(algorithm) = execution.get_mut(field).filter(|value| truthy(value)) else {
        return Ok(None);
    };
    let params = algorithm
        .as_object_mut()
        .ok_or_else(|| error(format!("账户 {id}: 旧算法设置无效")))?
        .get_mut("params");
    let Some(params) = params.filter(|value| truthy(value)) else {
        return Ok(None);
    };
    let previous = params
        .as_object_mut()
        .ok_or_else(|| error(format!("账户 {id}: 旧算法参数无效")))?
        .remove("offset_priority");
    let Some(previous) = previous.filter(|value| !value.is_null()) else {
        return Ok(None);
    };
    priority(&previous)
        .map(|value| Some(value.into()))
        .ok_or_else(|| error(format!("账户 {id}: 旧平仓顺序无效，请先处理")))
}

fn default_policy() -> Value {
    json!({"mode":"rules","fallback":"yesterday_first","overrides":{}})
}

fn migrate_execution(raw: &Value, id: i64, channel: &str) -> Result<Value> {
    let mut result = raw.clone();
    if !result.is_object() {
        return Err(error(format!("账户 {id}: 执行设置无效")));
    }
    if !matches!(channel, "ctp" | "tq") {
        result
            .as_object_mut()
            .unwrap()
            .entry("futures_close_policy")
            .or_insert_with(default_policy);
        return Ok(result);
    }
    let mut previous = Vec::new();
    for field in ["algorithm", "empty_positions_algorithm"] {
        if let Some(value) = remove_algorithm_priority(&mut result, field, id)? {
            previous.push(value);
        }
    }
    if previous.iter().collect::<HashSet<_>>().len() > 1 {
        return Err(error(format!("账户 {id}: 主交易和清仓顺序冲突，请先统一")));
    }
    let fallback = previous
        .first()
        .map(String::as_str)
        .unwrap_or("yesterday_first");
    let mut overrides = Map::new();
    if fallback == "today_first" {
        for product in products::PRODUCTS {
            overrides.insert((*product).into(), json!(fallback));
        }
    }
    let mut explicit = BTreeMap::new();
    if let Some(rules) = result.get_mut("trade_rules").filter(|value| truthy(value)) {
        let rules = rules
            .as_object_mut()
            .ok_or_else(|| error(format!("账户 {id}: 旧合约规则无效")))?;
        for (symbol, rule) in rules {
            let Some(rule) = rule.as_object_mut() else {
                continue;
            };
            let Some(old) = rule
                .remove("offset_priority")
                .filter(|value| !value.is_null())
            else {
                continue;
            };
            let (Some(product), Some(priority)) = (product(symbol), priority(&old)) else {
                return Err(error(format!("账户 {id}: 合约平仓规则无法迁移，请先处理")));
            };
            if explicit
                .get(&product)
                .is_some_and(|previous| *previous != priority)
            {
                return Err(error(format!(
                    "账户 {id}: {product} 不同月份的平仓规则冲突，请先统一"
                )));
            }
            explicit.insert(product, priority);
        }
    }
    for (product, priority) in explicit {
        overrides.insert(product, json!(priority));
    }
    result
        .as_object_mut()
        .unwrap()
        .entry("futures_close_policy")
        .or_insert_with(|| json!({"mode":"rules","fallback":fallback,"overrides":overrides}));
    Ok(result)
}

pub(super) fn futures_upgrade(connection: &Connection) -> Result<()> {
    let rows = query_rows(connection, "SELECT s.account_id,s.execution,a.trade_channel FROM account_settings s JOIN account a ON a.id=s.account_id", [], |row| {
            Ok((
                row.get::<_, i64>(0)?,
                value(row, "execution")?,
                row.get::<_, String>(2)?,
            ))
        })?;
    let mut pending = Vec::new();
    for (id, raw, channel) in rows {
        let previous = decode(&raw)?;
        let next = migrate_execution(&previous, id, &channel)?;
        pending.push((id, previous, next));
    }
    sql(connection, include_str!("0029.sql"))?;
    for (id, previous, next) in pending {
        connection.execute("INSERT INTO _futures_close_policy_backup(account_id,previous_execution,migrated_execution) VALUES(?1,?2,?3)",params![id,encoded(&previous)?,encoded(&next)?]).map_err(db)?;
        connection
            .execute(
                "UPDATE account_settings SET execution=?1 WHERE account_id=?2",
                params![encoded(&next)?, id],
            )
            .map_err(db)?;
    }
    Ok(())
}

pub(super) fn futures_downgrade(connection: &Connection) -> Result<()> {
    let rows = query_rows(
        connection,
        "SELECT account_id,previous_execution,migrated_execution FROM _futures_close_policy_backup",
        [],
        |row| {
            Ok((
                row.get::<_, i64>(0)?,
                value(row, "previous_execution")?,
                value(row, "migrated_execution")?,
            ))
        },
    )?;
    let mut archived = BTreeMap::new();
    for (id, before, after) in rows {
        archived.insert(id, (decode(&before)?, decode(&after)?));
    }
    let rows = query_rows(
        connection,
        "SELECT account_id,execution FROM account_settings",
        [],
        |row| Ok((row.get::<_, i64>(0)?, value(row, "execution")?)),
    )?;
    let mut pending = Vec::new();
    for (id, raw) in rows {
        let execution = decode(&raw)?;
        let previous = if let Some((before, _after)) = archived
            .get(&id)
            .filter(|(_, after)| same_json(&execution, after))
        {
            before.clone()
        } else if !archived.contains_key(&id)
            && execution
                .get("futures_close_policy")
                .is_some_and(|value| same_json(value, &default_policy()))
        {
            let mut previous = execution.clone();
            previous
                .as_object_mut()
                .unwrap()
                .remove("futures_close_policy");
            previous
        } else {
            return Err(error(format!(
                "账户 {id}: 升级后执行设置已修改，不能无损降级，请使用备份恢复"
            )));
        };
        pending.push((id, previous));
    }
    for (id, previous) in pending {
        connection
            .execute(
                "UPDATE account_settings SET execution=?1 WHERE account_id=?2",
                params![encoded(&previous)?, id],
            )
            .map_err(db)?;
    }
    sql(connection, "DROP TABLE _futures_close_policy_backup")
}

fn symbol_kind(value: &Value) -> &'static str {
    if !value.is_object() {
        return "error";
    }
    if matches!(
        value.get("status").and_then(Value::as_str),
        Some("SUCCEEDED" | "NOOP")
    ) {
        return "open";
    }
    if value.get("status") == Some(&json!("BLOCKED"))
        && value.get("reason_code") == Some(&json!("COMMON.SESSION.CLOSED"))
        && !value.get("orders").is_some_and(truthy)
        && !value.get("trades").is_some_and(truthy)
    {
        return "closed";
    }
    "error"
}

fn is_partial_session(raw: &Value) -> bool {
    if !raw.is_object()
        || raw.get("status") != Some(&json!("PARTIAL"))
        || raw.get("reason_code") != Some(&json!("COMMON.SESSION.CLOSED"))
    {
        return false;
    }
    if !matches!(raw.get("task_status"), None | Some(Value::Null))
        && !matches!(
            raw.get("task_status").and_then(Value::as_str),
            Some("FAILED" | "PARTIAL")
        )
    {
        return false;
    }
    if raw.get("termination").is_some_and(truthy) || raw.get("success") == Some(&Value::Bool(true))
    {
        return false;
    }
    let Some(symbols) = raw
        .get("symbol_results")
        .and_then(Value::as_object)
        .filter(|value| !value.is_empty())
    else {
        return false;
    };
    symbols.values().map(symbol_kind).collect::<HashSet<_>>() == HashSet::from(["closed", "open"])
}

pub(super) fn partial_session(connection: &Connection, upgrade: bool) -> Result<()> {
    let query = if upgrade {
        "SELECT i.execution_id,r.id,r.raw_result FROM execution_intent i JOIN executerecord r ON r.execution_id=i.execution_id AND r.account_id=i.account_id WHERE i.status='FAILED' AND r.is_success=0 AND (i.error IS NULL OR i.error='') AND i.cancel_requested_at IS NULL AND i.cancel_reason IS NULL AND i.terminate_mode IS NULL"
    } else {
        "SELECT execution_id,id,raw_result FROM executerecord"
    };
    let rows = query_rows(connection, query, [], |row| {
        Ok((
            row.get::<_, Option<String>>(0)?,
            row.get::<_, i64>(1)?,
            value(row, "raw_result")?,
        ))
    })?;
    for (execution, id, raw) in rows {
        let Some(mut raw) = document(&raw) else {
            continue;
        };
        if upgrade {
            if !is_partial_session(&raw) {
                continue;
            }
            raw["task_status"] = json!("PARTIAL");
            if !raw.get("outcome_reason").is_some_and(truthy) {
                raw["outcome_reason"] = raw.get("error").cloned().unwrap_or_default();
            }
            connection
                .execute(
                    "UPDATE execution_intent SET status='PARTIAL' WHERE execution_id=?1",
                    [execution],
                )
                .map_err(db)?;
        } else {
            if raw.get("task_status") != Some(&json!("PARTIAL")) {
                continue;
            }
            raw["task_status"] = json!("FAILED");
        }
        connection
            .execute(
                "UPDATE executerecord SET raw_result=?1 WHERE id=?2",
                params![encoded(&raw)?, id],
            )
            .map_err(db)?;
    }
    if !upgrade {
        sql(
            connection,
            "UPDATE execution_intent SET status='FAILED' WHERE status='PARTIAL'",
        )?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn close_policy_preserves_global_choice_and_rejects_contract_conflicts() {
        let old = json!({"algorithm":{"params":{"offset_priority":"今昨","max_wait_seconds":60}},"trade_rules":{"ag2611":{"offset_priority":"昨今"}}});
        let migrated = migrate_execution(&old, 7, "ctp").unwrap();
        assert_eq!(
            migrated["futures_close_policy"]["overrides"]["AG"],
            "yesterday_first"
        );
        assert_eq!(
            migrated["futures_close_policy"]["overrides"]["RB"],
            "today_first"
        );
        assert!(migrated["algorithm"]["params"]
            .get("offset_priority")
            .is_none());
        let conflict = json!({"trade_rules":{"ag2611":{"offset_priority":"昨今"},"ag2612":{"offset_priority":"今昨"}}});
        assert!(migrate_execution(&conflict, 7, "ctp")
            .unwrap_err()
            .error
            .contains("不同月份"));
        assert_eq!(
            migrate_execution(&old, 7, "external").unwrap()["algorithm"],
            old["algorithm"]
        );
    }

    #[test]
    fn partial_session_requires_open_and_closed_without_failure_evidence() {
        let mut raw = json!({"status":"PARTIAL","task_status":"FAILED","reason_code":"COMMON.SESSION.CLOSED","success":false,"symbol_results":{"a":{"status":"NOOP"},"b":{"status":"BLOCKED","reason_code":"COMMON.SESSION.CLOSED","orders":[],"trades":[]}}});
        assert!(is_partial_session(&raw));
        raw["termination"] = json!({"mode":"kill"});
        assert!(!is_partial_session(&raw));
    }
}
