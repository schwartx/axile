//! 账户配置与关联状态按业务操作提交，内部 helper 复用调用方连接。

use std::path::Path;

use rusqlite::{
    params, params_from_iter, types::Value as SqlValue, Connection, OptionalExtension, Row,
};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

use crate::{error::AnalysisError, runtime_sync, writer_scope};

const IDENTITY: &[&str] = &[
    "id",
    "name",
    "market",
    "trade_channel",
    "brokerage",
    "remark",
    "portfolio_id",
    "copied_from_account_id",
    "copied_from_account_name",
    "updated_at",
    "created_at",
];
const EXECUTION: &[&str] = &[
    "account_control_preset",
    "account_control_override",
    "weight_precision",
    "long_leverage",
    "short_leverage",
    "algorithm",
    "empty_positions_algorithm",
    "trade_rules",
    "futures_close_policy",
    "forbidden_symbols",
    "risk_symbols",
    "write_empty_record",
    "execution_timeout",
];
const SCHEDULE: &[&str] = &["is_started", "cron_expr", "supplement"];
const NOTIFICATION: &[&str] = &["feishu_key", "execution_notification_code"];
const BACKTEST: &[&str] = &["backtest_weight_type", "backtest_fee_rate"];
const GROUPS: &[(&str, &[&str])] = &[
    ("execution", EXECUTION),
    ("schedule", SCHEDULE),
    ("notification", NOTIFICATION),
    ("backtest", BACKTEST),
];
const ACCOUNT_SELECT: &str = "SELECT a.id,a.name,a.market,a.trade_channel,a.brokerage,a.remark,
    a.portfolio_id,a.copied_from_account_id,a.copied_from_account_name,a.updated_at,a.created_at,
    s.connection,s.execution,s.schedule,s.notification,s.backtest";
const ACCOUNT_FROM: &str = " FROM account a LEFT JOIN account_settings s ON s.account_id=a.id";
const STATE_SELECT: &str = ",
    CASE WHEN n.account_id IS NOT NULL THEN json_object(
        'account_id',n.account_id,'last_success_at',n.last_success_at,'execution_id',n.execution_id,
        'last_attempt_event_type',n.last_attempt_event_type,'last_attempt_at',n.last_attempt_at,
        'last_attempt_execution_id',n.last_attempt_execution_id,'last_attempt_ok',n.last_attempt_ok,
        'last_attempt_error',n.last_attempt_error) END,
    CASE WHEN c.account_id IS NOT NULL THEN json_object(
        'account_id',c.account_id,'config_key',c.config_key,'instance_id',c.instance_id,
        'sequence',c.sequence,'status',c.status,'observed_at',c.observed_at,'message',c.message,
        'error',json(c.error),'label',c.label,'presentation',json(c.presentation),
        'last_success_at',c.last_success_at,'last_failure',json(c.last_failure),
        'manually_disconnected',c.manually_disconnected) END,
    CASE WHEN r.account_id IS NOT NULL THEN json_object(
        'id',r.id,'account_id',r.account_id,'revision',r.revision,'status',r.status,
        'reset_worker',r.reset_worker,'create_request_key',r.create_request_key,'attempts',r.attempts,
        'last_error',r.last_error,'requested_at',r.requested_at,'last_attempt_at',r.last_attempt_at,
        'synchronized_at',r.synchronized_at) END,
    EXISTS(SELECT 1 FROM supplement_group g WHERE g.account_id=a.id AND g.status IN ('active','cancelling'))";
const STATE_FROM: &str = " LEFT JOIN account_notification_state n ON n.account_id=a.id
    LEFT JOIN account_connection_state c ON c.account_id=a.id
    LEFT JOIN accountruntimesync r ON r.account_id=a.id";

fn sql(error: rusqlite::Error) -> AnalysisError {
    AnalysisError::new("ACCOUNT_SQL", error.to_string())
}

fn invalid(message: impl Into<String>) -> AnalysisError {
    AnalysisError::new("ACCOUNT_INPUT", message)
}

fn id(request: &Value) -> Result<i64, AnalysisError> {
    request
        .get("account_id")
        .and_then(Value::as_i64)
        .ok_or_else(|| invalid("缺少账户 ID"))
}

fn now(request: &Value) -> Result<&str, AnalysisError> {
    request
        .get("now")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid("缺少操作时间"))
}

fn value(row: &Row<'_>, index: usize) -> Result<Value, AnalysisError> {
    use rusqlite::types::ValueRef;
    Ok(match row.get_ref(index).map_err(sql)? {
        ValueRef::Null => Value::Null,
        ValueRef::Integer(value) => json!(value),
        ValueRef::Real(value) => json!(value),
        ValueRef::Text(value) => {
            json!(std::str::from_utf8(value)
                .map_err(|error| AnalysisError::caused_by("ACCOUNT_DATA", error))?)
        }
        ValueRef::Blob(_) => {
            return Err(AnalysisError::new(
                "ACCOUNT_DATA",
                "账户配置包含非法二进制字段",
            ))
        }
    })
}

fn object(text: &str) -> Result<Map<String, Value>, AnalysisError> {
    serde_json::from_str::<Value>(text)
        .map_err(|error| AnalysisError::caused_by("ACCOUNT_DATA", error))?
        .as_object()
        .cloned()
        .ok_or_else(|| AnalysisError::new("ACCOUNT_DATA", "账户配置必须是对象"))
}

fn account_row(row: &Row<'_>, states: bool) -> Result<Value, AnalysisError> {
    let mut data = Map::new();
    for (index, name) in IDENTITY.iter().enumerate() {
        data.insert((*name).into(), value(row, index)?);
    }
    let connection: String = row.get(IDENTITY.len()).map_err(sql)?;
    data.insert("account_config".into(), Value::Object(object(&connection)?));
    for offset in 1..=GROUPS.len() {
        let group: String = row.get(IDENTITY.len() + offset).map_err(sql)?;
        data.extend(object(&group)?);
    }
    if states {
        for (offset, name, boolean) in [
            (0, "notification_state", "last_attempt_ok"),
            (1, "connection_state", "manually_disconnected"),
            (2, "runtime_sync", "reset_worker"),
        ] {
            let raw: Option<String> = row.get(IDENTITY.len() + 5 + offset).map_err(sql)?;
            let mut state = raw
                .map(|text| object(&text).map(Value::Object))
                .transpose()?
                .unwrap_or(Value::Null);
            if let Some(number) = state.get(boolean).and_then(Value::as_i64) {
                state[boolean] = json!(number != 0);
            }
            data.insert(name.into(), state);
        }
        data.insert(
            "has_pending_supplement".into(),
            json!(row.get::<_, bool>(IDENTITY.len() + 8).map_err(sql)?),
        );
    }
    Ok(Value::Object(data))
}

fn select_accounts(
    connection: &Connection,
    account_id: Option<i64>,
    states: bool,
) -> Result<Vec<Value>, AnalysisError> {
    let query = format!(
        "{ACCOUNT_SELECT}{}{ACCOUNT_FROM}{} WHERE (?1 IS NULL OR a.id=?1) ORDER BY a.id",
        if states { STATE_SELECT } else { "" },
        if states { STATE_FROM } else { "" }
    );
    let mut statement = connection.prepare(&query).map_err(sql)?;
    let mut rows = statement.query([account_id]).map_err(sql)?;
    let mut result = Vec::new();
    while let Some(row) = rows.next().map_err(sql)? {
        result.push(account_row(row, states)?);
    }
    Ok(result)
}

pub(crate) fn snapshot_on(
    connection: &Connection,
    account_id: i64,
) -> Result<Option<Value>, AnalysisError> {
    Ok(select_accounts(connection, Some(account_id), false)?.pop())
}

pub(crate) fn load_on(
    connection: &Connection,
    account_id: i64,
) -> Result<Option<Value>, AnalysisError> {
    Ok(select_accounts(connection, Some(account_id), true)?.pop())
}

pub(crate) fn list_on(connection: &Connection) -> Result<Vec<Value>, AnalysisError> {
    select_accounts(connection, None, true)
}

fn require_on(connection: &Connection, account_id: i64) -> Result<Value, AnalysisError> {
    load_on(connection, account_id)?
        .ok_or_else(|| AnalysisError::new("ACCOUNT_NOT_FOUND", "账户不存在"))
}

fn check_revision(account: &Value, request: &Value) -> Result<(), AnalysisError> {
    if let Some(expected) = request.get("expected_revision") {
        if expected
            != account
                .pointer("/runtime_sync/revision")
                .unwrap_or(&Value::Null)
        {
            return Err(AnalysisError::new(
                "ACCOUNT_CONFLICT",
                "账户配置已更新，请刷新后重试",
            ));
        }
    }
    Ok(())
}

pub(crate) fn latest_portfolio_on(
    connection: &Connection,
    account_id: i64,
) -> Result<Option<i64>, AnalysisError> {
    connection.query_row("SELECT portfolio_id FROM portfolioaccount WHERE account_id=?1 ORDER BY id DESC LIMIT 1",
        [account_id], |row| row.get(0)).optional().map(|value| value.flatten()).map_err(sql)
}

pub(crate) fn latest_account_on(
    connection: &Connection,
    portfolio_id: i64,
) -> Result<Option<i64>, AnalysisError> {
    connection.query_row("SELECT b.account_id FROM portfolioaccount b JOIN
        (SELECT account_id,max(id) AS id FROM portfolioaccount GROUP BY account_id) latest ON latest.id=b.id
        WHERE b.portfolio_id=?1 ORDER BY b.id DESC LIMIT 1", [portfolio_id], |row| row.get(0)).optional().map_err(sql)
}

fn ensure_portfolio_on(
    connection: &Connection,
    portfolio_id: Option<i64>,
) -> Result<(), AnalysisError> {
    if let Some(portfolio_id) = portfolio_id {
        let exists: bool = connection
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM portfolio WHERE id=?1)",
                [portfolio_id],
                |row| row.get(0),
            )
            .map_err(sql)?;
        if !exists {
            return Err(AnalysisError::new("PORTFOLIO_NOT_FOUND", "组合不存在"));
        }
    }
    Ok(())
}

pub(crate) fn bind_on(
    connection: &Connection,
    account_id: i64,
    portfolio_id: Option<i64>,
    now: &str,
) -> Result<(), AnalysisError> {
    ensure_portfolio_on(connection, portfolio_id)?;
    connection
        .execute(
            "INSERT INTO portfolioaccount(account_id,portfolio_id,created_at) VALUES(?1,?2,?3)",
            params![account_id, portfolio_id, now],
        )
        .map_err(sql)?;
    Ok(())
}

fn argument(value: &Value) -> SqlValue {
    match value {
        Value::Null => SqlValue::Null,
        Value::Bool(value) => SqlValue::Integer(i64::from(*value)),
        Value::Number(value) => value
            .as_i64()
            .map(SqlValue::Integer)
            .unwrap_or_else(|| SqlValue::Real(value.as_f64().unwrap_or_default())),
        Value::String(value) => SqlValue::Text(value.clone()),
        _ => SqlValue::Text(value.to_string()),
    }
}

fn settings(data: &Value, fields: &[&str]) -> Value {
    Value::Object(
        fields
            .iter()
            .map(|name| {
                (
                    (*name).into(),
                    data.get(*name).cloned().unwrap_or(Value::Null),
                )
            })
            .collect(),
    )
}

fn insert_on(connection: &Connection, data: &Value, now: &str) -> Result<i64, AnalysisError> {
    crate::control_policy::parse_override(data["account_control_override"].clone())
        .map_err(|error| AnalysisError::caused_by("ACCOUNT_INPUT", error))?;
    ensure_portfolio_on(connection, data["portfolio_id"].as_i64())?;
    let identity = [
        "name",
        "market",
        "trade_channel",
        "brokerage",
        "remark",
        "portfolio_id",
        "copied_from_account_id",
        "copied_from_account_name",
    ];
    let mut arguments = identity
        .iter()
        .map(|name| argument(&data[*name]))
        .collect::<Vec<_>>();
    arguments.push(SqlValue::Text(now.into()));
    connection.execute("INSERT INTO account(name,market,trade_channel,brokerage,remark,portfolio_id,copied_from_account_id,copied_from_account_name,created_at,updated_at)
        VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?9)", params_from_iter(arguments)).map_err(sql)?;
    let account_id = connection.last_insert_rowid();
    let mut arguments = vec![
        SqlValue::Integer(account_id),
        argument(&data["account_config"]),
    ];
    arguments.extend(
        GROUPS
            .iter()
            .map(|(_, fields)| argument(&settings(data, fields))),
    );
    connection.execute("INSERT INTO account_settings(account_id,connection,execution,schedule,notification,backtest) VALUES(?1,?2,?3,?4,?5,?6)", params_from_iter(arguments)).map_err(sql)?;
    bind_on(connection, account_id, data["portfolio_id"].as_i64(), now)?;
    Ok(account_id)
}

pub fn account_get(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let account_id = id(request)?;
    crate::with_read_snapshot(Path::new(path), |connection| {
        Ok(load_on(connection, account_id)?.unwrap_or(Value::Null))
    })
}

fn public_account(mut account: Value, request: &Value) -> Result<Value, AnalysisError> {
    account["account_control_override"] =
        crate::control_policy::public_override(&account["account_control_override"])
            .map_err(|error| AnalysisError::caused_by("ACCOUNT_DATA", error))?;
    let channel = account["trade_channel"]
        .as_str()
        .ok_or_else(|| invalid("交易渠道不能为空"))?;
    let fields = request
        .get("channel_fields")
        .and_then(|channels| channels.get(channel))
        .and_then(Value::as_array)
        .ok_or_else(|| invalid(format!("未注册的交易渠道: {channel}")))?;
    let config = account["account_config"]
        .as_object()
        .ok_or_else(|| invalid("account_config 必须是对象"))?;
    let connection_values: Map<String, Value> = fields
        .iter()
        .filter_map(Value::as_str)
        .filter_map(|name| config.get(name).map(|value| (name.into(), value.clone())))
        .collect();
    let configured = !config.is_empty();
    let feishu_configured = account["feishu_key"]
        .as_str()
        .is_some_and(|value| !value.is_empty());
    let code = account["execution_notification_code"]
        .as_str()
        .unwrap_or_default()
        .trim();
    let notification_status = if code.is_empty() {
        "none"
    } else {
        let hash = format!("{:x}", Sha256::digest(code.as_bytes()));
        if request["default_notification_hash"].as_str() == Some(hash.as_str()) {
            "default"
        } else {
            "function"
        }
    };
    let connection =
        crate::connection_state::public_connection(&account, &request["live_sessions"], false)?;
    let data = account.as_object_mut().unwrap();
    for name in ["account_config", "feishu_key", "connection_state"] {
        data.remove(name);
    }
    if let Some(state) = data
        .get_mut("notification_state")
        .and_then(Value::as_object_mut)
    {
        state.remove("account_id");
    }
    data.insert("connection_values".into(), Value::Object(connection_values));
    data.insert("account_configured".into(), json!(configured));
    data.insert("feishu_configured".into(), json!(feishu_configured));
    data.insert(
        "execution_notification_status".into(),
        json!(notification_status),
    );
    data.insert("connection".into(), connection);
    data.insert("runtime_sync".into(), Value::Null);
    Ok(account)
}

fn json_bytes(value: &Value) -> Result<Vec<u8>, AnalysisError> {
    serde_json::to_vec(value).map_err(|error| invalid(error.to_string()))
}

pub fn account_public(_path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let mut account = public_account(request["account"].clone(), request)?;
    account["runtime_sync"] = request["runtime_sync"].clone();
    Ok(account)
}

pub fn account_list_json(path: &str, request: &Value) -> Result<Vec<u8>, AnalysisError> {
    crate::with_read_snapshot(Path::new(path), |connection| {
        let accounts = list_on(connection)?
            .into_iter()
            .map(|account| public_account(account, request))
            .collect::<Result<Vec<_>, _>>()?;
        json_bytes(&json!({"data": accounts}))
    })
}

pub fn account_info_json(path: &str, request: &Value) -> Result<Vec<u8>, AnalysisError> {
    let account_id = id(request)?;
    crate::with_read_snapshot(Path::new(path), |connection| {
        json_bytes(&public_account(
            require_on(connection, account_id)?,
            request,
        )?)
    })
}

pub fn account_connection_json(path: &str, request: &Value) -> Result<Vec<u8>, AnalysisError> {
    let account_id = id(request)?;
    crate::with_read_snapshot(Path::new(path), |connection| {
        let account = require_on(connection, account_id)?;
        json_bytes(&crate::connection_state::public_connection(
            &account,
            &request["live_sessions"],
            true,
        )?)
    })
}

pub fn account_portfolio_records_json(
    path: &str,
    request: &Value,
) -> Result<Vec<u8>, AnalysisError> {
    json_bytes(&account_portfolio_records(path, request)?)
}

pub fn account_create_replay(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let key = request
        .get("idempotency_key")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid("缺少创建请求键"))?;
    crate::with_read_snapshot(Path::new(path), |connection| {
        let account_id: Option<i64> = connection
            .query_row(
                "SELECT account_id FROM accountruntimesync WHERE create_request_key=?1",
                [key],
                |row| row.get(0),
            )
            .optional()
            .map_err(sql)?;
        account_id
            .map(|account_id| require_on(connection, account_id))
            .transpose()
            .map(|account| account.unwrap_or(Value::Null))
    })
}

pub fn account_create(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let now = now(request)?;
    let connection = writer_scope::open_writer(Path::new(path))?;
    writer_scope::immediate(&connection, || {
        let key = request
            .get("idempotency_key")
            .and_then(Value::as_str)
            .filter(|key| !key.is_empty());
        if let Some(key) = key {
            let existing: Option<i64> = connection
                .query_row(
                    "SELECT account_id FROM accountruntimesync WHERE create_request_key=?1",
                    [key],
                    |row| row.get(0),
                )
                .optional()
                .map_err(sql)?;
            if let Some(account_id) = existing {
                return Ok(
                    json!({"account": require_on(&connection, account_id)?, "replayed": true}),
                );
            }
        }
        let data = request
            .get("account")
            .filter(|data| data.is_object())
            .ok_or_else(|| invalid("缺少账户配置"))?;
        let account_id = insert_on(&connection, data, now)?;
        runtime_sync::enqueue_on(&connection, account_id, false, key, now)?;
        Ok(json!({"account": require_on(&connection, account_id)?, "replayed": false}))
    })
}

pub fn account_copy(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let source_id = id(request)?;
    let name = request
        .get("name")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|name| !name.is_empty())
        .ok_or_else(|| invalid("新账户名称不能为空"))?;
    let now = now(request)?;
    let connection = writer_scope::open_writer(Path::new(path))?;
    writer_scope::immediate(&connection, || {
        let mut source = require_on(&connection, source_id)?;
        check_revision(&source, request)?;
        if source["is_started"] == true {
            return Err(AnalysisError::new(
                "ACCOUNT_CONFLICT",
                "请先暂停来源账户的自动调度",
            ));
        }
        source["copied_from_account_id"] = json!(source_id);
        source["copied_from_account_name"] = source["name"].clone();
        source["name"] = json!(name);
        source["is_started"] = json!(false);
        if source["execution_notification_code"]
            .as_str()
            .is_some_and(|code| code.trim().is_empty())
        {
            source["execution_notification_code"] = Value::Null;
        }
        if let Some(config) = request.get("account_config") {
            if !config.is_object() {
                return Err(invalid("account_config 必须是对象"));
            }
            source["account_config"] = config.clone();
        }
        let account_id = insert_on(&connection, &source, now)?;
        runtime_sync::enqueue_on(&connection, account_id, false, None, now)?;
        Ok(require_on(&connection, account_id)?)
    })
}

fn patch_on(
    connection: &Connection,
    account_id: i64,
    data: &Map<String, Value>,
    now: &str,
) -> Result<(), AnalysisError> {
    let mut columns = vec!["updated_at=?1".to_owned()];
    let mut arguments = vec![SqlValue::Text(now.into())];
    for name in IDENTITY.iter().filter(|name| {
        !matches!(
            **name,
            "id" | "created_at"
                | "updated_at"
                | "copied_from_account_id"
                | "copied_from_account_name"
        )
    }) {
        if let Some(value) = data.get(*name) {
            arguments.push(argument(value));
            columns.push(format!("{name}=?{}", arguments.len()));
        }
    }
    arguments.push(SqlValue::Integer(account_id));
    connection
        .execute(
            &format!(
                "UPDATE account SET {} WHERE id=?{}",
                columns.join(","),
                arguments.len()
            ),
            params_from_iter(arguments),
        )
        .map_err(sql)?;
    if let Some(config) = data.get("account_config") {
        if !config.is_object() {
            return Err(invalid("account_config 必须是对象"));
        }
        connection
            .execute(
                "UPDATE account_settings SET connection=?1 WHERE account_id=?2",
                params![config.to_string(), account_id],
            )
            .map_err(sql)?;
    }
    for (group, fields) in GROUPS {
        let changes: Map<String, Value> = fields
            .iter()
            .filter_map(|name| data.get(*name).map(|value| ((*name).into(), value.clone())))
            .collect();
        if changes.is_empty() {
            continue;
        }
        let raw: String = connection
            .query_row(
                &format!("SELECT {group} FROM account_settings WHERE account_id=?1"),
                [account_id],
                |row| row.get(0),
            )
            .map_err(sql)?;
        let mut values = object(&raw)?;
        values.extend(changes);
        connection
            .execute(
                &format!("UPDATE account_settings SET {group}=?1 WHERE account_id=?2"),
                params![Value::Object(values).to_string(), account_id],
            )
            .map_err(sql)?;
    }
    Ok(())
}

pub fn account_update(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let account_id = id(request)?;
    let now = now(request)?;
    let data = request
        .get("patch")
        .and_then(Value::as_object)
        .ok_or_else(|| invalid("缺少账户更新配置"))?;
    let connection = writer_scope::open_writer(Path::new(path))?;
    writer_scope::immediate(&connection, || {
        let previous = require_on(&connection, account_id)?;
        check_revision(&previous, request)?;
        let (override_, code) = match data.get("account_control_override") {
            Some(value) => (value, "ACCOUNT_INPUT"),
            None => (&previous["account_control_override"], "ACCOUNT_DATA"),
        };
        crate::control_policy::parse_override(override_.clone())
            .map_err(|error| AnalysisError::caused_by(code, error))?;
        let runtime_changed = ["account_config", "trade_channel"].iter().any(|name| {
            data.get(*name)
                .is_some_and(|value| value != &previous[*name])
        });
        let schedule_changed = ["cron_expr", "supplement", "trade_channel", "portfolio_id"]
            .iter()
            .any(|name| {
                data.get(*name)
                    .is_some_and(|value| value != &previous[*name])
            });
        if (!previous["supplement"].is_null() || previous["has_pending_supplement"] == true)
            && (schedule_changed || data.get("is_started") == Some(&json!(false)))
        {
            let reason = if data.get("is_started") == Some(&json!(false)) {
                "account_stopped"
            } else {
                "configuration_changed"
            };
            crate::supplements::cancel_groups_in_connection(
                &connection,
                account_id,
                reason,
                None,
                now,
            )?;
        }
        if let Some(portfolio_id) = data.get("portfolio_id") {
            if portfolio_id != &previous["portfolio_id"] {
                bind_on(&connection, account_id, portfolio_id.as_i64(), now)?;
            }
        }
        patch_on(&connection, account_id, data, now)?;
        if runtime_changed {
            let channel = data
                .get("trade_channel")
                .unwrap_or(&previous["trade_channel"])
                .as_str()
                .ok_or_else(|| invalid("交易渠道不能为空"))?;
            let config = data
                .get("account_config")
                .unwrap_or(&previous["account_config"]);
            let key = crate::connection_state::config_key(channel, config)?;
            crate::connection_state::reset_connection_for_config(&connection, account_id, &key)?;
        }
        runtime_sync::enqueue_on(&connection, account_id, runtime_changed, None, now)?;
        Ok(
            json!({"account": require_on(&connection, account_id)?, "runtime_changed": runtime_changed}),
        )
    })
}

pub fn account_delete(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let account_id = id(request)?;
    let connection = crate::storage_connection::open_writer(Path::new(path), true)?;
    writer_scope::immediate(&connection, || {
        require_on(&connection, account_id)?;
        // 先将独立摘要与执行记录解绑，再由外键级联清理账户拥有的全部状态。
        crate::execution_ops::delete_account_executions(&connection, account_id)?;
        connection
            .execute("DELETE FROM account WHERE id=?1", [account_id])
            .map_err(sql)?;
        Ok(Value::Null)
    })
}

pub fn account_dashboard_context(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let logic = request
        .get("logic_version")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid("缺少分析逻辑版本"))?;
    let engine = request
        .get("engine_version")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid("缺少分析引擎版本"))?;
    crate::with_read_snapshot(Path::new(path), |connection| {
        let mut accounts = list_on(connection)?;
        for account in &mut accounts {
            account["connection"] = crate::connection_state::public_connection(
                account,
                &request["live_sessions"],
                false,
            )?;
        }
        let ids = accounts
            .iter()
            .filter_map(|account| account["id"].as_i64())
            .collect::<Vec<_>>();
        let inputs = crate::dashboard::dashboard_inputs(connection, &ids, logic, engine)
            .map_err(AnalysisError::from)?;
        Ok(json!({"accounts": accounts, "inputs": inputs}))
    })
}

pub fn account_next_run_context(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let account_id = id(request)?;
    crate::with_read_snapshot(Path::new(path), |connection| {
        let account = require_on(connection, account_id)?;
        let mut statement = connection.prepare("SELECT s.scheduled_at FROM supplement_step s JOIN supplement_group g ON g.id=s.group_id
            WHERE g.account_id=?1 AND g.status='active' AND s.status='pending' ORDER BY s.scheduled_at").map_err(sql)?;
        let times = statement
            .query_map([account_id], |row| row.get::<_, String>(0))
            .map_err(sql)?
            .collect::<rusqlite::Result<Vec<_>>>()
            .map_err(sql)?;
        Ok(json!({"account": account, "pending_scheduled_at": times}))
    })
}

pub fn account_portfolio_records(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let account_id = id(request)?;
    let skip = request
        .get("skip")
        .and_then(Value::as_i64)
        .unwrap_or(0)
        .max(0);
    let limit = request
        .get("limit")
        .and_then(Value::as_i64)
        .unwrap_or(100)
        .clamp(1, 500);
    crate::with_read_snapshot(Path::new(path), |connection| {
        require_on(connection, account_id)?;
        let count: i64 = connection
            .query_row(
                "SELECT count(*) FROM portfolioaccount WHERE account_id=?1",
                [account_id],
                |row| row.get(0),
            )
            .map_err(sql)?;
        let mut statement = connection.prepare("SELECT account_id,portfolio_id,created_at FROM portfolioaccount WHERE account_id=?1 ORDER BY id LIMIT ?2 OFFSET ?3").map_err(sql)?;
        let rows = statement.query_map(params![account_id,limit,skip], |row| Ok(json!({"account_id":row.get::<_,i64>(0)?,"portfolio_id":row.get::<_,Option<i64>>(1)?,"created_at":row.get::<_,String>(2)?}))).map_err(sql)?.collect::<rusqlite::Result<Vec<_>>>().map_err(sql)?;
        Ok(json!({"data": rows, "count": count}))
    })
}

pub fn account_latest_portfolio(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let account_id = id(request)?;
    crate::with_read_snapshot(Path::new(path), |connection| {
        Ok(json!(latest_portfolio_on(connection, account_id)?))
    })
}

pub fn account_update_backtest(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let account_id = id(request)?;
    let now = now(request)?;
    let patch = request
        .get("patch")
        .and_then(Value::as_object)
        .ok_or_else(|| invalid("缺少回测设置"))?;
    if patch.keys().any(|name| !BACKTEST.contains(&name.as_str())) {
        return Err(invalid("回测设置包含未知字段"));
    }
    let connection = writer_scope::open_writer(Path::new(path))?;
    writer_scope::immediate(&connection, || {
        require_on(&connection, account_id)?;
        patch_on(&connection, account_id, patch, now)?;
        connection.query_row(
            "SELECT json_extract(backtest,'$.backtest_fee_rate') FROM account_settings WHERE account_id=?1",
            [account_id],
            |row| Ok(json!({"backtest_weight_type":"cs","backtest_fee_rate":row.get::<_, f64>(0)?})),
        ).map_err(sql)
    })
}

pub fn account_stop_for_clear(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let account_id = id(request)?;
    let now = now(request)?;
    let connection = writer_scope::open_writer(Path::new(path))?;
    writer_scope::immediate(&connection, || {
        require_on(&connection, account_id)?;
        crate::supplements::cancel_groups_in_connection(
            &connection,
            account_id,
            "account_stopped",
            None,
            now,
        )?;
        patch_on(
            &connection,
            account_id,
            json!({"is_started": false}).as_object().unwrap(),
            now,
        )?;
        snapshot_on(&connection, account_id)?
            .ok_or_else(|| AnalysisError::new("ACCOUNT_NOT_FOUND", "账户不存在"))
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn database() -> Connection {
        let connection = Connection::open_in_memory().unwrap();
        connection.execute_batch("PRAGMA foreign_keys=ON;
            CREATE TABLE portfolio(id INTEGER PRIMARY KEY);
            CREATE TABLE account(id INTEGER PRIMARY KEY,name TEXT NOT NULL,market TEXT NOT NULL,
                trade_channel TEXT NOT NULL,brokerage TEXT NOT NULL,remark TEXT,portfolio_id INTEGER REFERENCES portfolio(id),
                copied_from_account_id INTEGER,copied_from_account_name TEXT,updated_at TEXT NOT NULL,created_at TEXT NOT NULL);
            CREATE TABLE account_settings(account_id INTEGER PRIMARY KEY REFERENCES account(id) ON DELETE CASCADE,
                connection TEXT NOT NULL,execution TEXT NOT NULL,schedule TEXT NOT NULL,notification TEXT NOT NULL,backtest TEXT NOT NULL);
            CREATE TABLE portfolioaccount(id INTEGER PRIMARY KEY,account_id INTEGER,portfolio_id INTEGER,created_at TEXT);
            CREATE TABLE accountruntimesync(id INTEGER PRIMARY KEY,account_id INTEGER UNIQUE,revision INTEGER,
                status TEXT,reset_worker INTEGER,create_request_key TEXT UNIQUE,attempts INTEGER,last_error TEXT,
                requested_at TEXT,last_attempt_at TEXT,synchronized_at TEXT);
            CREATE TABLE account_notification_state(account_id INTEGER PRIMARY KEY,last_success_at TEXT,execution_id TEXT,
                last_attempt_event_type TEXT,last_attempt_at TEXT,last_attempt_execution_id TEXT,last_attempt_ok INTEGER,last_attempt_error TEXT);
            CREATE TABLE account_connection_state(account_id INTEGER PRIMARY KEY,config_key TEXT,instance_id TEXT,
                sequence INTEGER,status TEXT,observed_at TEXT,message TEXT,error TEXT,label TEXT,presentation TEXT,
                last_success_at TEXT,last_failure TEXT,manually_disconnected INTEGER);
            CREATE TABLE supplement_group(id TEXT PRIMARY KEY,account_id INTEGER,status TEXT);
            INSERT INTO portfolio VALUES(1);").unwrap();
        connection
    }

    fn account() -> Value {
        json!({"name":"账户","market":"期货","trade_channel":"ctp","brokerage":"test","remark":null,
            "portfolio_id":1,"account_config":{"password":"secret"},"account_control_preset":"default",
            "account_control_override":null,"weight_precision":0.01,"long_leverage":null,"short_leverage":null,
            "algorithm":{"method":"SINGLE-MAKER","params":{}},"empty_positions_algorithm":null,"trade_rules":null,
            "futures_close_policy":{"mode":"rules","fallback":"yesterday_first","overrides":{}},
            "forbidden_symbols":null,"risk_symbols":null,"write_empty_record":null,"execution_timeout":120,
            "is_started":false,"cron_expr":"","supplement":null,"feishu_key":"webhook",
            "execution_notification_code":null,"backtest_weight_type":"ts","backtest_fee_rate":0})
    }

    #[test]
    fn create_returns_complete_settings_binding_and_sync_from_one_transaction() {
        let connection = database();
        let saved = writer_scope::immediate(&connection, || {
            let id = insert_on(&connection, &account(), "2026-10-23T09:00:00")?;
            runtime_sync::enqueue_on(
                &connection,
                id,
                false,
                Some("request-1"),
                "2026-10-23T09:00:00",
            )?;
            require_on(&connection, id)
        })
        .unwrap();
        assert_eq!(saved["account_config"]["password"], "secret");
        assert!(saved["trade_rules"].is_null());
        assert_eq!(saved["runtime_sync"]["revision"], 1);
        assert_eq!(saved["runtime_sync"]["reset_worker"], false);
        assert_eq!(
            latest_portfolio_on(&connection, saved["id"].as_i64().unwrap()).unwrap(),
            Some(1)
        );
        assert_eq!(list_on(&connection).unwrap().len(), 1);
    }

    #[test]
    fn runtime_registration_failure_rolls_back_account_settings_and_binding() {
        let connection = database();
        connection
            .execute_batch(
                "CREATE TRIGGER reject_sync BEFORE INSERT ON accountruntimesync
            BEGIN SELECT RAISE(ABORT,'sync failed'); END;",
            )
            .unwrap();
        let result = writer_scope::immediate(&connection, || {
            let id = insert_on(&connection, &account(), "2026-10-23T09:00:00")?;
            runtime_sync::enqueue_on(&connection, id, false, None, "2026-10-23T09:00:00")
        });
        assert!(result.is_err());
        for table in [
            "account",
            "account_settings",
            "portfolioaccount",
            "accountruntimesync",
        ] {
            assert_eq!(
                connection
                    .query_row(&format!("SELECT count(*) FROM {table}"), [], |row| row
                        .get::<_, i64>(0))
                    .unwrap(),
                0
            );
        }
    }

    #[test]
    fn patch_replaces_override_preserves_other_group_fields_and_unbinding_order() {
        let connection = database();
        let id = insert_on(&connection, &account(), "2026-10-23T09:00:00").unwrap();
        let patch =
            json!({"account_control_override":{"timezone":"UTC"},"risk_symbols":["rb2610"]});
        patch_on(
            &connection,
            id,
            patch.as_object().unwrap(),
            "2026-10-23T09:01:00",
        )
        .unwrap();
        let saved = snapshot_on(&connection, id).unwrap().unwrap();
        assert_eq!(saved["algorithm"], account()["algorithm"]);
        assert_eq!(
            saved["account_control_override"],
            patch["account_control_override"]
        );
        assert_eq!(saved["risk_symbols"], patch["risk_symbols"]);
        bind_on(&connection, id, None, "2026-10-23T09:02:00").unwrap();
        assert_eq!(latest_portfolio_on(&connection, id).unwrap(), None);
        assert_eq!(latest_account_on(&connection, 1).unwrap(), None);
        let bad = json!({"portfolio_id":999,"name":"bad"});
        let mut input = account();
        input
            .as_object_mut()
            .unwrap()
            .extend(bad.as_object().unwrap().clone());
        assert_eq!(
            insert_on(&connection, &input, "2026-10-23T09:03:00")
                .unwrap_err()
                .code,
            "PORTFOLIO_NOT_FOUND"
        );
    }
}
