//! Market identities keep common namespace text once and only index extra observations separately.
use crate::execution_replay::{failure, io_error, ReplayResult};
use rusqlite::{params, Connection, OptionalExtension};
use serde_json::Value;

fn key_parts(key: &str) -> (String, String) {
    if let Ok(Value::Array(items)) = serde_json::from_str::<Value>(key) {
        if items.len() >= 2 && serde_json::to_string(&items).ok().as_deref() == Some(key) {
            let suffix = serde_json::to_string(items.last().unwrap()).unwrap();
            return (key[..key.len() - suffix.len() - 1].to_owned(), suffix);
        }
    }
    (String::new(), key.to_owned())
}

pub struct Pending {
    seq: u64,
    stable: bool,
    key: String,
    related: Option<u64>,
    symbol: String,
    block: u64,
    line: usize,
    stream: u16,
}
pub fn capture(
    event: &Value,
    block: u64,
    line: usize,
    stream: u16,
) -> ReplayResult<Option<Pending>> {
    if !event["topic"]
        .as_str()
        .is_some_and(|s| s.starts_with("/market_trade/"))
    {
        return Ok(None);
    }
    let seq = event["capture_seq"].as_u64().unwrap();
    let data = &event["data"];
    let stable = data["identity_quality"] == "stable"
        && data["trade_key"].as_str().is_some_and(|s| !s.is_empty());
    let related = data["related_capture_seq"]
        .as_u64()
        .filter(|n| *n > 0 && *n < seq);
    let key = if stable {
        data["trade_key"].as_str().unwrap().to_owned()
    } else {
        related.unwrap_or(seq).to_string()
    };
    if key.len() > 4096 {
        return Err(failure("BUDGET", "market identity length"));
    }
    Ok(Some(Pending {
        seq,
        stable,
        key,
        related,
        symbol: data["symbol"].as_str().unwrap_or("").into(),
        block,
        line,
        stream,
    }))
}
pub fn publish(db: &Connection, recording: &str, rows: &mut Vec<Pending>) -> ReplayResult<()> {
    if rows.len() > 32_768 {
        return Err(failure("BUDGET", "market commit identities"));
    }
    rows.sort_unstable_by_key(|r| r.seq);
    for row in rows.drain(..) {
        observation(db, recording, row)?;
    }
    Ok(())
}
fn observation(db: &Connection, recording: &str, row: Pending) -> ReplayResult<()> {
    let Pending {
        seq,
        stable,
        key,
        related,
        symbol,
        block,
        line,
        stream,
    } = row;
    let rec: i64 = db.query_row("INSERT INTO recording_keys(recording) VALUES (?1) ON CONFLICT(recording) DO UPDATE SET recording=excluded.recording RETURNING id", [recording], |r|r.get(0)).map_err(io_error)?;
    if key.len() > 4096 {
        return Err(failure("BUDGET", "market identity length"));
    }
    let (prefix, suffix) = key_parts(&key);
    let ns:i64=db.query_row("INSERT INTO market_namespaces(recording,prefix) VALUES (?1,?2) ON CONFLICT(recording,prefix) DO UPDATE SET prefix=excluded.prefix RETURNING id",params![rec,prefix],|r|r.get(0)).map_err(io_error)?;
    db.execute(
        "INSERT OR IGNORE INTO market_namespace_symbols VALUES (?1,?2,?3)",
        params![ns, symbol, seq],
    )
    .map_err(io_error)?;
    let existing:Option<u64>=db.query_row("SELECT first_seq FROM market_objects WHERE recording=?1 AND kind=?2 AND namespace=?3 AND suffix=?4",params![rec,!stable,ns,suffix],|r|r.get(0)).optional().map_err(io_error)?;
    let identity = existing.unwrap_or(seq);
    if existing.is_some() {
        db.execute(
            "INSERT OR IGNORE INTO market_refs VALUES (?1,?2,?3,?3,?4,?5,?6)",
            params![rec, identity, seq, block, line, stream],
        )
        .map_err(io_error)?;
        db.execute("UPDATE market_objects SET last_seq=max(last_seq,?3) WHERE recording=?1 AND first_seq=?2",params![rec,identity,seq]).map_err(io_error)?;
    } else {
        let last = if stable {
            seq
        } else {
            db.query_row(
                "SELECT coalesce(max(seq),?2) FROM market_refs WHERE recording=?1 AND object=?2",
                params![rec, seq],
                |r| r.get::<_, u64>(0),
            )
            .map_err(io_error)?
        };
        db.execute(
            "INSERT INTO market_objects VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,0)",
            params![rec, seq, !stable, ns, suffix, block, line, stream, last],
        )
        .map_err(io_error)?;
    }
    if stable {
        if let Some(original) = related {
            db.execute(
                "INSERT OR IGNORE INTO market_links VALUES (?1,?2,?3,?4)",
                params![rec, identity, original, seq],
            )
            .map_err(io_error)?;
            db.execute(
                "INSERT OR IGNORE INTO market_refs VALUES (?1,?2,?3,?3,?4,?5,?6)",
                params![rec, original, seq, block, line, stream],
            )
            .map_err(io_error)?;
            db.execute(
                "UPDATE market_objects SET linked=1 WHERE recording=?1 AND first_seq=?2",
                params![rec, identity],
            )
            .map_err(io_error)?;
            db.execute("UPDATE market_objects SET last_seq=max(last_seq,?3) WHERE recording=?1 AND first_seq=?2",params![rec,original,seq]).map_err(io_error)?;
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn namespace_packing_preserves_exact_opaque_and_noncanonical_keys() {
        for key in ["opaque:x", "[\"B\",\"S\",\"12\"]", "[\"B\", \"12\"]"] {
            let (prefix, suffix) = key_parts(key);
            assert_eq!(
                if prefix.is_empty() {
                    suffix
                } else {
                    format!("{prefix}{suffix}]")
                },
                key
            );
        }
    }
}
