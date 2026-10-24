use super::*;

pub(super) fn upgrade(connection: &Connection) -> Result<()> {
    let mut highest = 0;
    for table in rows(
        connection,
        "SELECT name FROM sqlite_master WHERE type='table'",
        [],
    )? {
        let name = text(&table, "name")?;
        for column in rows(
            connection,
            &format!("PRAGMA table_info({})", quote(name)),
            [],
        )? {
            let column = text(&column, "name")?;
            if column == "account_id"
                || (name == "account" && matches!(column, "id" | "copied_from_account_id"))
            {
                let maximum: Option<i64> = connection
                    .query_row(
                        &format!("SELECT MAX({}) FROM {}", quote(column), quote(name)),
                        [],
                        |row| row.get(0),
                    )
                    .map_err(sql)?;
                highest = highest.max(maximum.unwrap_or_default());
            }
        }
    }
    rebuild(connection, true)?;
    // 旧库可能只剩已删账户的同步或审计行，不能将这些编号分给新账户。
    connection.execute("INSERT INTO sqlite_sequence(name,seq) SELECT 'account',?1 WHERE NOT EXISTS(SELECT 1 FROM sqlite_sequence WHERE name='account')", [highest]).map_err(sql)?;
    connection
        .execute(
            "UPDATE sqlite_sequence SET seq=MAX(seq,?1) WHERE name='account'",
            [highest],
        )
        .map_err(sql)?;
    Ok(())
}

pub(super) fn downgrade(connection: &Connection) -> Result<()> {
    rebuild(connection, false)
}

fn rebuild(connection: &Connection, autoincrement: bool) -> Result<()> {
    let definition = one(
        connection,
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='account'",
        [],
    )?;
    let definition = text(&definition, "sql")?;
    let ordinary = "PRIMARY KEY (id)";
    let monotonic = "PRIMARY KEY (id AUTOINCREMENT)";
    let (before, after) = if autoincrement {
        (ordinary, monotonic)
    } else {
        (monotonic, ordinary)
    };
    if !definition.contains(before) {
        return Err(error("Unexpected account primary key definition"));
    }
    let body = definition
        .find('(')
        .ok_or_else(|| error("Missing account table definition"))?;
    let objects = rows(connection, "SELECT sql FROM sqlite_master WHERE tbl_name='account' AND type IN ('index','trigger') AND sql IS NOT NULL ORDER BY type,name", [])?;
    // 保留其他表上的外键、触发器和视图对 account 的原名引用。
    execute(
        connection,
        &format!(
            "PRAGMA legacy_alter_table=ON; CREATE TABLE account_0040 {}; \
         INSERT INTO account_0040 SELECT * FROM account; DROP TABLE account; \
         ALTER TABLE account_0040 RENAME TO account; PRAGMA legacy_alter_table=OFF;",
            definition[body..].replacen(before, after, 1),
        ),
    )?;
    for object in objects {
        execute(connection, text(&object, "sql")?)?;
    }
    Ok(())
}
