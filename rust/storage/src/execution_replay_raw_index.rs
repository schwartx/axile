//! Direct raw identities, row locations and proof members. No business projections.

use crate::execution_replay::{failure, io_error, ReplayResult};
use rusqlite::{params, Connection};
use serde_json::{json, Value};

pub const LAYOUT: &str = "7";

pub fn context(db: &Connection, recording: &str, value: &Value) -> ReplayResult<()> {
    if value["content_refs"].is_null() {
        return Ok(());
    }
    let refs = value["content_refs"]
        .as_array()
        .filter(|r| r.len() <= 64)
        .ok_or_else(|| failure("SCHEMA", "raw content references"))?;
    for reference in refs {
        let target = reference["recording"]
            .as_str()
            .ok_or_else(|| failure("SCHEMA", "raw content target"))?;
        crate::execution_replay::identifier(target)?;
        let commit = reference["commit_id"]
            .as_str()
            .filter(|s| {
                s.len() == 64
                    && s.bytes()
                        .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
            })
            .ok_or_else(|| failure("SCHEMA", "raw content commit"))?;
        if target == recording {
            return Err(failure("SCHEMA", "raw self reference"));
        }
        db.execute(
            "INSERT OR IGNORE INTO content_refs VALUES (?1,?2,?3)",
            params![recording, target, commit],
        )
        .map_err(io_error)?;
    }
    Ok(())
}

pub fn finish_links(db: &Connection, recording: &str, after: u64) -> ReplayResult<()> {
    db.execute("INSERT OR IGNORE INTO object_blocks SELECT r.recording,r.source,b.stream,b.block,b.first_seq,b.last_seq FROM relations r JOIN object_blocks b ON b.recording=r.recording AND b.object=r.target JOIN objects o ON o.id=r.target WHERE r.recording=?1 AND r.seq>?2 AND r.role='related_capture_seq' AND b.first_seq<=o.first_seq",params![recording,after]).map_err(io_error)?;
    Ok(())
}

fn key(value: &Value) -> Option<String> {
    value.as_str().filter(|v| !v.is_empty()).map(str::to_owned)
}

fn object(
    db: &Connection,
    recording: &str,
    kind: &str,
    key: &str,
    sequence: Option<u64>,
) -> ReplayResult<i64> {
    if key.len() > 4096 {
        return Err(failure("BUDGET", "raw identity length"));
    }
    db.execute("INSERT INTO objects(recording,kind,key,first_seq) VALUES (?1,?2,?3,?4) ON CONFLICT(recording,kind,key) DO UPDATE SET first_seq=CASE WHEN objects.first_seq IS NULL THEN excluded.first_seq WHEN excluded.first_seq IS NULL THEN objects.first_seq ELSE min(objects.first_seq,excluded.first_seq) END",
        params![recording,kind,key,sequence]).map_err(io_error)?;
    db.query_row(
        "SELECT id FROM objects WHERE recording=?1 AND kind=?2 AND key=?3",
        params![recording, kind, key],
        |r| r.get(0),
    )
    .map_err(io_error)
}

fn reference(db: &Connection, recording: &str, id: i64, seq: u64, row: i64) -> ReplayResult<()> {
    db.execute(
        "INSERT OR IGNORE INTO object_refs VALUES (?1,?2,?3,?4)",
        params![recording, id, seq, row],
    )
    .map_err(io_error)?;
    Ok(())
}

fn relation(
    db: &Connection,
    recording: &str,
    source: i64,
    target: i64,
    seq: u64,
    role: &str,
) -> ReplayResult<()> {
    db.execute(
        "INSERT OR IGNORE INTO relations VALUES (?1,?2,?3,?4,?5)",
        params![recording, source, target, seq, role],
    )
    .map_err(io_error)?;
    Ok(())
}

fn scope(db: &Connection, recording: &str, kind: &str, data: &Value, seq: u64) -> ReplayResult<()> {
    let value = &data["scope"];
    let mut symbols = Vec::new();
    if value["kind"] == "account" || value.is_null() && data["symbol"].is_null() {
        symbols.push("");
    } else if let Some(items) = value.as_array().or_else(|| value["symbols"].as_array()) {
        for symbol in items.iter().filter_map(Value::as_str) {
            symbols.push(if symbol == "*" { "" } else { symbol });
        }
    } else if let Some(symbol) = data["symbol"].as_str() {
        symbols.push(symbol);
    }
    for symbol in symbols {
        db.execute(
            "INSERT OR IGNORE INTO scope_refs VALUES (?1,?2,?3,?4)",
            params![recording, kind, symbol, seq],
        )
        .map_err(io_error)?;
    }
    Ok(())
}

fn proof(db: &Connection, recording: &str, seq: u64, row: i64, basis: &Value) -> ReplayResult<()> {
    if basis.is_null() {
        return Ok(());
    }
    let members = basis["proof"]["included_trade_keys"].as_array();
    db.execute(
        "INSERT INTO proofs VALUES (?1,?2,?3,?4)",
        params![recording, seq, row, members.map_or(0, Vec::len)],
    )
    .map_err(io_error)?;
    if let Some(members) = members {
        for (position, member) in members.iter().enumerate() {
            if let Some(key) = member.as_str() {
                let id = object(db, recording, "trade", key, None)?;
                db.execute(
                    "INSERT OR IGNORE INTO proof_members VALUES (?1,?2,?3,?4,?5)",
                    params![recording, seq, row, id, position],
                )
                .map_err(io_error)?;
            }
        }
    }
    Ok(())
}

fn row(
    db: &Connection,
    recording: &str,
    topic: &str,
    data: &Value,
    seq: u64,
    position: i64,
) -> ReplayResult<()> {
    let symbol = data["symbol"].as_str().unwrap_or("");
    let order = key(&data["order_id"]).filter(|_| !symbol.is_empty());
    let order_id = order
        .as_ref()
        .map(|key| {
            object(
                db,
                recording,
                "order",
                &json!([symbol, key]).to_string(),
                Some(seq),
            )
        })
        .transpose()?;
    if let Some(order) = order_id {
        reference(db, recording, order, seq, position)?;
    }
    let execution_id = key(&data["execution_id"])
        .map(|key| object(db, recording, "execution", &key, None))
        .transpose()?;
    if position >= 0 {
        if let Some(execution) = key(&data["execution_id"]) {
            db.execute(
                "INSERT OR IGNORE INTO executions VALUES (?1,?2)",
                params![recording, execution],
            )
            .map_err(io_error)?;
        }
    }
    let selected = match topic {
        "/execution" => key(&data["execution_id"]).map(|key| ("execution", key)),
        "/command" => Some((
            "command",
            key(&data["command_id"]).unwrap_or_else(|| format!("observation:{seq}")),
        )),
        "/order" | "/order_set" => Some((
            "order",
            order
                .as_ref()
                .map(|key| json!([symbol, key]).to_string())
                .unwrap_or_else(|| {
                    json!([symbol, format!("observation:{seq}:{position}")]).to_string()
                }),
        )),
        "/account_observation" => Some((
            "position",
            json!([
                symbol,
                data["position_side"]
                    .as_str()
                    .map(str::to_ascii_lowercase)
                    .unwrap_or_else(|| format!("observation:{seq}:{position}"))
            ])
            .to_string(),
        )),
        "/trade" => Some(
            if data["identity_quality"] == "stable" && key(&data["trade_key"]).is_some() {
                ("trade", key(&data["trade_key"]).unwrap())
            } else {
                (
                    "weak_trade",
                    data["related_capture_seq"]
                        .as_u64()
                        .filter(|n| *n > 0 && *n < seq)
                        .unwrap_or(seq)
                        .to_string(),
                )
            },
        ),
        _ if topic.starts_with("/market_trade/") => Some(
            if data["identity_quality"] == "stable" && key(&data["trade_key"]).is_some() {
                ("market_trade", key(&data["trade_key"]).unwrap())
            } else {
                (
                    "weak_market_trade",
                    data["related_capture_seq"]
                        .as_u64()
                        .filter(|n| *n > 0 && *n < seq)
                        .unwrap_or(seq)
                        .to_string(),
                )
            },
        ),
        _ => None,
    };
    if let Some((kind, key)) = selected {
        let id = object(db, recording, kind, &key, Some(seq))?;
        reference(db, recording, id, seq, position)?;
        if matches!(kind, "command" | "trade" | "weak_trade") {
            if let Some(order) = data["order_id"].as_str().filter(|s| !s.is_empty()) {
                db.execute("INSERT INTO order_mentions VALUES (?1,?2,?3,?4,?5) ON CONFLICT(recording,kind,order_id,object) DO UPDATE SET first_seq=min(order_mentions.first_seq,excluded.first_seq)",params![recording,kind,order,id,seq]).map_err(io_error)?;
            }
        }
        if let Some(order) = order_id {
            relation(db, recording, id, order, seq, "order")?;
        }
        if let Some(execution) = execution_id {
            relation(db, recording, id, execution, seq, "execution")?;
        }
        if let Some(related) = data["related_capture_seq"]
            .as_u64()
            .filter(|related| kind.contains("trade") && *related > 0 && *related < seq)
        {
            let weak = object(
                db,
                recording,
                if kind.starts_with("market") {
                    "weak_market_trade"
                } else {
                    "weak_trade"
                },
                &related.to_string(),
                None,
            )?;
            relation(db, recording, id, weak, seq, "related_capture_seq")?;
            reference(db, recording, weak, seq, position)?;
            db.execute("INSERT OR IGNORE INTO object_blocks SELECT recording,?3,stream,block,first_seq,last_seq FROM object_blocks WHERE recording=?1 AND object=?2 AND first_seq<=?4", params![recording,weak,id,related]).map_err(io_error)?;
        }
        if topic == "/account_observation" {
            proof(
                db,
                recording,
                seq,
                position,
                data.get("position_basis").unwrap_or(&Value::Null),
            )?;
        }
    }
    Ok(())
}

pub fn observation(
    db: &Connection,
    recording: &str,
    value: &Value,
    stream: u16,
    block: u64,
) -> ReplayResult<()> {
    let seq = value["capture_seq"]
        .as_u64()
        .ok_or_else(|| failure("SCHEMA", "sequence required"))?;
    let topic = value["topic"]
        .as_str()
        .ok_or_else(|| failure("SCHEMA", "topic required"))?;
    let data = &value["data"];
    if let Some(generation) = data["sync_generation"]
        .as_u64()
        .filter(|_| matches!(topic, "/order" | "/order_set" | "/connection"))
    {
        db.execute(
            "INSERT INTO generations VALUES (?1,?2,?3)",
            params![recording, seq, generation],
        )
        .map_err(io_error)?;
    }
    if data.get("query_basis").is_some() || topic == "/order_set" {
        db.execute(
            "INSERT INTO query_refs VALUES (?1,?2)",
            params![recording, seq],
        )
        .map_err(io_error)?;
    }
    let mut symbols = Vec::new();
    if let Some(symbol) = data["symbol"].as_str() {
        symbols.push(symbol);
    }
    for name in ["symbols"] {
        if let Some(items) = data[name].as_array() {
            symbols.extend(items.iter().filter_map(Value::as_str));
        }
    }
    if let Some(items) = data["scope"]["symbols"].as_array() {
        symbols.extend(items.iter().filter_map(Value::as_str));
    }
    for name in ["orders", "positions"] {
        if let Some(items) = data[name].as_array() {
            symbols.extend(items.iter().filter_map(|v| v["symbol"].as_str()));
        }
    }
    for symbol in symbols.into_iter().filter(|s| !s.is_empty()) {
        db.prepare_cached("INSERT INTO symbols VALUES (?1,?2,?3) ON CONFLICT(recording,symbol) DO UPDATE SET first_seq=excluded.first_seq WHERE excluded.first_seq<symbols.first_seq").map_err(io_error)?.execute(params![recording,symbol,seq]).map_err(io_error)?;
    }
    if let Some(execution) = data["execution_id"].as_str().filter(|s| !s.is_empty()) {
        db.prepare_cached("INSERT OR IGNORE INTO executions VALUES (?1,?2)")
            .map_err(io_error)?
            .execute(params![recording, execution])
            .map_err(io_error)?;
    }
    if topic.starts_with("/market_trade/") {
        return Ok(());
    }
    if topic.starts_with("/quote/") {
        return Ok(());
    }
    match topic {
        "/order_set" | "/account_observation" => {
            scope(db, recording, topic, data, seq)?;
            if topic == "/account_observation" {
                proof(db, recording, seq, -1, &data["position_basis"])?;
            }
            let name = if topic == "/order_set" {
                "orders"
            } else {
                "positions"
            };
            if let Some(rows) = data[name].as_array() {
                for (position, item) in rows.iter().enumerate() {
                    row(db, recording, topic, item, seq, position as i64)?;
                }
            }
        }
        "/connection" | "/capture" => scope(db, recording, topic, data, seq)?,
        _ => row(db, recording, topic, data, seq, -1)?,
    }
    db.execute("INSERT INTO object_blocks SELECT r.recording,r.object,?3,?4,?2,?2 FROM object_refs r WHERE r.recording=?1 AND r.seq=?2 GROUP BY r.object ON CONFLICT(recording,object,stream,block) DO UPDATE SET first_seq=min(object_blocks.first_seq,excluded.first_seq),last_seq=max(object_blocks.last_seq,excluded.last_seq)",params![recording,seq,stream,block]).map_err(io_error)?;
    db.execute("INSERT INTO scope_blocks SELECT recording,kind,symbol,?3,?4,seq,seq FROM scope_refs WHERE recording=?1 AND seq=?2 ON CONFLICT(recording,kind,symbol,stream,block) DO UPDATE SET first_seq=min(scope_blocks.first_seq,excluded.first_seq),last_seq=max(scope_blocks.last_seq,excluded.last_seq)",params![recording,seq,stream,block]).map_err(io_error)?;
    if !topic.starts_with("/market_trade/") && !topic.starts_with("/quote/") {
        db.execute("INSERT OR IGNORE INTO business_changes SELECT r.recording,o.kind,r.seq,r.object FROM object_refs r JOIN objects o ON o.id=r.object WHERE r.recording=?1 AND r.seq=?2",params![recording,seq]).map_err(io_error)?;
    }
    Ok(())
}
