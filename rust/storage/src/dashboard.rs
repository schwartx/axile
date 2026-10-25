//! Persisted dashboard inputs, read on the caller's single SQLite snapshot.

use std::collections::HashMap;

use rusqlite::Connection;
use serde_json::{json, Map, Value};

use crate::{assets, snapshots};

fn database(error: rusqlite::Error) -> String {
    format!("DASHBOARD_READ: {error}")
}

fn invalid(message: &str) -> String {
    format!("INVALID_DATA: {message}")
}

fn targets(connection: &Connection, cte: &str, ids: &str) -> Result<HashMap<i64, Value>, String> {
    let mut statement = connection
        .prepare(&format!(
            "{cte}
        SELECT t.account_id,t.normalized_weights_kind,t.weights_metadata,w.map_key,w.value
        FROM targets t LEFT JOIN target_weight_snapshot_weight w
        ON w.snapshot_id=t.id AND w.role='normalized_weights'
        ORDER BY t.account_id,w.map_key"
        ))
        .map_err(database)?;
    let mut rows = statement.query([ids]).map_err(database)?;
    let mut result = HashMap::new();
    while let Some(row) = rows.next().map_err(database)? {
        let id: i64 = row.get(0).map_err(database)?;
        let kind: String = row.get(1).map_err(database)?;
        match kind.as_str() {
            "mapping" => {
                let weights = result.entry(id).or_insert_with(|| json!({}));
                if let Some(key) = row.get::<_, Option<String>>(3).map_err(database)? {
                    weights[key] = json!(row.get::<_, Option<f64>>(4).map_err(database)?);
                }
            }
            "invalid" => {
                let metadata: String = row.get(2).map_err(database)?;
                let metadata: Value =
                    serde_json::from_str(&metadata).map_err(|e| invalid(&e.to_string()))?;
                let value = metadata
                    .pointer("/normalized_weights/columns/value")
                    .ok_or_else(|| invalid("missing invalid target evidence"))?;
                result.insert(id, value.clone());
            }
            _ => return Err(invalid("unknown target weight snapshot container state")),
        }
    }
    Ok(result)
}

fn observations(
    connection: &Connection,
    cte: &str,
    ids: &str,
) -> Result<HashMap<i64, Value>, String> {
    let mut statement = connection
        .prepare(&format!(
            "{cte}
        SELECT p.* FROM business_asset_position p
        WHERE p.asset_id IN (SELECT asset_fact_id FROM observations)
        ORDER BY p.asset_id,p.ordinal"
        ))
        .map_err(database)?;
    let mut rows = statement.query([ids]).map_err(database)?;
    let mut positions: HashMap<String, Vec<(i64, Value)>> = HashMap::new();
    while let Some(row) = rows.next().map_err(database)? {
        positions
            .entry(row.get("asset_id").map_err(database)?)
            .or_default()
            .push((
                row.get("ordinal").map_err(database)?,
                assets::position(row)?,
            ));
    }
    let mut statement = connection
        .prepare(&format!(
            "{cte}
        SELECT a.*,s.account_id,s.created_at,s.source AS snapshot_source,
        {} AS owner_valid FROM observations s
        JOIN business_asset_snapshot a ON a.asset_id=s.asset_fact_id",
            assets::OWNER_VALID
        ))
        .map_err(database)?;
    let mut rows = statement.query([ids]).map_err(database)?;
    let mut result = HashMap::new();
    while let Some(row) = rows.next().map_err(database)? {
        if !row.get::<_, bool>("owner_valid").map_err(database)? {
            return Err(invalid(
                "asset observation belongs to another snapshot, account or execution",
            ));
        }
        let source: String = row.get("snapshot_source").map_err(database)?;
        if !matches!(source.as_str(), "manual" | "execution") {
            return Err(invalid("invalid snapshot source"));
        }
        let asset_id: String = row.get("asset_id").map_err(database)?;
        let assets = assets::observation(
            row,
            positions.get(&asset_id).map(Vec::as_slice).unwrap_or(&[]),
        )?;
        result.insert(row.get("account_id").map_err(database)?, json!({
            "assets": assets, "created_at": row.get::<_, String>("created_at").map_err(database)?
        }));
    }
    Ok(result)
}

pub fn dashboard_inputs(
    connection: &Connection,
    account_ids: &[i64],
    logic_version: &str,
    engine_version: &str,
) -> Result<Value, String> {
    if account_ids.is_empty() {
        return Ok(json!({}));
    }
    let ids = json!(account_ids).to_string();
    let cte = "WITH ids AS (SELECT DISTINCT value AS id FROM json_each(?1)),
        bindings AS (SELECT b.* FROM portfolioaccount b JOIN
            (SELECT account_id,max(id) AS id FROM portfolioaccount
             WHERE account_id IN (SELECT id FROM ids) GROUP BY account_id) latest ON b.id=latest.id),
        observations AS (SELECT s.* FROM account_asset_snapshot s JOIN
            (SELECT s.account_id,max(s.id) AS id FROM account_asset_snapshot s
             JOIN business_asset_snapshot a ON a.asset_id=s.asset_fact_id
             WHERE s.account_id IN (SELECT id FROM ids) AND a.observation_valid IS TRUE
             GROUP BY s.account_id) latest ON s.id=latest.id),
        targets AS (SELECT t.* FROM target_weight_snapshot t JOIN
            (SELECT t.account_id,max(t.id) AS id FROM target_weight_snapshot t
             JOIN bindings b ON b.account_id=t.account_id AND b.portfolio_id=t.portfolio_id
             WHERE t.normalized_weights_kind NOT IN ('missing','null')
             GROUP BY t.account_id) latest ON t.id=latest.id)";
    let mut observations = observations(connection, cte, &ids)?;
    let mut targets = targets(connection, cte, &ids)?;
    let performance =
        snapshots::summaries_for_accounts(connection, account_ids, logic_version, engine_version)?;
    let mut statement = connection
        .prepare(&format!(
            "{cte},
        recent AS (SELECT r.* FROM executerecord r JOIN
            (SELECT account_id,max(id) AS id FROM executerecord
             WHERE account_id IN (SELECT id FROM ids) GROUP BY account_id) latest ON r.id=latest.id)
        SELECT ids.id,b.portfolio_id,r.id,r.created_at,r.is_success,r.display_status
        FROM ids LEFT JOIN bindings b ON b.account_id=ids.id
        LEFT JOIN recent r ON r.account_id=ids.id"
        ))
        .map_err(database)?;
    let mut rows = statement.query([&ids]).map_err(database)?;
    let mut result = Map::new();
    while let Some(row) = rows.next().map_err(database)? {
        let id: i64 = row.get(0).map_err(database)?;
        let latest = if row.get::<_, Option<i64>>(2).map_err(database)?.is_some() {
            let status: Option<String> = row.get(5).map_err(database)?;
            json!({"created_at":row.get::<_, String>(3).map_err(database)?,
                "is_success":row.get::<_, i64>(4).map_err(database)?,
                "display_status":status.filter(|s| !s.trim().is_empty()).map(|s| s.trim().to_owned())})
        } else {
            Value::Null
        };
        result.insert(
            id.to_string(),
            json!({
                "portfolio_id":row.get::<_, Option<i64>>(1).map_err(database)?,
                "latest":latest, "observation":observations.remove(&id),
                "target_weights":targets.remove(&id),
                "performance":performance.get(id.to_string()).cloned().unwrap_or_else(|| json!({
                    "snapshot_id":null,"status":"empty","computed_at":null,"observed_at":null,
                    "account_equity":null,"account_daily_return":null,"points":[]
                }))
            }),
        );
    }
    Ok(Value::Object(result))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn one_snapshot_keeps_binding_and_target_together_during_publication() {
        let path = std::env::temp_dir().join(format!(
            "axile-dashboard-snapshot-{}.sqlite",
            std::process::id()
        ));
        let reader = Connection::open(&path).unwrap();
        reader.execute_batch("PRAGMA journal_mode=WAL;
            CREATE TABLE portfolioaccount(id INTEGER,account_id INTEGER,portfolio_id INTEGER);
            CREATE TABLE account_asset_snapshot(id INTEGER,account_id INTEGER,asset_fact_id TEXT,created_at TEXT,source TEXT,execution_id TEXT);
            CREATE TABLE business_asset_snapshot(asset_id TEXT,observation_valid INTEGER,account_snapshot_id INTEGER,record_id INTEGER,input_id TEXT,path TEXT,scope TEXT);
            CREATE TABLE business_asset_position(asset_id TEXT,ordinal INTEGER);
            CREATE TABLE business_asset_binding(asset_id TEXT,record_id INTEGER,path TEXT);
            CREATE TABLE executerecord(id INTEGER,account_id INTEGER,execution_id TEXT,input_id TEXT,result_input_id TEXT,created_at TEXT,is_success INTEGER,display_status TEXT);
            CREATE TABLE execution_input(id TEXT,account_id INTEGER,owner_execution_id TEXT,execution_id TEXT);
            CREATE TABLE target_weight_snapshot(id INTEGER,account_id INTEGER,portfolio_id INTEGER,normalized_weights_kind TEXT,weights_metadata TEXT);
            CREATE TABLE target_weight_snapshot_weight(snapshot_id INTEGER,role TEXT,map_key TEXT,value REAL);
            CREATE TABLE account_analysis(account_id INTEGER,source_version INTEGER,published_version INTEGER,requested INTEGER,running_version INTEGER,error TEXT,current_snapshot TEXT);
            CREATE TABLE performance_snapshot(id TEXT,computed_at TEXT,logic_version TEXT,engine_version TEXT,result_schema_version INTEGER,ranges TEXT);
            CREATE TABLE performance_result_point(snapshot_id TEXT,range_key TEXT,idx INTEGER,date TEXT,observed_at TEXT,record_id INTEGER,execution_id TEXT,account_return REAL,account_equity REAL,portfolio_return REAL,account_daily_return REAL,portfolio_daily_return REAL,target_portfolio_return REAL,target_portfolio_daily_return REAL,difference REAL,sizing_fallback INTEGER);
            INSERT INTO portfolioaccount VALUES(1,1,7);
            INSERT INTO target_weight_snapshot VALUES(1,1,7,'mapping','{}');
            BEGIN;").unwrap();
        assert_eq!(
            dashboard_inputs(&reader, &[1], "l", "e").unwrap()["1"]["target_weights"],
            json!({})
        );
        let writer = Connection::open(&path).unwrap();
        writer
            .execute_batch("BEGIN; INSERT INTO portfolioaccount VALUES(2,1,NULL),(3,2,8); COMMIT;")
            .unwrap();
        let old = dashboard_inputs(&reader, &[1], "l", "e").unwrap();
        assert_eq!(old["1"]["portfolio_id"], 7);
        assert_eq!(old["1"]["target_weights"], json!({}));
        reader.execute_batch("COMMIT").unwrap();
        let new = dashboard_inputs(&reader, &[1], "l", "e").unwrap();
        assert_eq!(new["1"]["portfolio_id"], Value::Null);
        assert_eq!(new["1"]["target_weights"], Value::Null);
        let subset = dashboard_inputs(&reader, &[2], "l", "e").unwrap();
        assert_eq!(subset.as_object().unwrap().len(), 1);
        assert_eq!(subset["2"]["portfolio_id"], 8);
        assert!(subset.get("1").is_none());
        drop(writer);
        drop(reader);
        std::fs::remove_file(path).unwrap();
    }

    #[test]
    fn empty_accounts_need_no_schema() {
        assert_eq!(
            dashboard_inputs(&Connection::open_in_memory().unwrap(), &[], "l", "e").unwrap(),
            json!({})
        );
    }

    #[test]
    fn target_batch_restores_invalid_values_and_empty_mappings() {
        let connection = Connection::open_in_memory().unwrap();
        connection.execute_batch("CREATE TABLE target_weight_snapshot
            (id INTEGER,account_id INTEGER,normalized_weights_kind TEXT,weights_metadata TEXT);
            CREATE TABLE target_weight_snapshot_weight (snapshot_id INTEGER,role TEXT,map_key TEXT,value REAL);
            INSERT INTO target_weight_snapshot VALUES
            (1,1,'mapping','{}'),(2,2,'invalid','{\"normalized_weights\":{\"columns\":{\"value\":[]}}}'),
            (3,3,'mapping','{}');
            INSERT INTO target_weight_snapshot_weight VALUES (3,'normalized_weights','Z',NULL),
            (3,'normalized_weights','A',0.5),(3,'raw_weights','excluded',1);").unwrap();
        let cte = "WITH targets AS (SELECT * FROM target_weight_snapshot WHERE account_id IN (SELECT value FROM json_each(?1)))";
        let values = targets(&connection, cte, "[1,2,3]").unwrap();
        assert_eq!(values[&1], json!({}));
        assert_eq!(values[&2], json!([]));
        assert_eq!(values[&3], json!({"A":0.5,"Z":null}));
        connection
            .execute(
                "UPDATE target_weight_snapshot SET weights_metadata='{}' WHERE id=2",
                [],
            )
            .unwrap();
        assert!(targets(&connection, cte, "[2]")
            .unwrap_err()
            .starts_with("INVALID_DATA:"));
    }
}
