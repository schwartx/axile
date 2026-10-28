//! Three physical tables; temporary adapters keep existing typed result readers reusable.
use rusqlite::{
    params, params_from_iter,
    types::{Value as SqlValue, ValueRef},
    Connection, OptionalExtension,
};
use serde_json::{json, Map, Value};
pub(crate) type Result<T> = std::result::Result<T, String>;
pub(crate) fn sql(e: impl std::fmt::Display) -> String {
    e.to_string()
}
pub(crate) fn get(c: &Connection, key: &str) -> Result<Value> {
    c.query_row("SELECT value FROM state WHERE key=?1", [key], |r| {
        r.get::<_, String>(0)
    })
    .optional()
    .map_err(sql)?
    .map(|s| serde_json::from_str(&s).map_err(sql))
    .transpose()
    .map(|v| v.unwrap_or(Value::Null))
}
pub(crate) fn set(c: &Connection, key: &str, value: &Value) -> Result<()> {
    c.execute(
        "INSERT INTO state VALUES(?1,?2) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        params![key, value.to_string()],
    )
    .map_err(sql)?;
    Ok(())
}
pub(crate) fn transaction<T>(c: &Connection, f: impl FnOnce() -> Result<T>) -> Result<T> {
    c.execute_batch("BEGIN IMMEDIATE").map_err(sql)?;
    match f() {
        Ok(v) => {
            c.execute_batch("COMMIT").map_err(sql)?;
            Ok(v)
        }
        Err(e) => {
            let _ = c.execute_batch("ROLLBACK");
            Err(e)
        }
    }
}
#[derive(Clone)]
pub(crate) struct Schema {
    pub name: String,
    pub columns: Vec<String>,
    pub keys: Vec<String>,
}
pub(crate) fn result_schemas() -> Result<&'static [Schema]> {
    static SCHEMAS: std::sync::OnceLock<Vec<Schema>> = std::sync::OnceLock::new();
    if let Some(schemas) = SCHEMAS.get() {
        return Ok(schemas);
    }
    let schemas = schemas(&scratch()?)?;
    Ok(SCHEMAS.get_or_init(|| schemas))
}
pub(crate) fn schemas(c: &Connection) -> Result<Vec<Schema>> {
    let mut tables = Vec::new();
    for name in crate::staging::RESULT_TABLES {
        let columns = c
            .prepare(&format!("PRAGMA table_info({name})"))
            .map_err(sql)?
            .query_map([], |r| Ok((r.get::<_, String>(1)?, r.get::<_, i64>(5)?)))
            .map_err(sql)?
            .collect::<rusqlite::Result<Vec<_>>>()
            .map_err(sql)?;
        let mut keys = columns
            .iter()
            .filter(|(n, k)| *k > 0 && n != "snapshot_id")
            .map(|(n, k)| (*k, n.clone()))
            .collect::<Vec<_>>();
        keys.sort();
        tables.push(Schema {
            name: (*name).into(),
            columns: columns
                .iter()
                .filter(|(n, _)| n != "snapshot_id")
                .map(|(n, _)| n.clone())
                .collect(),
            keys: keys.into_iter().map(|(_, n)| n).collect(),
        });
    }
    Ok(tables)
}
pub(crate) fn scratch() -> Result<Connection> {
    let c = Connection::open_in_memory().map_err(sql)?;
    crate::calendar::register_time_function(&c).map_err(sql)?;
    c.execute_batch(include_str!(
        "../../storage/src/migrations/structured/analysis.sql"
    ))
    .map_err(sql)?;
    c.execute_batch(include_str!(
        "../../storage/src/migrations/contributions.sql"
    ))
    .map_err(sql)?;
    for t in crate::browser_fact_schema::TABLES {
        if t.name != "analysis_calendar_day" {
            c.execute_batch(&t.create_sql()).map_err(sql)?;
        }
    }
    let s = schemas(&c)?;
    c.execute_batch("ATTACH DATABASE ':memory:' AS native_stage")
        .map_err(sql)?;
    for t in s {
        c.execute_batch(&format!(
            "CREATE TABLE native_stage.{} AS SELECT {} FROM {} WHERE 0",
            t.name,
            t.columns.join(","),
            t.name
        ))
        .map_err(sql)?;
    }
    c.execute_batch("CREATE TABLE native_stage.contribution_seen(symbol TEXT,id TEXT,quantity REAL,amount REAL,fee REAL,currency TEXT,PRIMARY KEY(symbol,id))").map_err(sql)?;
    Ok(c)
}
pub(crate) fn load(c: &Connection, raw: &Value) -> Result<()> {
    for t in crate::browser_fact_schema::TABLES {
        if let Some(rows) = raw[t.name].as_array() {
            let names = t.names();
            let query = format!(
                "INSERT OR REPLACE INTO {}({}) VALUES({})",
                t.name,
                names.join(","),
                vec!["?"; names.len()].join(",")
            );
            let mut insert = c.prepare_cached(&query).map_err(sql)?;
            for row in rows {
                let values = row
                    .as_array()
                    .filter(|a| a.len() == names.len())
                    .ok_or("INVALID_FACT_PAGE: wrong columns")?;
                let args = values
                    .iter()
                    .map(|v| match v {
                        Value::Null => Ok(SqlValue::Null),
                        Value::String(s) => Ok(SqlValue::Text(s.clone())),
                        Value::Number(n) if n.is_i64() => {
                            Ok(SqlValue::Integer(n.as_i64().unwrap()))
                        }
                        Value::Number(n) => n
                            .as_f64()
                            .filter(|v| v.is_finite())
                            .map(SqlValue::Real)
                            .ok_or("Invalid fact number"),
                        _ => Err("Invalid fact scalar"),
                    })
                    .collect::<std::result::Result<Vec<_>, _>>()?;
                insert.execute(params_from_iter(args)).map_err(sql)?;
            }
        }
    }
    Ok(())
}
pub(crate) fn object(row: &rusqlite::Row<'_>, columns: &[String]) -> rusqlite::Result<Value> {
    let mut out = Map::new();
    for (i, name) in columns.iter().enumerate() {
        out.insert(
            name.clone(),
            match row.get_ref(i)? {
                ValueRef::Null => Value::Null,
                ValueRef::Integer(v) => json!(v),
                ValueRef::Real(v) => json!(v),
                ValueRef::Text(v) => json!(String::from_utf8_lossy(v)),
                _ => Value::Null,
            },
        );
    }
    Ok(Value::Object(out))
}
pub(crate) fn put(
    c: &Connection,
    kind: &str,
    range: &str,
    key: &str,
    version: i64,
    record: i64,
    time: &str,
    body: &Value,
) -> Result<()> {
    c.execute("INSERT INTO results(kind,range_key,key,version,record_id,time,body) VALUES(?1,?2,?3,?4,?5,?6,?7) ON CONFLICT(kind,range_key,key,version) DO UPDATE SET body=excluded.body,record_id=excluded.record_id,time=excluded.time",params![kind,range,key,version,record,time,if body.is_null(){None}else{Some(body.to_string())}]).map_err(sql)?;
    Ok(())
}
pub(crate) fn save_stage(
    c: &Connection,
    source: &Connection,
    schemas: &[Schema],
    version: i64,
    time: &str,
) -> Result<()> {
    for t in schemas {
        let query = format!(
            "SELECT {} FROM native_stage.{}",
            t.columns
                .iter()
                .map(|s| format!("\"{s}\""))
                .collect::<Vec<_>>()
                .join(","),
            t.name
        );
        let mut s = source.prepare(&query).map_err(sql)?;
        let mut rows = s.query([]).map_err(sql)?;
        while let Some(row) = rows.next().map_err(sql)? {
            let body = object(row, &t.columns).map_err(sql)?;
            let key = Value::Array(t.keys.iter().map(|k| body[k].clone()).collect()).to_string();
            put(
                c,
                &t.name,
                body["range_key"].as_str().unwrap_or(""),
                &key,
                version,
                body["record_id"].as_i64().unwrap_or(0),
                time,
                &body,
            )?;
        }
    }
    Ok(())
}
pub(crate) fn adapters(
    c: &Connection,
    schemas: &[Schema],
    version: i64,
    snapshot: &str,
    account: i64,
    settings: &Value,
    build: &str,
) -> Result<()> {
    c.execute_batch("DROP VIEW IF EXISTS temp.account; DROP VIEW IF EXISTS temp.performance_snapshot; DROP VIEW IF EXISTS temp.local_results").map_err(sql)?;
    c.execute_batch(&format!("CREATE TEMP VIEW account AS SELECT {account} AS id;CREATE TEMP VIEW local_results AS SELECT r.* FROM results r WHERE r.version<={version} AND r.body IS NOT NULL AND NOT EXISTS(SELECT 1 FROM results n WHERE n.kind=r.kind AND n.range_key=r.range_key AND n.key=r.key AND n.version<= {version} AND n.version>r.version)")).map_err(sql)?;
    // Values are bound into a temporary single-row snapshot rather than interpolated SQL.
    c.execute_batch("DROP TABLE IF EXISTS temp.browser_snapshot;CREATE TEMP TABLE browser_snapshot(id,account_id,source_version,logic_version,engine_version,computed_at,data_until,backtest_weight_type,backtest_fee_rate,result_schema_version,cost_schema_version,read_view_schema_version);CREATE TEMP VIEW performance_snapshot AS SELECT * FROM browser_snapshot").map_err(sql)?;
    c.execute(
        "INSERT INTO browser_snapshot VALUES(?1,?2,?3,?4,'equity-ledger-1',?5,?6,'cs',?7,2,1,1)",
        params![
            snapshot,
            account,
            settings["source_version"].as_i64().unwrap_or(0),
            build,
            settings["computed_at"].as_str(),
            settings["data_until"].as_str(),
            settings["settings"]["backtest_fee_rate"]
                .as_f64()
                .unwrap_or(0.0)
        ],
    )
    .map_err(sql)?;
    for t in schemas {
        c.execute_batch(&format!("DROP VIEW IF EXISTS temp.{}", t.name))
            .map_err(sql)?;
        let columns = t
            .columns
            .iter()
            .map(|n| format!("json_extract(r.body,'$.{n}') AS \"{n}\""))
            .collect::<Vec<_>>()
            .join(",");
        c.execute_batch(&format!("CREATE TEMP VIEW {} AS SELECT (SELECT id FROM browser_snapshot) AS snapshot_id,{columns} FROM local_results r WHERE r.kind='{}'",t.name,t.name)).map_err(sql)?;
    }
    Ok(())
}
pub(crate) fn remove(
    c: &Connection,
    version: i64,
    filter: &str,
    args: &[&dyn rusqlite::ToSql],
) -> Result<()> {
    let q=format!("INSERT INTO results(kind,range_key,key,version,record_id,time,body) SELECT kind,range_key,key,{version},record_id,time,NULL FROM local_results WHERE {filter} ON CONFLICT(kind,range_key,key,version) DO UPDATE SET body=NULL");
    c.execute(&q, args).map_err(sql)?;
    Ok(())
}
