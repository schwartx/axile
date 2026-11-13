//! Account-owned external capital movements; edits invalidate analysis in the same transaction.
use std::path::Path;

use rusqlite::{params, Connection};
use serde_json::{json, Value};

use crate::{analysis, error::AnalysisError, time_us, writer_scope};

fn invalid(message: impl Into<String>) -> AnalysisError {
    AnalysisError::new("INVALID_REQUEST", message)
}
fn integer(v: &Value, key: &str) -> Result<i64, AnalysisError> {
    v[key]
        .as_i64()
        .filter(|n| *n > 0)
        .ok_or_else(|| invalid(format!("invalid {key}")))
}
fn text<'a>(v: &'a Value, key: &str) -> Result<&'a str, AnalysisError> {
    v[key]
        .as_str()
        .ok_or_else(|| invalid(format!("missing {key}")))
}
fn timestamp(v: &Value, key: &str) -> Result<i64, AnalysisError> {
    time_us::parse(text(v, key)?).map_err(invalid)
}
fn row(c: &Connection, account: i64, id: i64) -> Result<Value, AnalysisError> {
    analysis::rows(
        c,
        "SELECT * FROM account_cash_flow WHERE account_id=?1 AND id=?2",
        &[&account, &id],
    )?
    .pop()
    .ok_or_else(|| AnalysisError::new("CASH_FLOW_NOT_FOUND", "流水不存在"))
}
fn audit(c: &Connection, before: &Value, now: i64) -> Result<(), AnalysisError> {
    c.execute("INSERT INTO account_cash_flow_revision(cash_flow_id,before_json,changed_at_us) VALUES(?1,?2,?3)",
        params![before["id"].as_i64(), before.to_string(), now]).map_err(analysis::sql)?;
    Ok(())
}
fn changed(c: &Connection, account: i64) -> Result<(), AnalysisError> {
    c.execute(
        "INSERT INTO account_analysis(account_id) VALUES(?1) ON CONFLICT DO NOTHING",
        [account],
    )
    .map_err(analysis::sql)?;
    c.execute("UPDATE account_analysis SET source_version=source_version+1,dirty_record=0,requested=1,failures=0,retry_at_us=0,error=NULL WHERE account_id=?1", [account]).map_err(analysis::sql)?;
    Ok(())
}
fn checked<'a>(v: &'a Value, now: i64) -> Result<(&'a str, f64, i64, &'a str), AnalysisError> {
    let direction = text(v, "direction")?;
    if !matches!(direction, "deposit" | "withdraw") {
        return Err(invalid("方向必须为入金或出金"));
    }
    let amount = v["amount"]
        .as_f64()
        .filter(|n| n.is_finite() && *n > 0.0 && *n < 1e100)
        .ok_or_else(|| invalid("金额必须为有限正数"))?;
    let time = timestamp(v, "occurred_at")?;
    if time > now {
        return Err(invalid("不能记录尚未发生的出入金"));
    }
    let note = v
        .get("note")
        .map(|n| n.as_str().ok_or_else(|| invalid("备注必须是文本")))
        .transpose()?
        .unwrap_or("");
    if note.chars().count() > 500 {
        return Err(invalid("备注不能超过500字"));
    }
    Ok((direction, amount, time, note))
}
fn revision(before: &Value, request: &Value) -> Result<(), AnalysisError> {
    if before["revision"].as_i64() != Some(integer(request, "revision")?) {
        return Err(AnalysisError::new(
            "CASH_FLOW_CHANGED",
            "流水已更新，请重新读取",
        ));
    }
    Ok(())
}
fn manual_status(
    c: &Connection,
    account: i64,
    direction: &str,
    amount: f64,
) -> Result<&'static str, AnalysisError> {
    let duplicate: bool=c.query_row("SELECT EXISTS(SELECT 1 FROM account_cash_flow WHERE account_id=?1 AND source='channel' AND status='active' AND direction=?2 AND amount=?3)",params![account,direction,amount],|r|r.get(0)).map_err(analysis::sql)?;
    Ok(if duplicate { "pending" } else { "active" })
}
fn create(c: &Connection, account: i64, r: &Value, now: i64) -> Result<Value, AnalysisError> {
    let (direction, amount, time, note) = checked(r, now)?;
    let status = manual_status(c, account, direction, amount)?;
    c.execute("INSERT INTO account_cash_flow(account_id,direction,amount,occurred_at_us,source,status,note,created_at_us,updated_at_us) VALUES(?1,?2,?3,?4,'manual',?7,?5,?6,?6)", params![account,direction,amount,time,note,now,status]).map_err(analysis::sql)?;
    let result = row(c, account, c.last_insert_rowid())?;
    changed(c, account)?;
    Ok(result)
}
fn edit(c: &Connection, account: i64, r: &Value, now: i64) -> Result<Value, AnalysisError> {
    let id = integer(r, "id")?;
    let before = row(c, account, id)?;
    revision(&before, r)?;
    if before["source"] != "manual" || before["status"] != "active" {
        return Err(invalid("只能修改有效的手工流水"));
    }
    let (direction, amount, time, note) = checked(r, now)?;
    let status = manual_status(c, account, direction, amount)?;
    audit(c, &before, now)?;
    c.execute("UPDATE account_cash_flow SET direction=?2,amount=?3,occurred_at_us=?4,note=?5,status=?7,revision=revision+1,updated_at_us=?6 WHERE id=?1", params![id,direction,amount,time,note,now,status]).map_err(analysis::sql)?;
    changed(c, account)?;
    row(c, account, id)
}
fn resolve(c: &Connection, account: i64, r: &Value, now: i64) -> Result<Value, AnalysisError> {
    let id = integer(r, "id")?;
    let before = row(c, account, id)?;
    revision(&before, r)?;
    let op = text(r, "op")?;
    if op == "confirm" || op == "link" {
        if before["status"] != "pending" {
            return Err(invalid("流水无需核对"));
        }
        if op == "link" {
            let manual = row(c, account, integer(r, "manual_id")?)?;
            if manual["source"] != "manual"
                || manual["status"] != "active"
                || manual["direction"] != before["direction"]
                || manual["amount"] != before["amount"]
            {
                return Err(invalid("只能关联同账户、同方向、同金额的有效手工流水"));
            }
            revision(&manual, &json!({"revision":r["manual_revision"]}))?;
            audit(c, &manual, now)?;
            c.execute("UPDATE account_cash_flow SET status='void',note=note || ?2,revision=revision+1,updated_at_us=?3 WHERE id=?1", params![manual["id"].as_i64(),format!(" [已关联渠道流水 {id}]"),now]).map_err(analysis::sql)?;
        }
    }
    audit(c, &before, now)?;
    c.execute(
        "UPDATE account_cash_flow SET status=?2,revision=revision+1,updated_at_us=?3 WHERE id=?1",
        params![id, if op == "void" { "void" } else { "active" }, now],
    )
    .map_err(analysis::sql)?;
    changed(c, account)?;
    row(c, account, id)
}
fn sync_one(c: &Connection, account: i64, r: &Value, now: i64) -> Result<bool, AnalysisError> {
    let (direction, amount, time, note) = checked(r, now)?;
    let external = text(r, "external_id")?;
    if external.is_empty() || external.len() > 200 {
        return Err(invalid("无效渠道流水号"));
    }
    let previous = analysis::rows(
        c,
        "SELECT * FROM account_cash_flow WHERE account_id=?1 AND external_id=?2",
        &[&account, &external],
    )?
    .pop();
    let cancelled = match r.get("cancelled") {
        None => false,
        Some(Value::Bool(value)) => *value,
        _ => return Err(invalid("无效的渠道撤销标志")),
    };
    if let Some(before) = previous {
        if cancelled && before["status"] != "void" {
            audit(c, &before, now)?;
            c.execute("UPDATE account_cash_flow SET status='void',revision=revision+1,updated_at_us=?2 WHERE id=?1",params![before["id"].as_i64(),now]).map_err(analysis::sql)?;
            return Ok(true);
        }
        if before["status"] == "void" {
            return Ok(false);
        }
        if before["amount"].as_f64() == Some(amount)
            && before["direction"] == direction
            && before["occurred_at_us"].as_i64() == Some(time)
        {
            return Ok(false);
        }
        audit(c, &before, now)?;
        // A revised channel record needs confirmation; never silently replace an accepted amount.
        c.execute("UPDATE account_cash_flow SET direction=?2,amount=?3,occurred_at_us=?4,status='pending',revision=revision+1,updated_at_us=?5 WHERE id=?1",params![before["id"].as_i64(),direction,amount,time,now]).map_err(analysis::sql)?;
        return Ok(true);
    }
    let duplicate: bool = c.query_row("SELECT EXISTS(SELECT 1 FROM account_cash_flow WHERE account_id=?1 AND source='manual' AND status='active' AND direction=?2 AND amount=?3)",params![account,direction,amount],|row|row.get(0)).map_err(analysis::sql)?;
    c.execute("INSERT INTO account_cash_flow(account_id,direction,amount,occurred_at_us,source,external_id,status,note,created_at_us,updated_at_us) VALUES(?1,?2,?3,?4,'channel',?5,?6,?7,?8,?8)",params![account,direction,amount,time,external,if cancelled {"void"} else if duplicate {"pending"} else {"active"},note,now]).map_err(analysis::sql)?;
    Ok(true)
}
fn write(c: &Connection, account: i64, r: &Value) -> Result<Value, AnalysisError> {
    let exists: bool = c
        .query_row(
            "SELECT EXISTS(SELECT 1 FROM account WHERE id=?1)",
            [account],
            |r| r.get(0),
        )
        .map_err(analysis::sql)?;
    if !exists {
        return Err(AnalysisError::new("ACCOUNT_NOT_FOUND", "账户不存在"));
    }
    crate::accounts::ensure_current_on(c, account)?;
    let now = timestamp(r, "now")?;
    match text(r, "op")? {
        "create" => create(c, account, r, now),
        "edit" => edit(c, account, r, now),
        "void" | "confirm" | "link" => resolve(c, account, r, now),
        "sync" => {
            if let Some(key) = r.get("config_key") {
                if key.as_str() != crate::connection_state::saved_key(c, account)?.as_deref() {
                    return Err(AnalysisError::new(
                        "ACCOUNT_CONFLICT",
                        "账户连接配置已变化，请重新同步流水",
                    ));
                }
            }
            let rows = r["rows"]
                .as_array()
                .filter(|rows| rows.len() <= 10000)
                .ok_or_else(|| invalid("渠道流水批次无效"))?;
            let mut count = 0;
            for r in rows {
                count += usize::from(sync_one(c, account, r, now)?);
            }
            if count > 0 {
                changed(c, account)?;
            }
            Ok(json!({"changed":count}))
        }
        _ => Err(invalid("不支持的流水操作")),
    }
}
pub fn operation(path: &str, r: &Value) -> Result<Value, AnalysisError> {
    let account = integer(r, "account_id")?;
    if r["op"] == "list" {
        return crate::with_read_snapshot(Path::new(path), |c| {
            let exists: bool = c
                .query_row(
                    "SELECT EXISTS(SELECT 1 FROM account WHERE id=?1)",
                    [account],
                    |r| r.get(0),
                )
                .map_err(analysis::sql)?;
            if !exists {
                return Err(AnalysisError::new("ACCOUNT_NOT_FOUND", "账户不存在"));
            }
            let limit = r.get("limit").and_then(Value::as_i64).unwrap_or(100);
            let offset = r.get("offset").and_then(Value::as_i64).unwrap_or(0);
            if !(1..=500).contains(&limit) || offset < 0 {
                return Err(invalid("无效分页"));
            }
            let total: i64 = c
                .query_row(
                    "SELECT count(*) FROM account_cash_flow WHERE account_id=?1",
                    [account],
                    |r| r.get(0),
                )
                .map_err(analysis::sql)?;
            let mut rows = analysis::rows(c,"SELECT * FROM account_cash_flow WHERE account_id=?1 ORDER BY (status='pending') DESC,occurred_at_us DESC,id DESC LIMIT ?2 OFFSET ?3", &[&account,&limit,&offset])?;
            for r in &mut rows {
                if r["status"] == "pending" && r["source"] == "channel" {
                    let direction = r["direction"].as_str().unwrap();
                    let amount = r["amount"].as_f64().unwrap();
                    let mut candidates=analysis::rows(c,"SELECT id,revision,occurred_at_us FROM account_cash_flow WHERE account_id=?1 AND source='manual' AND status='active' AND direction=?2 AND amount=?3 ORDER BY occurred_at_us DESC LIMIT 100",&[&account,&direction,&amount])?;
                    for candidate in &mut candidates {
                        candidate["occurred_at"] = json!(time_us::format(
                            candidate["occurred_at_us"].as_i64().unwrap()
                        )
                        .map_err(invalid)?);
                        candidate.as_object_mut().unwrap().remove("occurred_at_us");
                    }
                    r["manual_candidates"] = json!(candidates);
                }
                for (from, to) in [
                    ("occurred_at_us", "occurred_at"),
                    ("created_at_us", "created_at"),
                    ("updated_at_us", "updated_at"),
                ] {
                    r[to] = json!(time_us::format(r[from].as_i64().unwrap()).map_err(invalid)?);
                    r.as_object_mut().unwrap().remove(from);
                }
            }
            Ok(json!({"data":rows,"total":total}))
        });
    }
    let c = writer_scope::open_writer(Path::new(path))?;
    writer_scope::immediate(&c, || write(&c, account, r))
}

#[derive(Clone, Copy, Debug)]
pub(crate) struct Flow {
    pub time: i64,
    pub amount: f64,
}
pub(crate) fn load(c: &Connection, account: i64) -> Result<Vec<Flow>, AnalysisError> {
    let mut s=c.prepare("SELECT occurred_at_us,CASE direction WHEN 'deposit' THEN amount ELSE -amount END FROM account_cash_flow WHERE account_id=?1 AND status='active' ORDER BY occurred_at_us,id").map_err(analysis::sql)?;
    let rows = s
        .query_map([account], |r| {
            Ok(Flow {
                time: r.get(0)?,
                amount: r.get(1)?,
            })
        })
        .map_err(analysis::sql)?;
    rows.collect::<Result<Vec<_>, _>>().map_err(analysis::sql)
}

/// Modified Dietz approximates returns when no valuation exists at the cash-flow instant.
pub(crate) fn interval(
    flows: &[Flow],
    start: i64,
    end: i64,
    before: f64,
    after: f64,
) -> Option<f64> {
    if end <= start {
        return None;
    }
    let mut net = 0.0;
    let mut capital = before;
    for f in flows.iter().filter(|f| f.time > start && f.time <= end) {
        net += f.amount;
        capital += f.amount * ((end - f.time) as f64 / (end - start) as f64);
    }
    let result = (after - before - net) / capital;
    (before >= 0.0 && capital > 0.0 && result.is_finite()).then_some(result)
}

pub(crate) fn live_return(
    c: &Connection,
    account: i64,
    observation: &Value,
    performance: &Value,
) -> Result<Option<f64>, AnalysisError> {
    let (Some(end), Some(after)) = (
        observation["created_at"].as_str(),
        observation["assets"]["total_asset"].as_f64(),
    ) else {
        return Ok(None);
    };
    let end = time_us::parse(end).map_err(invalid)?;
    let day = time_us::local_datetime(end).map_err(invalid)?.date();
    for point in performance["points"].as_array().into_iter().flatten().rev() {
        let Some(time) = point["observed_at"].as_str() else {
            continue;
        };
        let time = time_us::parse(time).map_err(invalid)?;
        // Baseline dates include a time; daily closes contain only a calendar date.
        let is_baseline = point["date"]
            .as_str()
            .is_some_and(|date| date.contains('T'));
        if time_us::local_datetime(time).map_err(invalid)?.date() < day || is_baseline {
            let flows = load(c, account)?;
            return Ok(point["account_equity"]
                .as_f64()
                .and_then(|before| interval(&flows, time, end, before, after)));
        }
    }
    Ok(None)
}

pub(crate) fn adjust(
    c: &Connection,
    account: i64,
    points: &mut [Value],
) -> Result<Value, AnalysisError> {
    let flows = load(c, account)?;
    let pending: i64 = c
        .query_row(
            "SELECT count(*) FROM account_cash_flow WHERE account_id=?1 AND status='pending'",
            [account],
            |r| r.get(0),
        )
        .map_err(analysis::sql)?;
    let first = points
        .first()
        .and_then(|p| p["observed_at"].as_str())
        .map(time_us::parse)
        .transpose()
        .map_err(invalid)?;
    let last = points
        .last()
        .and_then(|p| p["observed_at"].as_str())
        .map(time_us::parse)
        .transpose()
        .map_err(invalid)?;
    let relevant = flows
        .iter()
        .filter(|f| first.is_some_and(|s| f.time > s) && last.is_some_and(|e| f.time <= e))
        .copied()
        .collect::<Vec<_>>();
    if !relevant.is_empty() {
        adjust_curve(&relevant, points)?;
    }
    Ok(
        json!({"method":"modified_dietz","approximate":!relevant.is_empty(),"pending":pending,
        "flows":relevant.iter().map(|f| Ok(json!({"occurred_at":time_us::format(f.time).map_err(invalid)?,"amount":f.amount}))).collect::<Result<Vec<_>,AnalysisError>>()?}),
    )
}
fn adjust_curve(flows: &[Flow], points: &mut [Value]) -> Result<(), AnalysisError> {
    let Some(base) = points.first() else {
        return Ok(());
    };
    let mut previous_time = timestamp(base, "observed_at")?;
    let mut previous_equity = base["account_equity"].as_f64();
    let mut factor = Some(1.0);
    let mut adjacent_valid = previous_equity.is_some();
    for p in points.iter_mut().skip(1) {
        let time = timestamp(p, "observed_at")?;
        let equity = p["account_equity"].as_f64();
        let ret = match (previous_equity, equity) {
            (Some(a), Some(b)) if time > previous_time => {
                interval(flows, previous_time, time, a, b)
            }
            (_, Some(_)) if time == previous_time => Some(0.0),
            _ => None,
        };
        if equity.is_some() {
            factor = factor
                .zip(ret)
                .map(|(v, r)| v * (1.0 + r))
                .filter(|v| v.is_finite());
        }
        p["account_return"] = json!(equity.and(factor).map(|f| f - 1.0));
        p["account_daily_return"] = json!(ret.filter(|_| adjacent_valid && time > previous_time));
        p["difference"] = json!(p["account_return"]
            .as_f64()
            .zip(p["portfolio_return"].as_f64())
            .map(|(a, b)| a - b));
        adjacent_valid = equity.is_some();
        if equity.is_some() {
            previous_equity = equity;
            previous_time = time;
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn live_return_uses_previous_close_or_first_day_baseline() {
        let performance = json!({"points": [
            {"date":"2026-01-01T09:00:00", "observed_at":"2026-01-01T09:00:00+08:00", "account_equity":100.0},
            {"date":"2026-01-01", "observed_at":"2026-01-01T10:00:00+08:00", "account_equity":110.0}
        ]});
        for (observed_at, equity, deposit, expected) in [
            ("2026-01-01T11:00:00+08:00", 110.0, false, Some(0.1)),
            ("2026-01-01T11:00:00+08:00", 150.0, true, Some(0.0)),
            ("2026-01-02T11:00:00+08:00", 121.0, false, Some(0.1)),
            ("2026-01-01T09:00:00+08:00", 100.0, false, None),
            ("2026-01-01T08:00:00+08:00", 100.0, false, None),
        ] {
            let c = Connection::open_in_memory().unwrap();
            c.execute_batch(concat!(
                "CREATE TABLE account(id INTEGER PRIMARY KEY); INSERT INTO account VALUES(1);",
                include_str!("migrations/cash_flows.sql")
            ))
            .unwrap();
            if deposit {
                c.execute(
                    "INSERT INTO account_cash_flow(account_id,direction,amount,occurred_at_us,source,status,created_at_us,updated_at_us) VALUES(1,'deposit',50,?1,'manual','active',?1,?1)",
                    [time_us::parse(observed_at).unwrap()],
                )
                .unwrap();
            }
            let observation = json!({"created_at":observed_at,"assets":{"total_asset":equity}});
            assert_eq!(
                live_return(&c, 1, &observation, &performance).unwrap(),
                expected,
                "observation={observation}, deposit={deposit}"
            );
        }
    }

    #[test]
    fn full_withdrawal_then_deposit_keeps_cumulative_returns() {
        let flows = [
            Flow {
                time: time_us::parse("2026-01-02T09:00:00+08:00").unwrap(),
                amount: -100.0,
            },
            Flow {
                time: time_us::parse("2026-01-02T21:00:00+08:00").unwrap(),
                amount: 100.0,
            },
        ];
        let mut points = [(1, 100.0), (2, 0.0), (3, 110.0), (4, 121.0)]
            .into_iter()
            .map(|(day, equity)| {
                json!({"observed_at":format!("2026-01-{day:02}T09:00:00+08:00"),"account_equity":equity})
            })
            .collect::<Vec<_>>();
        adjust_curve(&flows, &mut points).unwrap();
        for (point, expected) in points.iter().skip(1).zip([0.0, 0.2, 0.32]) {
            let actual = point["account_return"]
                .as_f64()
                .expect("valid invested capital");
            assert!((actual - expected).abs() < 1e-12, "{point}");
        }
        assert_eq!(points[2]["account_daily_return"], 0.2);
        assert_eq!(points[3]["account_daily_return"], 0.1);
    }

    #[test]
    fn deposits_withdrawals_and_time_weighted_capital() {
        assert_eq!(interval(&[], 0, 10, 100.0, -100.0), Some(-2.0));
        assert!(interval(&[], 0, 10, 0.0, 0.0).is_none());
        assert_eq!(
            interval(
                &[Flow {
                    time: 10,
                    amount: 50.0
                }],
                0,
                10,
                110.0,
                160.0
            ),
            Some(0.0)
        );
        assert_eq!(
            interval(
                &[Flow {
                    time: 10,
                    amount: -50.0
                }],
                0,
                10,
                110.0,
                60.0
            ),
            Some(0.0)
        );
        assert!(
            (interval(
                &[Flow {
                    time: 5,
                    amount: 100.0
                }],
                0,
                10,
                100.0,
                215.0
            )
            .unwrap()
                - 0.1)
                .abs()
                < 1e-12
        );
        assert_eq!(
            interval(
                &[Flow {
                    time: 0,
                    amount: 100.0
                }],
                0,
                10,
                100.0,
                110.0
            ),
            Some(0.1)
        );
        assert!(interval(
            &[Flow {
                time: 1,
                amount: -200.0
            }],
            0,
            10,
            100.0,
            10.0
        )
        .is_none());
    }
}
