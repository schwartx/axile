use std::collections::{BTreeMap, BTreeSet};

use super::*;
const PREFIX: &str = "business_facts_0034_";
#[derive(Clone)]
struct ForeignKey {
    parent: String,
    keys: Vec<(String, String)>,
    cascade: bool,
}
#[derive(Clone)]
struct Table {
    name: String,
    columns: BTreeSet<String>,
    foreign_keys: Vec<ForeignKey>,
}
type Registry = Vec<Table>;
fn registry(connection: &Connection) -> Result<Registry> {
    let mut tables = Vec::new();
    for name in schema::BUSINESS_TABLES {
        let columns = rows(
            connection,
            &format!("PRAGMA table_info({})", quote(name)),
            [],
        )?
        .iter()
        .map(|row| text(row, "name").map(str::to_owned))
        .collect::<Result<BTreeSet<_>>>()?;
        let mut groups: BTreeMap<i64, ForeignKey> = BTreeMap::new();
        for row in rows(
            connection,
            &format!("PRAGMA foreign_key_list({})", quote(name)),
            [],
        )? {
            let key = groups.entry(integer(&row, "id")?).or_insert(ForeignKey {
                parent: text(&row, "table")?.into(),
                keys: Vec::new(),
                cascade: value(&row, "on_delete") == &Value::from("CASCADE"),
            });
            key.keys
                .push((text(&row, "from")?.into(), text(&row, "to")?.into()));
        }
        tables.push(Table {
            name: (*name).into(),
            columns,
            foreign_keys: groups.into_values().collect(),
        });
    }
    Ok(tables)
}
fn column(reference: &str, name: &str) -> String {
    format!("{reference}.{}", quote(name))
}
fn direct_role(name: &str) -> Option<&'static str> {
    match name {
        "record_id" => Some("record"),
        "input_id" => Some("input"),
        "account_snapshot_id" => Some("snapshot"),
        "account_id" => Some("account"),
        _ => None,
    }
}
fn ownership(
    table: &Table,
    reference: &str,
    tables: &Registry,
    seen: &BTreeSet<String>,
) -> BTreeMap<&'static str, Vec<String>> {
    let mut owners: [(&str, Vec<String>); 6] = [
        ("record", Vec::new()),
        ("input", Vec::new()),
        ("snapshot", Vec::new()),
        ("account", Vec::new()),
        ("asset", Vec::new()),
        ("execution", Vec::new()),
    ];
    let mut owners = owners
        .iter_mut()
        .map(|(role, values)| (*role, std::mem::take(values)))
        .collect::<BTreeMap<_, _>>();
    for name in ["record_id", "input_id", "account_snapshot_id", "account_id"] {
        if table.columns.contains(name) {
            owners
                .get_mut(direct_role(name).unwrap())
                .unwrap()
                .push(column(reference, name));
        }
    }
    for (table_name, role) in [
        ("executerecord", "record"),
        ("execution_input", "input"),
        ("account_asset_snapshot", "snapshot"),
    ] {
        if table.name == table_name {
            owners.get_mut(role).unwrap().push(column(reference, "id"));
        }
    }
    if table.name == "target_weight_snapshot" {
        owners
            .get_mut("execution")
            .unwrap()
            .push(column(reference, "execution_id"));
    }
    if table.columns.contains("asset_id") {
        owners
            .get_mut("asset")
            .unwrap()
            .push(column(reference, "asset_id"));
    }
    if seen.contains(&table.name) {
        return owners;
    }
    for constraint in &table.foreign_keys {
        if constraint
            .keys
            .iter()
            .all(|(name, _)| direct_role(name).is_some())
        {
            continue;
        }
        let Some(parent) = tables
            .iter()
            .find(|parent| parent.name == constraint.parent)
        else {
            continue;
        };
        let alias = format!("parent_{}", seen.len());
        let mut next_seen = seen.clone();
        next_seen.insert(table.name.clone());
        let inherited = ownership(parent, &alias, tables, &next_seen);
        let predicate = constraint
            .keys
            .iter()
            .map(|(local, remote)| {
                format!("{} IS {}", column(&alias, remote), column(reference, local))
            })
            .collect::<Vec<_>>()
            .join(" AND ");
        for (role, expressions) in inherited {
            for expression in expressions {
                owners.get_mut(role).unwrap().push(format!(
                    "(SELECT {expression} FROM {} AS {alias} WHERE {predicate})",
                    quote(&parent.name)
                ));
            }
        }
    }
    owners
}
fn joined(queries: Vec<String>) -> String {
    if queries.is_empty() {
        "SELECT NULL WHERE 0".into()
    } else {
        queries.join(" UNION ")
    }
}
fn scopes(table: &Table, reference: &str, tables: &Registry) -> (String, String) {
    let owners = ownership(table, reference, tables, &BTreeSet::new());
    let mut records = owners["record"]
        .iter()
        .map(|item| format!("SELECT {item} WHERE {item} IS NOT NULL"))
        .collect::<Vec<_>>();
    records.extend(owners["input"].iter().map(|item|format!("SELECT id FROM executerecord WHERE {item} IS NOT NULL AND (input_id IS {item} OR result_input_id IS {item})")));
    records.extend(owners["snapshot"].iter().map(|item|format!("SELECT e.id FROM executerecord AS e JOIN account_asset_snapshot AS s ON e.execution_id=s.execution_id AND e.account_id=s.account_id WHERE s.id IS {item} AND s.execution_id IS NOT NULL")));
    if table.name == "account_asset_snapshot" {
        let execution = column(reference, "execution_id");
        records.push(format!("SELECT id FROM executerecord WHERE execution_id IS {execution} AND {execution} IS NOT NULL AND account_id IS {}",column(reference,"account_id")));
    }
    records.extend(owners["execution"].iter().map(|item| {
        format!("SELECT id FROM executerecord WHERE execution_id IS {item} AND {item} IS NOT NULL")
    }));
    records.extend(owners["asset"].iter().map(|item| {
        format!("SELECT record_id FROM business_asset_binding WHERE asset_id IS {item}")
    }));
    let mut accounts = owners["account"]
        .iter()
        .map(|item| format!("SELECT id FROM account WHERE id IS {item}"))
        .collect::<Vec<_>>();
    accounts.extend(
        owners["record"]
            .iter()
            .map(|item| format!("SELECT account_id FROM executerecord WHERE id IS {item}")),
    );
    accounts.extend(
        owners["input"]
            .iter()
            .map(|item| format!("SELECT account_id FROM execution_input WHERE id IS {item}")),
    );
    accounts.extend(
        owners["snapshot"].iter().map(|item| {
            format!("SELECT account_id FROM account_asset_snapshot WHERE id IS {item}")
        }),
    );
    accounts.extend(owners["asset"].iter().map(|item|format!("SELECT account_id FROM executerecord WHERE id IN (SELECT record_id FROM business_asset_binding WHERE asset_id IS {item})")));
    (joined(records), joined(accounts))
}
fn install_invalidation(connection: &Connection, tables: &Registry) -> Result<()> {
    for table in tables.iter().filter(|table| table.name != "executerecord") {
        for action in ["INSERT", "UPDATE", "DELETE"] {
            let refs = if action == "UPDATE" {
                vec!["OLD", "NEW"]
            } else if action == "DELETE" {
                vec!["OLD"]
            } else {
                vec!["NEW"]
            };
            let scopes = refs
                .iter()
                .map(|reference| scopes(table, reference, tables))
                .collect::<Vec<_>>();
            let records = scopes
                .iter()
                .map(|scope| scope.0.clone())
                .collect::<Vec<_>>()
                .join(" UNION ");
            let accounts = scopes
                .iter()
                .map(|scope| scope.1.clone())
                .collect::<Vec<_>>()
                .join(" UNION ");
            let body = format!(" INSERT INTO account_analysis(account_id,source_version) SELECT id,1 FROM account WHERE id IN ({accounts}) AND 1 ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL THEN 1 ELSE requested END,failures=0,retry_at=0,error=NULL;");
            let condition = if action == "INSERT"
                && ![
                    "executerecord",
                    "account_asset_snapshot",
                    "target_weight_snapshot",
                ]
                .contains(&table.name.as_str())
            {
                let ready=format!("EXISTS(SELECT 1 FROM executerecord WHERE facts_complete=1 AND id IN ({records}))");
                if table.name == "business_asset_snapshot" {
                    format!(" WHEN (NEW.account_snapshot_id IS NOT NULL OR {ready})")
                } else {
                    format!(" WHEN {ready}")
                }
            } else {
                String::new()
            };
            execute(connection,&format!("CREATE TRIGGER IF NOT EXISTS {} AFTER {action} ON {}{condition} BEGIN {body} END",quote(&format!("{PREFIX}{}_{}",table.name,action.to_lowercase())),quote(&table.name)))?;
        }
    }
    Ok(())
}
fn input_guards(connection: &Connection) -> Result<()> {
    for action in ["INSERT", "UPDATE"] {
        let checks=["input_id","result_input_id"].iter().map(|name|format!("SELECT RAISE(ABORT,'Execution input owner mismatch') WHERE NEW.{name} IS NOT NULL AND NOT EXISTS(SELECT 1 FROM execution_input WHERE id=NEW.{name} AND account_id=NEW.account_id);")).collect::<Vec<_>>().join(" ");
        execute(connection,&format!("CREATE TRIGGER IF NOT EXISTS {PREFIX}record_input_{} BEFORE {action} ON executerecord BEGIN {checks} END",action.to_lowercase()))?;
    }
    execute(connection,&format!("CREATE TRIGGER IF NOT EXISTS {PREFIX}input_owner_update BEFORE UPDATE OF account_id,id ON execution_input BEGIN SELECT RAISE(ABORT,'Execution input owner mismatch') WHERE NEW.account_id IS NOT OLD.account_id OR NEW.id IS NOT OLD.id; END; CREATE TRIGGER IF NOT EXISTS {PREFIX}input_delete_guard BEFORE DELETE ON execution_input BEGIN SELECT RAISE(ABORT,'Execution input is still referenced') WHERE EXISTS(SELECT 1 FROM executerecord WHERE (input_id=OLD.id OR result_input_id=OLD.id) AND EXISTS(SELECT 1 FROM account WHERE id=executerecord.account_id)); END;"))
}
fn parent_guards(connection: &Connection, tables: &Registry) -> Result<()> {
    for table in tables {
        for constraint in &table.foreign_keys {
            if !tables.iter().any(|parent| parent.name == constraint.parent) {
                continue;
            }
            let present = constraint
                .keys
                .iter()
                .map(|(local, _)| format!("{} IS NOT NULL", column("NEW", local)))
                .collect::<Vec<_>>()
                .join(" AND ");
            let predicate = constraint
                .keys
                .iter()
                .map(|(local, remote)| format!("{} IS {}", quote(remote), column("NEW", local)))
                .collect::<Vec<_>>()
                .join(" AND ");
            let suffix = constraint
                .keys
                .iter()
                .map(|(local, _)| local.clone())
                .collect::<Vec<_>>()
                .join("_");
            for action in ["INSERT", "UPDATE"] {
                execute(connection,&format!("CREATE TRIGGER IF NOT EXISTS {} BEFORE {action} ON {} BEGIN SELECT RAISE(ABORT,'Business fact owner missing') WHERE {present} AND NOT EXISTS(SELECT 1 FROM {} WHERE {predicate}); END",quote(&format!("{PREFIX}{}_owner_{suffix}_{}",table.name,action.to_lowercase())),quote(&table.name),quote(&constraint.parent)))?;
            }
        }
    }
    Ok(())
}
fn asset_guards(connection: &Connection) -> Result<()> {
    let owner="SELECT COALESCE(r.account_id,i.account_id,s.account_id) FROM business_asset_snapshot AS a LEFT JOIN executerecord AS r ON r.id=a.record_id LEFT JOIN execution_input AS i ON i.id=a.input_id LEFT JOIN account_asset_snapshot AS s ON s.id=a.account_snapshot_id WHERE a.asset_id=NEW.asset_id";
    for action in ["INSERT", "UPDATE"] {
        execute(connection,&format!("CREATE TRIGGER IF NOT EXISTS {PREFIX}asset_binding_owner_{} BEFORE {action} ON business_asset_binding BEGIN SELECT RAISE(ABORT,'Asset binding owner mismatch') WHERE ({owner}) IS NOT (SELECT account_id FROM executerecord WHERE id=NEW.record_id); END",action.to_lowercase()))?;
    }
    let new_owner="COALESCE((SELECT account_id FROM executerecord WHERE id=NEW.record_id),(SELECT account_id FROM execution_input WHERE id=NEW.input_id),(SELECT account_id FROM account_asset_snapshot WHERE id=NEW.account_snapshot_id))";
    execute(connection,&format!("CREATE TRIGGER IF NOT EXISTS {PREFIX}asset_owner_update BEFORE UPDATE ON business_asset_snapshot BEGIN SELECT RAISE(ABORT,'Asset identity is immutable') WHERE NEW.asset_id IS NOT OLD.asset_id; SELECT RAISE(ABORT,'Asset owner mismatch') WHERE EXISTS(SELECT 1 FROM business_asset_binding AS b JOIN executerecord AS r ON r.id=b.record_id WHERE b.asset_id=OLD.asset_id AND r.account_id IS NOT ({new_owner})) OR EXISTS(SELECT 1 FROM account_asset_snapshot AS s WHERE s.asset_fact_id=OLD.asset_id AND s.account_id IS NOT ({new_owner})); END;"))?;
    for action in ["INSERT", "UPDATE"] {
        execute(connection,&format!("CREATE TRIGGER IF NOT EXISTS {PREFIX}snapshot_asset_owner_{} BEFORE {action} ON account_asset_snapshot WHEN NEW.asset_fact_id IS NOT NULL BEGIN SELECT RAISE(ABORT,'Snapshot asset owner mismatch') WHERE ({}) IS NOT NEW.account_id; END",action.to_lowercase(),owner.replace("NEW.asset_id","NEW.asset_fact_id")))?;
    }
    let retained="EXISTS(SELECT 1 FROM account_asset_snapshot WHERE asset_fact_id=a.asset_id) OR EXISTS(SELECT 1 FROM execution_artifact WHERE CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.asset_id') END=a.asset_id)";
    execute(connection,&format!("CREATE TRIGGER IF NOT EXISTS {PREFIX}record_retain_assets BEFORE DELETE ON executerecord WHEN EXISTS(SELECT 1 FROM account WHERE id=OLD.account_id) BEGIN SELECT RAISE(ABORT,'Referenced asset requires a durable input owner') WHERE OLD.input_id IS NULL AND EXISTS(SELECT 1 FROM business_asset_snapshot AS a WHERE a.record_id=OLD.id AND ({retained})); UPDATE business_asset_snapshot SET record_id=NULL,input_id=OLD.input_id WHERE record_id=OLD.id AND ({}); END",retained.replace("a.","business_asset_snapshot.")))
}
fn cleanup(connection: &Connection, tables: &Registry) -> Result<()> {
    let deletes = tables
        .iter()
        .rev()
        .filter(|table| table.name != "executerecord" && table.columns.contains("record_id"))
        .map(|table| format!("DELETE FROM {} WHERE record_id=OLD.id;", quote(&table.name)))
        .collect::<Vec<_>>()
        .join(" ");
    let orphans="DELETE FROM execution_input WHERE id IN (OLD.input_id,OLD.result_input_id) AND NOT EXISTS(SELECT 1 FROM executerecord WHERE input_id=execution_input.id OR result_input_id=execution_input.id) AND NOT EXISTS(SELECT 1 FROM execution_artifact WHERE CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.input_id') END=execution_input.id) AND NOT EXISTS(SELECT 1 FROM execution_artifact WHERE CASE WHEN json_valid(content) THEN json_extract(content,'$.business_facts_ref.asset_id') END IN (SELECT asset_id FROM business_asset_snapshot WHERE input_id=execution_input.id)) AND NOT EXISTS(SELECT 1 FROM execution_event WHERE CASE WHEN json_valid(details) THEN json_extract(details,'$.decision.business_facts_ref.input_id') END=execution_input.id) AND NOT EXISTS(SELECT 1 FROM account_asset_snapshot WHERE asset_fact_id IN (SELECT asset_id FROM business_asset_snapshot WHERE input_id=execution_input.id));";
    execute(connection,&format!("CREATE TRIGGER IF NOT EXISTS {PREFIX}record_cleanup AFTER DELETE ON executerecord BEGIN {deletes} {orphans} END"))?;
    for table in tables {
        for parent in tables {
            if table.name == parent.name {
                continue;
            }
            let mut keys = table
                .foreign_keys
                .iter()
                .filter(|key| key.parent == parent.name && key.cascade)
                .flat_map(|constraint| constraint.keys.iter())
                .collect::<Vec<_>>();
            if keys.is_empty() {
                continue;
            }
            if table.name == "execution_order" && parent.name == "execution_order_collection" {
                // 冻结 0037 复合清理谓词的历史顺序。
                keys.reverse();
            }
            let predicate = keys
                .iter()
                .map(|(local, remote)| format!("{} IS {}", quote(local), column("OLD", remote)))
                .collect::<Vec<_>>()
                .join(" AND ");
            execute(connection,&format!("CREATE TRIGGER IF NOT EXISTS {} AFTER DELETE ON {} BEGIN DELETE FROM {} WHERE {predicate}; END",quote(&format!("{PREFIX}{}_cleanup_{}",parent.name,table.name)),quote(&parent.name),quote(&table.name)))?;
        }
    }
    execute(connection,&format!("CREATE TRIGGER IF NOT EXISTS {PREFIX}account_inputs_cleanup BEFORE DELETE ON account BEGIN DELETE FROM execution_artifact WHERE execution_id IN (SELECT execution_id FROM executerecord WHERE account_id=OLD.id UNION SELECT COALESCE(owner_execution_id,execution_id) FROM execution_input WHERE account_id=OLD.id UNION SELECT execution_id FROM execution_event WHERE account_id=OLD.id); DELETE FROM execution_event WHERE account_id=OLD.id; DELETE FROM account_asset_snapshot WHERE account_id=OLD.id; DELETE FROM executerecord WHERE account_id=OLD.id; DELETE FROM execution_input WHERE account_id=OLD.id; UPDATE target_weight_snapshot SET account_id=NULL WHERE account_id=OLD.id; END"))
}
pub(super) fn drop_business(connection: &Connection) -> Result<()> {
    for row in rows(
        connection,
        "SELECT name FROM sqlite_master WHERE type='trigger' AND name GLOB 'business_facts_0034_*'",
        [],
    )? {
        execute(
            connection,
            &format!("DROP TRIGGER IF EXISTS {}", quote(text(&row, "name")?)),
        )?;
    }
    Ok(())
}
pub(super) fn install_business(connection: &Connection) -> Result<()> {
    execute(connection,"DROP TRIGGER IF EXISTS analysis_facts_record_update; DROP TRIGGER IF EXISTS analysis_structured_record_update")?;
    let tables = registry(connection)?;
    install_invalidation(connection, &tables)?;
    input_guards(connection)?;
    parent_guards(connection, &tables)?;
    asset_guards(connection)?;
    cleanup(connection, &tables)
}

pub(super) fn install_analysis(connection: &Connection) -> Result<()> {
    execute(connection,"CREATE TRIGGER analysis_account_channel AFTER UPDATE OF trade_channel ON account WHEN OLD.trade_channel IS NOT NEW.trade_channel BEGIN INSERT INTO account_analysis(account_id,source_version) VALUES(NEW.id,1) ON CONFLICT(account_id) DO UPDATE SET source_version=source_version+1,requested=CASE WHEN failures>0 OR current_snapshot IS NOT NULL OR running_version IS NOT NULL THEN 1 ELSE requested END,failures=0,retry_at=0,error=NULL; END;")?;
    let deletes = schema::ANALYSIS_TABLES
        .iter()
        .filter(|name| !matches!(**name, "performance_snapshot" | "analysis_calendar_day"))
        .map(|name| format!("DELETE FROM {name} WHERE snapshot_id=OLD.id;"))
        .collect::<Vec<_>>()
        .join(" ");
    execute(connection, &format!("CREATE TRIGGER analysis_snapshot_delete AFTER DELETE ON performance_snapshot BEGIN {deletes} END"))
}

pub(super) fn install_incremental(c: &Connection) -> Result<()> {
    let tables = registry(c)?;
    for table in &tables {
        for (action, reference, timing) in [
            ("INSERT", "NEW", "AFTER"),
            ("UPDATE", "OLD", "BEFORE"),
            ("UPDATE_NEW", "NEW", "AFTER"),
            ("DELETE", "OLD", "BEFORE"),
        ] {
            let event = if action == "UPDATE_NEW" {
                "UPDATE"
            } else {
                action
            };
            let (records, _) = scopes(table, reference, &tables);
            let selection = if table.name == "executerecord" {
                format!("SELECT {reference}.account_id AS account_id,{reference}.id AS id")
            } else {
                format!("SELECT account_id,id FROM executerecord WHERE id IN ({records})")
            };
            execute(c,&format!("CREATE TRIGGER incremental_{}_{} {timing} {event} ON {} BEGIN INSERT INTO account_analysis(account_id,source_version,dirty_record) SELECT account_id,0,min(id) FROM ({selection}) WHERE account_id IS NOT NULL GROUP BY account_id ON CONFLICT(account_id) DO UPDATE SET dirty_record=CASE WHEN dirty_record IS NULL THEN excluded.dirty_record ELSE min(dirty_record,excluded.dirty_record) END; END",table.name,action.to_lowercase(),quote(&table.name)))?;
        }
    }
    Ok(())
}
