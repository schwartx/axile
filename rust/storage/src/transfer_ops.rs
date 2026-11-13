//! 一致快照、同源累积接收和确认后的历史清理。
use crate::error::AnalysisError;
use rusqlite::{functions::FunctionFlags, params, Connection};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::path::Path;

mod files;
mod package;
mod state;

fn err(e: impl std::fmt::Display) -> AnalysisError {
    AnalysisError::new("TRANSFER", e.to_string())
}
fn text<'a>(v: &'a Value, key: &str) -> Result<&'a str, AnalysisError> {
    v[key].as_str().ok_or_else(|| err(format!("缺少 {key}")))
}
fn quote(s: &str) -> String {
    format!("\"{}\"", s.replace('"', "\"\""))
}
fn tables(c: &Connection, schema: &str) -> Result<Vec<String>, AnalysisError> {
    let mut s=c.prepare(&format!("SELECT name FROM {schema}.sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'transfer_%' ORDER BY name")).map_err(err)?;
    let rows = s.query_map([], |r| r.get(0)).map_err(err)?;
    rows.collect::<Result<Vec<_>, _>>().map_err(err)
}
fn idle(c: &Connection) -> Result<(), AnalysisError> {
    let n: i64 = c
        .query_row(
            "SELECT count(*) FROM execution_intent WHERE finished_at_us IS NULL",
            [],
            |r| r.get(0),
        )
        .map_err(err)?;
    let p: i64 = c
        .query_row(
            "SELECT count(*) FROM supplement_group WHERE status IN ('active','cancelling')",
            [],
            |r| r.get(0),
        )
        .map_err(err)?;
    if n + p > 0 {
        return Err(err("存在运行中的执行或补单"));
    }
    Ok(())
}
fn excluded(t: &str) -> bool {
    t == "alembic_version"
        || t.starts_with("analysis_")
        || t == "account_analysis"
        || t.starts_with("account_connection")
        || t.starts_with("accountruntime")
        || t.contains("notification")
        || t.starts_with("time_check")
}
fn records(c: &Connection) -> Result<Value, AnalysisError> {
    let mut s = c
        .prepare(
            "SELECT execution_id FROM executerecord WHERE execution_id IS NOT NULL ORDER BY id",
        )
        .map_err(err)?;
    let v = s
        .query_map([], |r| r.get::<_, String>(0))
        .map_err(err)?
        .collect::<Result<Vec<_>, _>>()
        .map_err(err)?;
    Ok(json!(v))
}
pub fn transfer(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    match text(request, "action")? {
        "export_package" => package::export(path, request),
        "import_package" => package::import(path, request),
        "acknowledge" => state::acknowledge(path, request),
        "prepare_package" => state::prepare(path, request),
        "prune_recordings" => state::prune_recordings_request(path, request),
        "backup_index" => {
            snapshot_file(Path::new(path), Path::new(text(request, "snapshot")?))?;
            Ok(json!({"ok":true}))
        }
        "import_index" => import_index(path, request),
        "export" => export_snapshot(path, request),
        "import" => import_snapshot(path, request),
        "prune_recording" => prune_recording(path, request),
        "prune" => prune_history(path, request),
        _ => Err(err("未知传送操作")),
    }
}

fn import_index(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let source = Path::new(text(request, "snapshot")?);
    if !Path::new(path).exists() {
        crate::execution_replay_index::BlockIndex::create(Path::new(path))?;
    }
    let c = Connection::open(path).map_err(err)?;
    c.execute(
        "ATTACH DATABASE ?1 AS incoming",
        [source.to_string_lossy().as_ref()],
    )
    .map_err(err)?;
    if tables(&c, "main")? != tables(&c, "incoming")? {
        return Err(err("录制索引版本不一致"));
    }
    for t in tables(&c, "main")? {
        let schema = |db: &str| {
            c.query_row(
                &format!("SELECT sql FROM {db}.sqlite_master WHERE type='table' AND name=?1"),
                [&t],
                |r| r.get::<_, String>(0),
            )
            .map_err(err)
        };
        if schema("main")? != schema("incoming")? {
            return Err(err("录制索引结构不一致"));
        }
    }
    let tx = c.unchecked_transaction().map_err(err)?;
    for t in tables(&tx, "main")? {
        tx.execute_batch(&format!(
            "INSERT OR IGNORE INTO main.{0} SELECT * FROM incoming.{0}",
            quote(&t)
        ))
        .map_err(err)?;
    }
    tx.execute_batch("INSERT INTO recordings SELECT * FROM incoming.recordings WHERE true ON CONFLICT(recording) DO UPDATE SET metadata=excluded.metadata").map_err(err)?;
    tx.commit().map_err(err)?;
    Ok(json!({"ok":true}))
}

fn snapshot_file(source: &Path, target: &Path) -> Result<(), AnalysisError> {
    if target.exists() {
        return Err(err("快照文件已存在"));
    }
    let source = Connection::open_with_flags(source, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY)
        .map_err(err)?;
    let mut dest = Connection::open(target).map_err(err)?;
    let backup = rusqlite::backup::Backup::new(&source, &mut dest).map_err(err)?;
    backup
        .run_to_completion(128, std::time::Duration::from_millis(10), None)
        .map_err(err)?;
    drop(backup);
    dest.execute_batch("PRAGMA wal_checkpoint(TRUNCATE); PRAGMA journal_mode=DELETE")
        .map_err(err)?;
    Ok(())
}

fn export_snapshot(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let target = Path::new(text(request, "snapshot")?);
    if target.exists() {
        return Err(err("快照已存在"));
    }
    snapshot_file(Path::new(path), target)?;
    let c = Connection::open(target).map_err(err)?;
    c.execute_batch("PRAGMA foreign_keys=ON; BEGIN IMMEDIATE")
        .map_err(err)?;
    let start = request["start_us"].as_i64().unwrap_or(0);
    let end = request["end_us"]
        .as_i64()
        .ok_or_else(|| err("缺少截止时间"))?;
    // 删除只发生于独立快照，运行中的源数据库不受影响。
    c.execute("DELETE FROM executerecord WHERE created_at_us<?1 OR created_at_us>=?2 OR execution_id IN (SELECT execution_id FROM execution_intent WHERE finished_at_us IS NULL)",params![start,end]).map_err(err)?;
    c.execute("DELETE FROM execution_intent WHERE finished_at_us IS NULL OR created_at_us<?1 OR created_at_us>=?2",params![start,end]).map_err(err)?;
    c.execute(
        "DELETE FROM supplement_group WHERE status IN ('active','cancelling')",
        [],
    )
    .map_err(err)?;
    for table in ["execution_event", "execution_artifact"] {
        c.execute(&format!("DELETE FROM {} WHERE execution_id NOT IN (SELECT execution_id FROM executerecord WHERE execution_id IS NOT NULL)",quote(table)),[]).map_err(err)?;
    }
    c.execute_batch("COMMIT; PRAGMA wal_checkpoint(TRUNCATE); PRAGMA journal_mode=DELETE")
        .map_err(err)?;
    Ok(json!({"executions":records(&c)?}))
}

fn merge_table(c: &Connection, t: &str) -> Result<(), AnalysisError> {
    // UPSERT 保留旧历史，不以 REPLACE 触发父行删除与级联。
    let mut info = c
        .prepare(&format!("PRAGMA main.table_info({})", quote(t)))
        .map_err(err)?;
    let cols = info
        .query_map([], |r| Ok((r.get::<_, String>(1)?, r.get::<_, i64>(5)?)))
        .map_err(err)?
        .collect::<Result<Vec<_>, _>>()
        .map_err(err)?;
    let keys = cols
        .iter()
        .filter(|(_, pk)| *pk > 0)
        .map(|(name, _)| quote(name))
        .collect::<Vec<_>>();
    if keys.is_empty() {
        return Err(err(format!("表缺少主键: {t}")));
    }
    let join = keys
        .iter()
        .map(|key| format!("m.{key} IS i.{key}"))
        .collect::<Vec<_>>()
        .join(" AND ");
    let row_key = format!(
        "json_array({})",
        keys.iter()
            .map(|k| format!("m.{k}"))
            .collect::<Vec<_>>()
            .join(",")
    );
    let row_hash = format!(
        "transfer_hash(json_array({}))",
        cols.iter()
            .map(|(n, _)| format!("m.{}", quote(n)))
            .collect::<Vec<_>>()
            .join(",")
    );
    let conflict:bool=c.query_row(&format!("SELECT EXISTS(SELECT 1 FROM main.{table} m JOIN incoming.{table} i ON {join} LEFT JOIN transfer_rows r ON r.table_name=?1 AND r.row_key={row_key} WHERE r.row_hash IS NOT {row_hash})", table=quote(t)), [t], |r|r.get(0)).map_err(err)?;
    if conflict {
        return Err(err(format!("接收端存在独立新增或修改的数据: {t}")));
    }
    let updates = cols
        .iter()
        .filter(|(_, pk)| *pk == 0)
        .map(|(n, _)| format!("{0}=excluded.{0}", quote(n)))
        .collect::<Vec<_>>()
        .join(",");
    let conflict = if updates.is_empty() {
        "DO NOTHING".to_owned()
    } else {
        format!("DO UPDATE SET {updates}")
    };
    c.execute_batch(&format!(
        "INSERT INTO main.{0} SELECT * FROM incoming.{0} WHERE true ON CONFLICT ({1}) {conflict}",
        quote(t),
        keys.join(",")
    ))
    .map_err(err)?;
    if t == "account_settings" {
        c.execute(
            "UPDATE account_settings SET schedule=json_set(schedule,'$.is_started',json('false'))",
            [],
        )
        .map_err(err)?;
    }
    if t == "account_control_session" {
        // 源端活跃会话在接收端只有历史观测；不能由本机启动恢复再次改写。
        c.execute(
            "UPDATE account_control_session SET status='incomplete' WHERE status='active' AND id IN (SELECT id FROM incoming.account_control_session)",
            [],
        )
        .map_err(err)?;
    }
    c.execute(&format!("INSERT INTO transfer_rows SELECT ?1,{row_key},{row_hash} FROM main.{table} m JOIN incoming.{table} i ON {join} WHERE true ON CONFLICT(table_name,row_key) DO UPDATE SET row_hash=excluded.row_hash", table=quote(t)), [t]).map_err(err)?;
    Ok(())
}

fn import_snapshot(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let source = text(request, "source_id")?;
    let c = Connection::open(path).map_err(err)?;
    c.create_scalar_function(
        "transfer_hash",
        1,
        FunctionFlags::SQLITE_DETERMINISTIC,
        |context| {
            Ok(format!(
                "{:x}",
                Sha256::digest(context.get::<String>(0)?.as_bytes())
            ))
        },
    )
    .map_err(err)?;
    c.execute_batch("PRAGMA foreign_keys=ON; PRAGMA busy_timeout=5000")
        .map_err(err)?;
    idle(&c)?;
    let started: i64 = c
        .query_row(
            "SELECT count(*) FROM account_settings WHERE json_extract(schedule,'$.is_started')=1",
            [],
            |r| r.get(0),
        )
        .map_err(err)?;
    if started > 0 {
        return Err(err("接收前请暂停本机账户调度"));
    }
    c.execute(
        "ATTACH DATABASE ?1 AS incoming",
        [text(request, "snapshot")?],
    )
    .map_err(err)?;
    let dest = tables(&c, "main")?;
    if dest != tables(&c, "incoming")? {
        return Err(err("数据结构不同，请先安装匹配版本"));
    }
    for t in &dest {
        let schema = |db: &str| {
            c.query_row(
                &format!("SELECT sql FROM {db}.sqlite_master WHERE type='table' AND name=?1"),
                [t],
                |r| r.get::<_, String>(0),
            )
            .map_err(err)
        };
        if schema("main")? != schema("incoming")? {
            return Err(err(format!("表结构不匹配: {t}")));
        }
    }
    let tx = c.unchecked_transaction().map_err(err)?;
    let c = &tx;
    c.execute_batch("PRAGMA defer_foreign_keys=ON; CREATE TABLE IF NOT EXISTS transfer_origin(source TEXT PRIMARY KEY); CREATE TABLE IF NOT EXISTS transfer_rows(table_name TEXT NOT NULL,row_key TEXT NOT NULL,row_hash TEXT NOT NULL,PRIMARY KEY(table_name,row_key)) WITHOUT ROWID").map_err(err)?;
    let origin: Option<String> = c
        .query_row("SELECT source FROM transfer_origin LIMIT 1", [], |r| {
            r.get(0)
        })
        .ok();
    if origin.as_deref().is_some_and(|s| s != source) {
        return Err(err("接收端已属于其他来源"));
    }
    if origin.is_none() {
        let count: i64 = c
            .query_row(
                "SELECT (SELECT count(*) FROM account)+(SELECT count(*) FROM portfolio)",
                [],
                |r| r.get(0),
            )
            .map_err(err)?;
        if count > 0 {
            return Err(err("首次接收需要空账户与组合，防止覆盖本机数据"));
        }
        c.execute("INSERT INTO transfer_origin VALUES(?1)", [source])
            .map_err(err)?;
    }
    let collisions:i64=c.query_row("SELECT count(*) FROM main.executerecord m JOIN incoming.executerecord i ON i.id=m.id WHERE m.execution_id IS NOT i.execution_id",[],|r|r.get(0)).map_err(err)?;
    if collisions > 0 {
        return Err(err("执行记录身份冲突，接收端不能与发送端独立生成重叠记录"));
    }
    for t in dest.iter().filter(|t| !excluded(t)) {
        merge_table(c, t)?;
    }
    let result = json!({"source_id":source,"executions":records(c)?});
    tx.commit().map_err(err)?;
    Ok(result)
}

fn prune_recording(_path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let index = Path::new(text(request, "index")?);
    let recording = text(request, "recording")?;
    if !recording
        .bytes()
        .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
    {
        return Err(err("无效录制 ID"));
    }
    if !index.exists() {
        return Ok(json!({"safe":false}));
    }
    let c = Connection::open(index).map_err(err)?;
    c.execute_batch("PRAGMA busy_timeout=5000").map_err(err)?;
    let tx = c.unchecked_transaction().map_err(err)?;
    let active:i64=tx.query_row("SELECT count(*) FROM recordings WHERE (recording=?1 AND (json_extract(metadata,'$.unsealed_start_us') IS NOT NULL OR json_extract(metadata,'$.state') IS NOT 'closed')) OR json_extract(metadata,'$.previous_recording_id')=?1",[recording],|r|r.get(0)).map_err(err)?;
    if active > 0 {
        return Ok(json!({"safe":false}));
    }
    for table in [
        "files",
        "sealed_files",
        "blocks",
        "watermarks",
        "checkpoints",
        "facts",
        "recording_executions",
        "recordings",
    ] {
        tx.execute(
            &format!("DELETE FROM {} WHERE recording=?1", quote(table)),
            [recording],
        )
        .map_err(err)?;
    }
    tx.commit().map_err(err)?;
    Ok(json!({"safe":true}))
}

fn prune_history(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let c = Connection::open(path).map_err(err)?;
    c.execute_batch("PRAGMA foreign_keys=ON; PRAGMA busy_timeout=5000")
        .map_err(err)?;
    idle(&c)?;
    let cutoff = request["cutoff_us"]
        .as_i64()
        .ok_or_else(|| err("缺少保留时间"))?;
    let ids = request["executions"]
        .as_array()
        .ok_or_else(|| err("缺少接收确认"))?;
    let tx = c.unchecked_transaction().map_err(err)?;
    let c = &tx;
    let mut removed = 0;
    for id in ids {
        let id = id.as_str().ok_or_else(|| err("无效执行 ID"))?;
        let count=c.execute("DELETE FROM executerecord WHERE execution_id=?1 AND id<(SELECT MAX(id) FROM executerecord) AND created_at_us<?2 AND NOT EXISTS(SELECT 1 FROM execution_intent WHERE execution_id=?1 AND finished_at_us IS NULL)",params![id,cutoff]).map_err(err)?;
        if count > 0 {
            for table in ["execution_event", "execution_artifact", "execution_intent"] {
                c.execute(
                    &format!(
                        "DELETE FROM {0} WHERE execution_id=?1 AND id<(SELECT MAX(id) FROM {0})",
                        quote(table)
                    ),
                    [id],
                )
                .map_err(err)?;
            }
        }
        removed += count;
    }
    tx.commit().map_err(err)?;
    Ok(json!({"removed_executions":removed}))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn quoted_identifiers_and_runtime_boundaries() {
        assert_eq!(quote("a\"b"), "\"a\"\"b\"");
        assert!(excluded("accountruntimesync"));
        assert!(excluded("notification_event"));
        assert!(!excluded("execution_event"));
    }
}
