use std::collections::HashSet;
use std::io::{self, Write};

use serde::Serialize;

use rusqlite::{params, OptionalExtension};
use serde_json::json;

use super::*;

pub(super) fn plugin_network(connection: &Connection, upgrade: bool) -> Result<()> {
    let mut query = connection
        .prepare("SELECT id,trade_channel,account_config FROM account")
        .map_err(db)?;
    let rows = query
        .query_map([], |row| {
            Ok((
                row.get::<_, i64>(0)?,
                row.get::<_, String>(1)?,
                value(row, "account_config")?,
            ))
        })
        .map_err(db)?;
    for row in rows {
        let (id, channel, raw) = row.map_err(db)?;
        if matches!(channel.as_str(), "ctp" | "gm" | "tq") {
            continue;
        }
        let mut config = decode(&raw)?;
        let Some(config) = config.as_object_mut() else {
            continue;
        };
        if upgrade {
            let Some(testnet) = config.remove("is_testnet") else {
                continue;
            };
            config.entry("network").or_insert_with(|| {
                json!(if testnet == true {
                    "testnet"
                } else {
                    "mainnet"
                })
            });
        } else {
            let Some(network) = config.remove("network") else {
                continue;
            };
            config
                .entry("is_testnet")
                .or_insert_with(|| json!(network == "testnet"));
        }
        connection
            .execute(
                "UPDATE account SET account_config=?1 WHERE id=?2",
                params![encoded(&Value::Object(config.clone()))?, id],
            )
            .map_err(db)?;
    }
    Ok(())
}

pub(super) fn optional_account_column(
    connection: &Connection,
    name: &str,
    kind: &str,
    add: bool,
) -> Result<()> {
    let present = columns(connection, "account")?.contains(name);
    if add && !present {
        sql(
            connection,
            &format!(
                "ALTER TABLE account ADD COLUMN {} {kind}",
                identifier(name)?
            ),
        )?;
    }
    if !add && present {
        sql(
            connection,
            &format!("ALTER TABLE account DROP COLUMN {}", identifier(name)?),
        )?;
    }
    Ok(())
}

pub(super) fn asset_snapshots(connection: &Connection) -> Result<()> {
    let mut query = connection
        .prepare("SELECT account_id,execution_id,raw_result,created_at FROM executerecord")
        .map_err(db)?;
    let rows = query
        .query_map([], |row| {
            Ok((
                row.get::<_, i64>(0)?,
                row.get::<_, Option<String>>(1)?,
                value(row, "raw_result")?,
                row.get::<_, String>(3)?,
            ))
        })
        .map_err(db)?;
    for row in rows {
        let (account, execution, raw, created) = row.map_err(db)?;
        let result = decode(&raw)?;
        let Some(assets) = result
            .get("account_assets")
            .filter(|assets| assets.as_object().is_some_and(|assets| !assets.is_empty()))
        else {
            continue;
        };
        connection.execute("INSERT INTO account_asset_snapshot(account_id,assets,source,execution_id,created_at) VALUES(?1,?2,'execution',?3,?4)",params![account,encoded(assets)?,execution,created]).map_err(db)?;
    }
    Ok(())
}

// 旧回填经 SQLAlchemy JSON 编码，保留默认分隔符与 ASCII 转义。
fn target_weights_json(value: &Value) -> Result<String> {
    struct Formatter;
    impl serde_json::ser::Formatter for Formatter {
        fn begin_array_value<W: ?Sized + Write>(
            &mut self,
            writer: &mut W,
            first: bool,
        ) -> io::Result<()> {
            if first {
                Ok(())
            } else {
                writer.write_all(b", ")
            }
        }
        fn begin_object_key<W: ?Sized + Write>(
            &mut self,
            writer: &mut W,
            first: bool,
        ) -> io::Result<()> {
            if first {
                Ok(())
            } else {
                writer.write_all(b", ")
            }
        }
        fn begin_object_value<W: ?Sized + Write>(&mut self, writer: &mut W) -> io::Result<()> {
            writer.write_all(b": ")
        }
        fn write_string_fragment<W: ?Sized + Write>(
            &mut self,
            writer: &mut W,
            fragment: &str,
        ) -> io::Result<()> {
            for character in fragment.chars() {
                if character <= '\u{7e}' {
                    writer.write_all(&[character as u8])?;
                } else {
                    for unit in character.encode_utf16(&mut [0; 2]) {
                        write!(writer, "\\u{unit:04x}")?;
                    }
                }
            }
            Ok(())
        }
    }
    let mut output = Vec::new();
    value
        .serialize(&mut serde_json::Serializer::with_formatter(
            &mut output,
            Formatter,
        ))
        .map_err(|error| super::error(error.to_string()))?;
    String::from_utf8(output).map_err(|error| super::error(error.to_string()))
}

pub(super) fn target_snapshots(connection: &Connection) -> Result<()> {
    if table_exists(connection, "target_weight_snapshot")? {
        let present = columns(connection, "target_weight_snapshot")?;
        let required = BTreeSet::from([
            "id",
            "portfolio_id",
            "account_id",
            "raw_weights",
            "normalized_weights",
            "source",
            "execution_id",
            "calculated_at",
        ]);
        let missing: Vec<_> = required
            .into_iter()
            .filter(|name| !present.contains(*name))
            .collect();
        if !missing.is_empty() {
            return Err(error(format!(
                "已有 target_weight_snapshot 表结构不完整，缺少列: {}",
                missing.join(", ")
            )));
        }
    }
    sql(connection, include_str!("0005.sql"))?;
    let mut existing_query = connection
        .prepare("SELECT execution_id FROM target_weight_snapshot WHERE execution_id IS NOT NULL")
        .map_err(db)?;
    let existing: HashSet<String> = existing_query
        .query_map([], |row| row.get(0))
        .map_err(db)?
        .collect::<std::result::Result<_, _>>()
        .map_err(db)?;
    let mut seen = HashSet::new();
    let mut query=connection.prepare("SELECT id,account_id,execution_id,raw_input,created_at FROM executerecord WHERE is_success=1 ORDER BY id DESC").map_err(db)?;
    let rows = query
        .query_map([], |row| {
            Ok((
                row.get::<_, i64>(1)?,
                row.get::<_, Option<String>>(2)?,
                value(row, "raw_input")?,
                row.get::<_, String>(4)?,
            ))
        })
        .map_err(db)?;
    for row in rows {
        let (account, execution, raw, created) = row.map_err(db)?;
        if seen.contains(&account)
            || execution
                .as_ref()
                .is_some_and(|execution| existing.contains(execution))
        {
            continue;
        }
        let input = decode(&raw)?;
        let Some(target) = input.get("curr_target").filter(|target| target.is_object()) else {
            continue;
        };
        let binding:Option<Option<i64>>=connection.query_row("SELECT portfolio_id FROM portfolioaccount WHERE account_id=?1 AND created_at<=?2 ORDER BY id DESC LIMIT 1",params![account,created],|row|row.get(0)).optional().map_err(db)?;
        let Some(Some(portfolio)) = binding else {
            continue;
        };
        seen.insert(account);
        connection.execute("INSERT INTO target_weight_snapshot(portfolio_id,account_id,raw_weights,normalized_weights,source,execution_id,calculated_at) VALUES(?1,?2,'null',?3,'execution',?4,?5)",params![portfolio,account,target_weights_json(target)?,execution,created]).map_err(db)?;
    }
    Ok(())
}

pub(super) const ANALYSIS_UPSERT:&str="ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1, requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL THEN 1 ELSE requested END, failures=0,retry_at=0,error=NULL";

pub(super) fn flat_settings_trigger(connection: &Connection) -> Result<()> {
    sql(connection,&format!("CREATE TRIGGER analysis_account_settings AFTER UPDATE OF backtest_weight_type,backtest_fee_rate ON account WHEN OLD.backtest_weight_type IS NOT NEW.backtest_weight_type OR OLD.backtest_fee_rate IS NOT NEW.backtest_fee_rate BEGIN INSERT INTO account_analysis(account_id,source_version) VALUES(NEW.id,1) {ANALYSIS_UPSERT}; END"))
}

pub(super) fn analysis_triggers(connection: &Connection) -> Result<()> {
    for table in [
        "executerecord",
        "portfolioaccount",
        "target_weight_snapshot",
        "schedule_skip",
    ] {
        for action in ["INSERT", "UPDATE", "DELETE"] {
            let refs: &[&str] = match action {
                "UPDATE" => &["OLD", "NEW"],
                "DELETE" => &["OLD"],
                _ => &["NEW"],
            };
            let mut statements = String::new();
            for reference in refs {
                let moved = if action == "UPDATE" && *reference == "NEW" {
                    "AND NEW.account_id IS NOT OLD.account_id"
                } else {
                    ""
                };
                statements.push_str(&format!("INSERT INTO account_analysis(account_id,source_version) SELECT {reference}.account_id,1 WHERE {reference}.account_id IS NOT NULL AND EXISTS(SELECT 1 FROM account WHERE id={reference}.account_id) {moved} {ANALYSIS_UPSERT};"));
            }
            sql(connection,&format!("CREATE TRIGGER IF NOT EXISTS analysis_{table}_{} AFTER {action} ON {table} BEGIN {statements} END",action.to_lowercase()))?;
        }
    }
    if connection
        .query_row(
            "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='analysis_account_settings'",
            [],
            |_| Ok(()),
        )
        .optional()
        .map_err(db)?
        .is_none()
    {
        flat_settings_trigger(connection)?;
    }
    sql(connection,"CREATE TRIGGER IF NOT EXISTS analysis_account_delete AFTER DELETE ON account BEGIN DELETE FROM performance_cost_trade WHERE snapshot_id IN(SELECT id FROM performance_snapshot WHERE account_id=OLD.id); DELETE FROM performance_cost_execution WHERE snapshot_id IN(SELECT id FROM performance_snapshot WHERE account_id=OLD.id); DELETE FROM performance_snapshot WHERE account_id=OLD.id; DELETE FROM account_analysis WHERE account_id=OLD.id; END")
}

pub(super) fn analysis_downgrade(connection: &Connection) -> Result<()> {
    for table in [
        "executerecord",
        "portfolioaccount",
        "target_weight_snapshot",
        "schedule_skip",
    ] {
        for action in ["insert", "update", "delete"] {
            sql(
                connection,
                &format!("DROP TRIGGER IF EXISTS analysis_{table}_{action}"),
            )?;
        }
    }
    sql(connection,"DROP TRIGGER IF EXISTS analysis_account_settings; DROP TRIGGER IF EXISTS analysis_account_delete; DROP TABLE performance_cost_trade; DROP TABLE performance_cost_execution; DROP TABLE performance_snapshot; DROP TABLE account_analysis")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn target_json_preserves_default_spaces_and_escaping_without_changing_keys() {
        assert_eq!(
            target_weights_json(&json!({"a,b:c":[1.5,"中😀"]})).unwrap(),
            r#"{"a,b:c": [1.5, "\u4e2d\ud83d\ude00"]}"#
        );
    }

    #[test]
    fn target_backfill_uses_newest_provable_binding_and_json_null() {
        let connection = Connection::open_in_memory().unwrap();
        sql(&connection,"CREATE TABLE account(id INTEGER PRIMARY KEY); CREATE TABLE portfolio(id INTEGER PRIMARY KEY); CREATE TABLE executerecord(id INTEGER PRIMARY KEY,account_id INTEGER,execution_id TEXT,raw_input JSON,is_success INTEGER,created_at TEXT); CREATE TABLE portfolioaccount(id INTEGER PRIMARY KEY,account_id INTEGER,portfolio_id INTEGER,created_at TEXT); INSERT INTO account VALUES(1); INSERT INTO portfolio VALUES(2); INSERT INTO portfolioaccount VALUES(1,1,2,'a'); INSERT INTO executerecord VALUES(1,1,'old','{\"curr_target\":{\"a\":1}}',1,'b'),(2,1,'new','{\"curr_target\":{}}',1,'c'),(3,1,'bad','{}',1,'d')").unwrap();
        target_snapshots(&connection).unwrap();
        let (execution, raw, target): (String, String, String) = connection
            .query_row(
                "SELECT execution_id,raw_weights,normalized_weights FROM target_weight_snapshot",
                [],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .unwrap();
        assert_eq!(
            (execution.as_str(), raw.as_str(), target.as_str()),
            ("new", "null", "{}")
        );
    }
}
