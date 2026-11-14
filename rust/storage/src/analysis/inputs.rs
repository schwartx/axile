//! Read-only, revision-bound pages; no browser analysis result is persisted.
use super::{failure, read, sql};
use crate::error::AnalysisError;
use axile_analysis_core::{account::InputKind, ALGORITHM, PROTOCOL};
use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine as _};
use rusqlite::{params, Connection};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

const TRADE_COLUMNS: &[&str] = &[
    "symbol",
    "time_us",
    "day",
    "estimated",
    "side",
    "price",
    "quantity",
    "multiplier",
    "reference",
    "fee",
    "fee_currency",
];

#[derive(Serialize, Deserialize)]
struct Cursor {
    account: i64,
    kind: InputKind,
    version: String,
    range: String,
    selection: Value,
    time: i64,
    id: i64,
}

pub(super) fn inputs(
    c: &Connection,
    account: i64,
    version: &str,
    range: &str,
    q: &Value,
) -> Result<Value, AnalysisError> {
    let state = read::guard(c, account, version, range)?;
    let kind = match q["kind"].as_str().unwrap_or("costs") {
        "costs" => InputKind::Costs,
        "fees" => InputKind::Fees,
        _ => return Err(failure("INVALID_REQUEST", "invalid analysis input kind")),
    };
    let selection: Value = ["day", "start", "end", "record_id"]
        .into_iter()
        .filter_map(|key| {
            q.get(key)
                .filter(|value| !value.is_null())
                .map(|value| (key.into(), value.clone()))
        })
        .collect::<serde_json::Map<_, _>>()
        .into();
    if kind == InputKind::Fees && selection.as_object().is_some_and(|s| !s.is_empty()) {
        return Err(failure(
            "INVALID_REQUEST",
            "fee inputs require the complete window",
        ));
    }
    let window = read::window(q)?;
    let candidates = if let Some(day) = q["day"].as_str() {
        let midnight = crate::time_us::parse(&format!("{day}T00:00:00"))
            .map_err(|e| failure("INVALID_REQUEST", e))?;
        Some((midnight - 1, midnight + 86_400_000_000 - 1))
    } else {
        window
    };
    let cursor = q
        .get("cursor")
        .filter(|v| !v.is_null())
        .map(|v| {
            let cursor = v.as_str().filter(|s| s.len() <= 1024).ok_or(())?;
            let bytes = URL_SAFE_NO_PAD.decode(cursor).map_err(|_| ())?;
            let cursor: Cursor = serde_json::from_slice(&bytes).map_err(|_| ())?;
            if cursor.account != account
                || cursor.version != version
                || cursor.range != range
                || cursor.kind != kind
                || cursor.selection != selection
            {
                return Err(());
            }
            Ok(cursor)
        })
        .transpose()
        .map_err(|_| failure("INVALID_REQUEST", "输入游标与当前窗口不匹配"))?;
    let start = if kind == InputKind::Fees {
        state.start
    } else {
        state.baseline_time.unwrap_or(i64::MIN)
    };
    let end = state.last_time.unwrap_or(i64::MIN);
    // Candidate pruning keeps unknown/invalid trade times for the canonical reader's fallback.
    let mut statement = c.prepare("SELECT e.id,e.created_at_us,CASE WHEN ?6='fees' THEN NOT EXISTS(SELECT 1 FROM executerecord n WHERE n.account_id=e.account_id AND n.result_staged=0 AND n.created_at_us=e.created_at_us AND n.id>e.id) ELSE 0 END FROM executerecord e WHERE e.account_id=?1 AND e.result_staged=0 AND e.created_at_us>=?2 AND e.created_at_us<=?3 AND (?4 IS NULL OR e.created_at_us>?4 OR (e.created_at_us=?4 AND e.id>?5)) AND (?7 IS NULL OR e.id=?7) AND (?8 IS NULL OR EXISTS(SELECT 1 FROM business_trade t WHERE t.record_id=e.id AND ((t.trade_time_us>?8 AND t.trade_time_us<=?9) OR (t.trade_time_us IS NULL AND e.created_at_us>?8 AND e.created_at_us<=?9) OR (t.trade_time_us IS NOT NULL AND datetime(t.trade_time_us/1000000,'unixepoch') IS NULL)))) ORDER BY e.created_at_us,e.id").map_err(sql)?;
    let mut rows = statement
        .query(params![
            account,
            start,
            end,
            cursor.as_ref().map(|v| v.time),
            cursor.as_ref().map(|v| v.id),
            if kind == InputKind::Fees {
                "fees"
            } else {
                "costs"
            },
            q["record_id"].as_i64(),
            candidates.map(|v| v.0),
            candidates.map(|v| v.1),
        ])
        .map_err(sql)?;
    let mut observations = (kind == InputKind::Fees)
        .then(|| crate::fact_observation::ObservationReader::new(c))
        .transpose()
        .map_err(sql)?;
    let mut ticks = (kind == InputKind::Costs)
        .then(|| crate::fact_ticks::TickReader::new(c))
        .transpose()
        .map_err(sql)?;
    let mut trades = (kind == InputKind::Costs)
        .then(|| crate::fact_trades::TradeReader::new(c))
        .transpose()
        .map_err(sql)?;
    let mut records = Vec::new();
    let mut size = 1024usize;
    let mut last = None;
    let mut more = false;
    while let Some(row) = rows.next().map_err(sql)? {
        if records.len() == 100 {
            more = true;
            break;
        }
        let id: i64 = row.get(0).map_err(sql)?;
        let time: i64 = row.get(1).map_err(sql)?;
        let canonical: bool = row.get(2).map_err(sql)?;
        let observation = if canonical && time >= state.start {
            Some(
                observations
                    .as_mut()
                    .expect("fee reader")
                    .read(account, id)
                    .map_err(|e| failure("ANALYSIS_FACTS", e))?,
            )
        } else {
            None
        };
        let raw = if kind == InputKind::Costs && time >= state.baseline_time.unwrap_or(i64::MIN) {
            trades
                .as_mut()
                .expect("cost reader")
                .read_limit(
                    id,
                    time,
                    &ticks.as_mut().expect("tick reader").read(id).map_err(sql)?,
                    20_001,
                )
                .map_err(|e| failure("ANALYSIS_FACTS", e))?
        } else {
            Vec::new()
        };
        if raw.len() > 20_000 {
            return Err(failure(
                "ANALYSIS_INPUT_LIMIT",
                "单次执行成交过多，请使用后端明细",
            ));
        }
        let trades = raw
            .into_iter()
            .filter(|t| read::matches(t, q, window))
            .map(|t| {
                json!([
                    t.symbol,
                    t.time,
                    t.day,
                    t.estimated,
                    t.side,
                    t.price,
                    t.quantity,
                    t.multiplier,
                    t.reference,
                    t.fee,
                    t.fee_currency
                ])
            })
            .collect::<Vec<_>>();
        if kind == InputKind::Costs && trades.is_empty() {
            continue;
        }
        let record = json!({
            "record_id": id.to_string(), "observation": observation,
            "trades": if kind == InputKind::Costs {
                json!({"columns": TRADE_COLUMNS, "rows": trades})
            } else { json!([]) }
        });
        let bytes =
            serde_json::to_vec(&record).map_err(|e| failure("ANALYSIS_INPUTS", e.to_string()))?;
        if size + bytes.len() > 2 * 1024 * 1024 {
            if records.is_empty() {
                return Err(failure(
                    "ANALYSIS_INPUT_LIMIT",
                    "单次执行超过本地分析上限，请使用后端明细",
                ));
            }
            more = true;
            break;
        }
        size += bytes.len();
        records.push(record);
        last = Some(Cursor {
            account,
            kind,
            version: version.into(),
            range: range.into(),
            selection: selection.clone(),
            time,
            id,
        });
    }
    let next = if more {
        last.map(|v| URL_SAFE_NO_PAD.encode(serde_json::to_vec(&v).expect("scalar cursor")))
    } else {
        None
    };
    Ok(
        json!({"protocol":PROTOCOL,"kind":kind,"algorithm":ALGORITHM,"account_id":account,"version":version,"range":range,
        "selection":selection,"initial_equity":if kind == InputKind::Fees { state.initial_equity } else { None },"records":records,"next_cursor":next}),
    )
}
