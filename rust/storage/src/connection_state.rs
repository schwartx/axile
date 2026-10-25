//! 主服务连接历史；worker 观察仍通过内存和 IPC 传入。

use std::collections::BTreeSet;
use std::path::Path;

use rusqlite::{params, Connection, OptionalExtension};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use crate::error::AnalysisError;
use crate::writer_scope::{immediate, open_writer};

fn invalid(message: impl Into<String>) -> AnalysisError {
    AnalysisError::new("INVALID_REQUEST", message)
}

fn database_error(error: rusqlite::Error) -> AnalysisError {
    AnalysisError::new("CONNECTION_STATE", error.to_string())
}

fn text<'a>(value: &'a Value, key: &str) -> Result<&'a str, AnalysisError> {
    value
        .get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| invalid(format!("{key} must be a string")))
}

fn account_id(value: &Value) -> Result<i64, AnalysisError> {
    value
        .get("account_id")
        .and_then(Value::as_i64)
        .filter(|id| *id > 0)
        .ok_or_else(|| invalid("account_id must be a positive integer"))
}

fn optional_text<'a>(value: &'a Value, key: &str) -> Result<Option<&'a str>, AnalysisError> {
    match value.get(key) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(value)) => Ok(Some(value)),
        _ => Err(invalid(format!("{key} must be a string or null"))),
    }
}

fn optional_object(value: &Value, key: &str) -> Result<Option<String>, AnalysisError> {
    match value.get(key) {
        None | Some(Value::Null) => Ok(None),
        Some(value @ Value::Object(_)) => serde_json::to_string(value)
            .map(Some)
            .map_err(|error| invalid(error.to_string())),
        _ => Err(invalid(format!("{key} must be an object or null"))),
    }
}

fn sorted(value: &Value) -> Value {
    match value {
        Value::Object(values) => Value::Object(
            values
                .keys()
                .collect::<BTreeSet<_>>()
                .into_iter()
                .map(|key| (key.clone(), sorted(&values[key])))
                .collect(),
        ),
        Value::Array(values) => Value::Array(values.iter().map(sorted).collect()),
        _ => value.clone(),
    }
}

/// 与 Python json.dumps(sort_keys=True, ensure_ascii=False) 的签名保持兼容。
pub fn config_key(channel: &str, config: &Value) -> Result<String, AnalysisError> {
    let compact = crate::business_detail::wire(&sorted(&json!([channel, config])))
        .map_err(AnalysisError::from)?;
    let mut wire = String::with_capacity(compact.len());
    let (mut quoted, mut escaped) = (false, false);
    for character in compact.chars() {
        wire.push(character);
        if escaped {
            escaped = false;
        } else if quoted && character == '\\' {
            escaped = true;
        } else if character == '"' {
            quoted = !quoted;
        } else if !quoted && matches!(character, ',' | ':') {
            wire.push(' ');
        }
    }
    Ok(format!("{:x}", Sha256::digest(wire.as_bytes())))
}

#[derive(Deserialize, Serialize)]
struct PresentationFact {
    label: String,
    value: String,
}

#[derive(Deserialize, Serialize)]
struct PresentationSection {
    title: Option<String>,
    text: Option<String>,
    #[serde(default)]
    fields: Vec<PresentationFact>,
    #[serde(default)]
    steps: Vec<String>,
}

#[derive(Deserialize, Serialize)]
struct PresentationAction {
    label: String,
    intent: String,
}

#[derive(Deserialize, Serialize)]
struct Presentation {
    title: Option<String>,
    summary: Option<String>,
    #[serde(default)]
    sections: Vec<PresentationSection>,
    #[serde(default)]
    actions: Vec<PresentationAction>,
}

fn presentation_snapshot(value: &Value) -> Value {
    let Ok(presentation) = serde_json::from_value::<Presentation>(value.clone()) else {
        return Value::Null;
    };
    let content = presentation
        .title
        .as_ref()
        .is_some_and(|text| !text.trim().is_empty())
        || presentation
            .summary
            .as_ref()
            .is_some_and(|text| !text.trim().is_empty())
        || presentation.sections.iter().any(|section| {
            section.text.as_ref().is_some_and(|text| !text.is_empty())
                || !section.fields.is_empty()
                || !section.steps.is_empty()
        })
        || presentation
            .actions
            .iter()
            .any(|action| !action.label.trim().is_empty() && !action.intent.trim().is_empty());
    if content {
        serde_json::to_value(presentation).expect("presentation fields are JSON values")
    } else {
        Value::Null
    }
}

fn public_failure(value: &Value) -> Result<Value, AnalysisError> {
    match value {
        Value::Null => return Ok(Value::Null),
        Value::Object(values) if values.is_empty() => return Ok(Value::Null),
        Value::Object(_) => (),
        _ => return Err(invalid("last_failure must be an object or null")),
    }
    let error = value.get("error").unwrap_or(&Value::Null);
    if !error.is_null() && !error.is_object() {
        return Err(invalid("last_failure.error must be an object or null"));
    }
    Ok(json!({
        "observed_at": text(value,"observed_at")?,
        "message": optional_text(value,"message")?,
        "label": optional_text(value,"label")?,
        "error": error,
        "presentation": presentation_snapshot(&value["presentation"]),
    }))
}

/// 合并本进程会话快照与配置匹配的历史，不把历史当作连接存活证据。
pub fn public_connection(
    account: &Value,
    live_sessions: &Value,
    detail: bool,
) -> Result<Value, AnalysisError> {
    let id = account
        .get("id")
        .and_then(Value::as_i64)
        .ok_or_else(|| invalid("account id is required"))?;
    let key = config_key(text(account, "trade_channel")?, &account["account_config"])?;
    let mut current = json!({
        "progress":null,"status":"uninitialized","observed_at":null,"message":null,"label":null,
        "worker_alive":false,"sampled_at":null,"last_success_at":null,"last_failure_at":null,
        "manually_disconnected":false,"retry_after_seconds":0,
    });
    if let Some(live) = live_sessions
        .get(id.to_string())
        .filter(|value| value["config_key"].as_str() == Some(key.as_str()))
    {
        for (name, value) in current.as_object_mut().unwrap() {
            if let Some(saved) = live["connection"].get(name.as_str()) {
                *value = saved.clone();
            }
        }
    }
    let mut failure = Value::Null;
    if let Some(state) = account
        .get("connection_state")
        .filter(|value| value["config_key"].as_str() == Some(key.as_str()))
    {
        current["last_success_at"] = state["last_success_at"].clone();
        current["manually_disconnected"] =
            json!(state["manually_disconnected"].as_bool().unwrap_or(false));
        failure = public_failure(&state["last_failure"])?;
        if !failure.is_null() {
            current["last_failure_at"] = failure["observed_at"].clone();
        }
        if current["manually_disconnected"] == true
            && matches!(
                current["status"].as_str(),
                Some("uninitialized" | "unknown" | "disconnected")
            )
        {
            current["status"] = json!("disconnected");
            current["message"] = json!("账户会话已主动断开，需要显式重新连接");
        }
    }
    if detail {
        let active = current["status"] == "unavailable";
        current["error"] = if active {
            failure["error"].clone()
        } else {
            Value::Null
        };
        current["presentation"] = if active {
            failure["presentation"].clone()
        } else {
            Value::Null
        };
        current["last_failure"] = failure;
    }
    Ok(current)
}

fn saved_key(connection: &Connection, account_id: i64) -> Result<Option<String>, AnalysisError> {
    let row = connection
        .query_row(
            "SELECT a.trade_channel, s.connection FROM account a
             JOIN account_settings s ON s.account_id=a.id WHERE a.id=?1",
            [account_id],
            |row| Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?)),
        )
        .optional()
        .map_err(database_error)?;
    row.map(|(channel, raw)| {
        let config = serde_json::from_str(&raw)
            .map_err(|error| AnalysisError::new("CONNECTION_STATE", error.to_string()))?;
        config_key(&channel, &config)
    })
    .transpose()
}

/// 账户配置保存事务复用此函数；相同配置保留历史和主动断开意愿。
pub fn reset_connection_for_config(
    connection: &Connection,
    account_id: i64,
    config_key: &str,
) -> Result<(), AnalysisError> {
    connection
        .execute(
            "INSERT INTO account_connection_state
             (account_id,config_key,instance_id,sequence,status,observed_at,message,error,
              label,presentation,last_success_at,last_failure,manually_disconnected)
             VALUES (?1,?2,'',0,'unknown',NULL,NULL,NULL,NULL,NULL,NULL,NULL,0)
             ON CONFLICT(account_id) DO UPDATE SET
              config_key=excluded.config_key,instance_id='',sequence=0,status='unknown',
              observed_at=NULL,message=NULL,error=NULL,label=NULL,presentation=NULL,
              last_success_at=NULL,last_failure=NULL,manually_disconnected=0
             WHERE account_connection_state.config_key<>excluded.config_key",
            params![account_id, config_key],
        )
        .map_err(database_error)?;
    Ok(())
}

pub fn connection_begin(database: &str, request: &Value) -> Result<Value, AnalysisError> {
    let account_id = account_id(request)?;
    let key = text(request, "config_key")?;
    let instance = text(request, "instance_id")?;
    if instance.is_empty() {
        return Err(invalid("instance_id must not be empty"));
    }
    let connection = open_writer(Path::new(database))?;
    immediate(&connection, || {
        if saved_key(&connection, account_id)?.as_deref() != Some(key) {
            return Ok(Value::Null);
        }
        reset_connection_for_config(&connection, account_id, key)?;
        connection
            .execute(
                "UPDATE account_connection_state SET instance_id=?2, sequence=0 WHERE account_id=?1",
                params![account_id, instance],
            )
            .map_err(database_error)?;
        let manually_disconnected: bool = connection
            .query_row(
                "SELECT manually_disconnected FROM account_connection_state WHERE account_id=?1",
                [account_id],
                |row| row.get(0),
            )
            .map_err(database_error)?;
        Ok(json!({
            "account_id": account_id, "config_key": key, "instance_id": instance,
            "manually_disconnected": manually_disconnected,
        }))
    })
}

fn observe_on(connection: &Connection, request: &Value) -> Result<Value, AnalysisError> {
    let binding = request
        .get("binding")
        .ok_or_else(|| invalid("binding is required"))?;
    let account_id = account_id(binding)?;
    let key = text(binding, "config_key")?;
    let instance = text(binding, "instance_id")?;
    let sequence = request
        .get("sequence")
        .and_then(Value::as_i64)
        .filter(|sequence| *sequence > 0)
        .ok_or_else(|| invalid("sequence must be a positive integer"))?;
    let observation = request
        .get("observation")
        .filter(|value| value.is_object())
        .ok_or_else(|| invalid("observation must be an object"))?;
    let status = text(observation, "status")?;
    let observed_at = text(observation, "observed_at")?;
    let kind = optional_text(observation, "kind")?.unwrap_or("native");
    let message = optional_text(observation, "message")?;
    let label = optional_text(observation, "label")?;
    let error = optional_object(observation, "error")?;
    let presentation = optional_object(observation, "presentation")?;
    let history = request.get("history").filter(|value| !value.is_null());
    if history.is_some_and(|value| !value.is_object()) {
        return Err(invalid("history must be an object or null"));
    }
    let last_success = history
        .map(|value| optional_text(value, "last_success_at"))
        .transpose()?
        .flatten()
        .or_else(|| (kind == "access" && status == "ok").then_some(observed_at));
    let failure = matches!(status, "error" | "cooldown").then(|| {
        json!({
            "observed_at": observed_at, "message": message, "label": label,
            "error": observation["error"], "presentation": observation["presentation"],
        })
        .to_string()
    });
    let failure = history
        .map(|value| optional_object(value, "last_failure"))
        .transpose()?
        .flatten()
        .or(failure);
    let manually_disconnected = match (kind, status) {
        ("session", "disconnected") => Some(true),
        ("session", "ok" | "connecting" | "recovering") => Some(false),
        _ => None,
    };
    if instance.is_empty() || saved_key(connection, account_id)?.as_deref() != Some(key) {
        return Ok(json!({"applied": false}));
    }
    let changed = connection
        .execute(
            "UPDATE account_connection_state SET sequence=?4,status=?5,observed_at=?6,
             message=?7,error=?8,label=?9,presentation=?10,
             last_success_at=COALESCE(?11,last_success_at),last_failure=COALESCE(?12,last_failure),
             manually_disconnected=COALESCE(?13,manually_disconnected)
             WHERE account_id=?1 AND config_key=?2 AND instance_id=?3 AND sequence<?4",
            params![
                account_id,
                key,
                instance,
                sequence,
                status,
                observed_at,
                message,
                error,
                label,
                presentation,
                last_success,
                failure,
                manually_disconnected,
            ],
        )
        .map_err(database_error)?;
    Ok(json!({"applied": changed != 0}))
}

pub fn connection_observe(database: &str, request: &Value) -> Result<Value, AnalysisError> {
    let connection = open_writer(Path::new(database))?;
    immediate(&connection, || observe_on(&connection, request))
}

pub fn connection_end(database: &str, request: &Value) -> Result<Value, AnalysisError> {
    let binding = request
        .get("binding")
        .ok_or_else(|| invalid("binding is required"))?;
    let account_id = account_id(binding)?;
    let key = text(binding, "config_key")?;
    let instance = text(binding, "instance_id")?;
    let failure = optional_object(request, "last_failure")?;
    let connection = open_writer(Path::new(database))?;
    immediate(&connection, || {
        if instance.is_empty() || saved_key(&connection, account_id)?.as_deref() != Some(key) {
            return Ok(json!({"applied": false}));
        }
        let changed = connection
            .execute(
                "UPDATE account_connection_state SET instance_id='',last_failure=COALESCE(?4,last_failure)
                 WHERE account_id=?1 AND config_key=?2 AND instance_id=?3",
                params![account_id, key, instance, failure],
            )
            .map_err(database_error)?;
        Ok(json!({"applied": changed != 0}))
    })
}

pub fn connection_recover(database: &str, _request: &Value) -> Result<Value, AnalysisError> {
    let connection = open_writer(Path::new(database))?;
    immediate(&connection, || {
        let changed = connection
            .execute(
                "UPDATE account_connection_state SET instance_id='',sequence=0",
                [],
            )
            .map_err(database_error)?;
        Ok(json!({"updated": changed}))
    })
}

pub fn connection_accounts(database: &str, request: &Value) -> Result<Value, AnalysisError> {
    let ids = match request.get("account_ids") {
        None | Some(Value::Null) => None,
        Some(Value::Array(ids)) => Some(
            ids.iter()
                .map(|id| {
                    id.as_i64()
                        .filter(|id| *id > 0)
                        .ok_or_else(|| invalid("invalid account id"))
                })
                .collect::<Result<BTreeSet<_>, _>>()?,
        ),
        _ => return Err(invalid("account_ids must be an array or null")),
    };
    crate::with_read_snapshot(Path::new(database), |connection| {
        let mut accounts = match ids {
            None => crate::accounts::list_on(connection)?,
            Some(ids) => ids
                .into_iter()
                .filter_map(|id| crate::accounts::load_on(connection, id).transpose())
                .collect::<Result<Vec<_>, _>>()?,
        };
        for account in &mut accounts {
            account["connection"] = public_connection(account, &request["live_sessions"], true)?;
        }
        Ok(Value::Array(accounts))
    })
}

#[cfg(test)]
mod tests {
    use std::sync::atomic::{AtomicU64, Ordering};

    use super::*;

    static NEXT_DATABASE: AtomicU64 = AtomicU64::new(0);

    fn database(test: impl FnOnce(&Connection, &str, &str)) {
        let path = std::env::temp_dir().join(format!(
            "axile-connection-test-{}-{}.db",
            std::process::id(),
            NEXT_DATABASE.fetch_add(1, Ordering::Relaxed)
        ));
        let connection = Connection::open(&path).unwrap();
        connection
            .execute_batch(
                "PRAGMA foreign_keys=ON;
                 CREATE TABLE account(id INTEGER PRIMARY KEY,trade_channel TEXT NOT NULL);
                 CREATE TABLE account_settings(account_id INTEGER PRIMARY KEY REFERENCES account(id) ON DELETE CASCADE,
                    connection JSON NOT NULL);
                 CREATE TABLE account_connection_state(
                    account_id INTEGER PRIMARY KEY REFERENCES account(id) ON DELETE CASCADE,
                    config_key TEXT NOT NULL,instance_id TEXT NOT NULL,sequence INTEGER NOT NULL,
                    status TEXT NOT NULL,observed_at TEXT,message TEXT,error JSON,label TEXT,presentation JSON,
                    last_success_at TEXT,last_failure JSON,manually_disconnected BOOLEAN NOT NULL);
                 INSERT INTO account VALUES(1,'ctp');
                 INSERT INTO account_settings VALUES(1,'{}');",
            )
            .unwrap();
        test(
            &connection,
            path.to_str().unwrap(),
            &config_key("ctp", &json!({})).unwrap(),
        );
        drop(connection);
        std::fs::remove_file(path).unwrap();
    }

    fn begin(path: &str, key: &str, instance: &str) -> Value {
        connection_begin(
            path,
            &json!({"account_id":1,"config_key":key,"instance_id":instance}),
        )
        .unwrap()
    }

    fn observe(path: &str, binding: &Value, sequence: i64, observation: Value) -> Value {
        connection_observe(
            path,
            &json!({"binding":binding,"sequence":sequence,"observation":observation}),
        )
        .unwrap()
    }

    fn history(connection: &Connection) -> (String, i64, Option<String>, Option<String>, bool) {
        connection
            .query_row(
                "SELECT instance_id,sequence,last_success_at,last_failure,manually_disconnected
                 FROM account_connection_state WHERE account_id=1",
                [],
                |row| {
                    Ok((
                        row.get(0)?,
                        row.get(1)?,
                        row.get(2)?,
                        row.get(3)?,
                        row.get(4)?,
                    ))
                },
            )
            .unwrap()
    }

    #[test]
    fn config_hash_preserves_python_unicode_sorting_and_float_spelling() {
        assert_eq!(
            config_key("ctp", &json!({})).unwrap(),
            "8ce8d306e3390a68bdb01b98301340bbecd0dfb1f4ba1bf8c72ce4cf39271708"
        );
        assert_eq!(
            config_key(
                "ctp",
                &json!({
                    "password":"秘密, : \\\"", "z":[1.0,1e-7,1e20,true,null],"a":{"b":2,"a":1}
                })
            )
            .unwrap(),
            "6878433645da18abe7f14e097f3067be994afa47bdbb05db25a7e167d435842d"
        );
        assert_eq!(
            config_key("ctp", &json!({"x":-0.0,"small":0.0001,"large":1e16})).unwrap(),
            "261943ab4b4356a7e237f31c53ecc0baca7a7f445a4127ab2db883f078d5f179"
        );
    }

    #[test]
    fn public_view_requires_a_matching_live_session_and_keeps_history_separate() {
        let key = config_key("ctp", &json!({"password":"secret"})).unwrap();
        let account = json!({
            "id":1,"trade_channel":"ctp","account_config":{"password":"secret"},
            "connection_state":{"config_key":key,"status":"ok","last_success_at":"access", "manually_disconnected":false,
                "last_failure":{"observed_at":"failure","message":"original","error":{"technical_detail":"evidence"},
                    "presentation":{"title":"original title","sections":[{"fields":[{"label":"fact","value":"value"}]}]}}}
        });
        let offline = public_connection(&account, &json!({}), true).unwrap();
        assert_eq!(offline["status"], "uninitialized");
        assert_eq!(offline["last_success_at"], "access");
        assert_eq!(offline["last_failure_at"], "failure");
        assert!(offline["error"].is_null());
        assert_eq!(
            offline["last_failure"]["presentation"]["summary"],
            Value::Null
        );
        assert_eq!(
            offline["last_failure"]["presentation"]["sections"][0]["steps"],
            json!([])
        );
        let live = json!({"1":{"config_key":key,"connection":{"status":"ready","worker_alive":true,"progress":{"run_id":"run"}}}});
        let ready = public_connection(&account, &live, false).unwrap();
        assert_eq!(ready["status"], "ready");
        assert_eq!(ready["worker_alive"], true);
        assert_eq!(ready["progress"]["run_id"], "run");
        assert!(ready.get("error").is_none() && ready.get("last_failure").is_none());
        let mut replaced = account.clone();
        replaced["account_config"] = json!({"password":"replacement"});
        let replaced = public_connection(&replaced, &live, true).unwrap();
        assert_eq!(replaced["status"], "uninitialized");
        assert!(replaced["last_failure"].is_null() && replaced["last_success_at"].is_null());
        assert!(!ready.to_string().contains("secret"));
    }

    #[test]
    fn public_view_keeps_manual_disconnect_and_drops_only_invalid_presentation() {
        let key = config_key("ctp", &json!({})).unwrap();
        let account = json!({
            "id":1,"trade_channel":"ctp","account_config":{},
            "connection_state":{"config_key":key,"last_success_at":"access","manually_disconnected":true,
                "last_failure":{"observed_at":"failure","label":"original label","error":{"technical_detail":"original"},
                    "presentation":{"title":123,"sections":"invalid"}}}
        });
        let disconnected = public_connection(&account, &json!({}), true).unwrap();
        assert_eq!(disconnected["status"], "disconnected");
        assert_eq!(disconnected["manually_disconnected"], true);
        assert_eq!(
            disconnected["message"],
            "账户会话已主动断开，需要显式重新连接"
        );
        for status in ["ready", "connecting", "recovering", "unavailable"] {
            let live = json!({"1":{"config_key":key,"connection":{"status":status,"worker_alive":true,"retry_after_seconds":20}}});
            let detail = public_connection(&account, &live, true).unwrap();
            assert_eq!(detail["status"], status);
            assert_eq!(detail["last_failure"]["label"], "original label");
            assert!(detail["presentation"].is_null());
            if status == "unavailable" {
                assert_eq!(detail["error"]["technical_detail"], "original");
                assert_eq!(detail["retry_after_seconds"], 20);
            } else {
                assert!(detail["error"].is_null());
            }
        }
        for value in [
            json!({}),
            json!({"title":" "}),
            json!({"sections":[{"title":"title only"}]}),
            json!({"actions":[{"label":" ","intent":"open"}]}),
            json!({"title":"valid","sections":null}),
        ] {
            assert!(presentation_snapshot(&value).is_null());
        }
        let normalized = presentation_snapshot(
            &json!({"summary":"reason","extra":"ignored","sections":[{"text":" ","extra":true}],"actions":[{"label":"open","intent":"settings","extra":true}]}),
        );
        assert_eq!(
            normalized,
            json!({"title":null,"summary":"reason","sections":[{"title":null,"text":" ","fields":[],"steps":[]}],"actions":[{"label":"open","intent":"settings"}]})
        );
    }

    #[test]
    fn instance_and_sequence_cas_reject_late_observations_and_end_callbacks() {
        database(|connection, path, key| {
            let old = begin(path, key, "old");
            let current = begin(path, key, "current");
            assert_eq!(
                observe(
                    path,
                    &current,
                    2,
                    json!({"status":"error","observed_at":"failure","message":"current"})
                )["applied"],
                true
            );
            assert_eq!(
                observe(path, &old, 99, json!({"status":"ok","observed_at":"late"}))["applied"],
                false
            );
            assert_eq!(
                observe(
                    path,
                    &current,
                    1,
                    json!({"status":"ok","observed_at":"earlier"})
                )["applied"],
                false
            );
            assert_eq!(
                connection_end(
                    path,
                    &json!({"binding":old,"last_failure":{"observed_at":"late"}})
                )
                .unwrap()["applied"],
                false
            );
            let state = history(connection);
            assert_eq!((&state.0, state.1), (&"current".to_owned(), 2));
            assert!(state.3.unwrap().contains("current"));
            assert_eq!(connection_end(path, &json!({"binding":current,"last_failure":{"observed_at":"ended","error":{"technical_detail":"original"}}})).unwrap()["applied"], true);
            assert_eq!(
                observe(
                    path,
                    &current,
                    3,
                    json!({"status":"ok","observed_at":"too-late"})
                )["applied"],
                false
            );
            assert!(history(connection).3.unwrap().contains("original"));
        });
    }

    #[test]
    fn restart_and_reconnect_preserve_history_and_manual_disconnect() {
        database(|connection, path, key| {
            let binding = begin(path, key, "first");
            observe(
                path,
                &binding,
                1,
                json!({"status":"error","observed_at":"failure","error":{"technical_detail":"evidence"}}),
            );
            observe(
                path,
                &binding,
                2,
                json!({"status":"ok","observed_at":"success","kind":"access"}),
            );
            observe(
                path,
                &binding,
                3,
                json!({"status":"disconnected","observed_at":"manual","kind":"session"}),
            );
            connection_recover(path, &json!({})).unwrap();
            let state = history(connection);
            assert_eq!(
                (&state.0, state.1, state.2.as_deref(), state.4),
                (&String::new(), 0, Some("success"), true)
            );
            assert!(state.3.unwrap().contains("evidence"));
            assert_eq!(
                observe(
                    path,
                    &binding,
                    99,
                    json!({"status":"ok","observed_at":"late","kind":"session"})
                )["applied"],
                false
            );
            let replacement = begin(path, key, "replacement");
            assert_eq!(replacement["manually_disconnected"], true);
            observe(
                path,
                &replacement,
                1,
                json!({"status":"connecting","observed_at":"explicit","kind":"session"}),
            );
            assert!(!history(connection).4);
            let error: Option<String> = connection
                .query_row("SELECT error FROM account_connection_state", [], |row| {
                    row.get(0)
                })
                .unwrap();
            assert!(error.is_none());
            assert!(history(connection).3.unwrap().contains("evidence"));
        });
    }

    #[test]
    fn failure_capture_keeps_nulls_json_evidence_and_history_priority() {
        database(|connection, path, key| {
            let binding = begin(path, key, "worker");
            assert_eq!(
                observe(
                    path,
                    &binding,
                    1,
                    json!({"status":"error","observed_at":"first"})
                )["applied"],
                true
            );
            assert_eq!(
                history(connection).3.as_deref(),
                Some(
                    r#"{"observed_at":"first","message":null,"label":null,"error":null,"presentation":null}"#
                )
            );
            let evidence = json!({"code":9007199254740993_u64,"detail":{"native":true}});
            let presentation = json!({"summary":"原始说明","sections":[]});
            observe(
                path,
                &binding,
                2,
                json!({"status":"cooldown","observed_at":"second",
                "message":"重试","label":"连接","error":evidence,"presentation":presentation,"extra":true}),
            );
            assert_eq!(
                serde_json::from_str::<Value>(&history(connection).3.unwrap()).unwrap(),
                json!({"observed_at":"second","message":"重试","label":"连接",
                    "error":evidence,"presentation":presentation})
            );
            let historical = json!({"observed_at":"prior","message":"历史故障","error":evidence});
            connection_observe(
                path,
                &json!({"binding":binding,"sequence":3,
                "observation":{"status":"error","observed_at":"current"},
                "history":{"last_failure":historical}}),
            )
            .unwrap();
            assert_eq!(
                serde_json::from_str::<Value>(&history(connection).3.unwrap()).unwrap(),
                historical
            );
            let error = connection_observe(
                path,
                &json!({"binding":binding,"sequence":4,
                "observation":{"status":"error","observed_at":"invalid","error":"not an object"}}),
            )
            .unwrap_err();
            assert_eq!(error.code, "INVALID_REQUEST");
            assert_eq!(history(connection).1, 3);
        });
    }

    #[test]
    fn intermediate_history_survives_a_successful_final_frame() {
        database(|connection, path, key| {
            let binding = begin(path, key, "worker");
            connection_observe(path, &json!({
                "binding":binding,"sequence":5,
                "observation":{"status":"ok","observed_at":"ready","kind":"session"},
                "history":{"last_success_at":"access","last_failure":{"observed_at":"failure","message":"intermediate"}}
            })).unwrap();
            let state = history(connection);
            assert_eq!(state.2.as_deref(), Some("access"));
            assert!(state.3.unwrap().contains("intermediate"));
        });
    }

    #[test]
    fn config_reset_joins_caller_transaction_and_deleted_accounts_stay_deleted() {
        database(|connection, path, key| {
            let binding = begin(path, key, "worker");
            observe(
                path,
                &binding,
                1,
                json!({"status":"error","observed_at":"failure"}),
            );
            let replacement = config_key("ctp", &json!({"password":"replacement"})).unwrap();
            connection.execute_batch("BEGIN IMMEDIATE").unwrap();
            connection
                .execute(
                    "UPDATE account_settings SET connection=?1",
                    [json!({"password":"replacement"}).to_string()],
                )
                .unwrap();
            reset_connection_for_config(connection, 1, &replacement).unwrap();
            assert!(history(connection).3.is_none());
            connection.execute_batch("ROLLBACK").unwrap();
            assert_eq!(history(connection).0, "worker");
            assert!(history(connection).3.is_some());
            connection
                .execute(
                    "UPDATE account_settings SET connection=?1",
                    [json!({"password":"replacement"}).to_string()],
                )
                .unwrap();
            assert!(begin(path, key, "stale").is_null());
            assert_eq!(
                observe(
                    path,
                    &binding,
                    99,
                    json!({"status":"ok","observed_at":"stale"})
                )["applied"],
                false
            );
            assert_eq!(
                connection_end(path, &json!({"binding":binding})).unwrap()["applied"],
                false
            );
            begin(path, &replacement, "new");
            assert!(history(connection).3.is_none());
            connection
                .execute("DELETE FROM account WHERE id=1", [])
                .unwrap();
            assert!(begin(path, &replacement, "deleted").is_null());
            assert_eq!(
                connection
                    .query_row("SELECT COUNT(*) FROM account_connection_state", [], |row| {
                        row.get::<_, i64>(0)
                    })
                    .unwrap(),
                0
            );
        });
    }
}
