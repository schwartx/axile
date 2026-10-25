//! 公共和发行版私有迁移共用一个 SQLite 事务。

use std::path::Path;
use std::time::Duration;

use rusqlite::{Connection, OpenFlags, TransactionBehavior};
use serde_json::{json, Value};

use crate::error::AnalysisError;

mod legacy;
mod private;
mod structured;

pub const PUBLIC_HEAD: &str = "0041";
pub const PRIVATE_HEAD: &str = "axon_0001";

const PUBLIC_VERSION_TABLE: &str = "alembic_version";
const PRIVATE_VERSION_TABLE: &str = "axon_alembic_version";
const PUBLIC_BACKUP_TABLE: &str = "axile_0013_error_backup";
const PRIVATE_BACKUP_TABLE: &str = "axon_0001_error_backup";
const LEGACY_HEAD: u32 = 33;
const PUBLIC_HEAD_NUMBER: u32 = 41;

fn invalid(message: impl Into<String>) -> AnalysisError {
    AnalysisError::new("MIGRATION_VALUE", message)
}

fn sql(error: rusqlite::Error) -> AnalysisError {
    AnalysisError::new("MIGRATION_SQL", error.to_string())
}

#[derive(Clone, Copy, Debug, Default)]
struct State {
    public: u32,
    private: bool,
}

struct Policy<'a> {
    codes: Option<&'a Value>,
}

#[derive(Clone, Copy)]
struct Targets {
    public: u32,
    private: Option<bool>,
}

impl<'a> Policy<'a> {
    fn parse(request: &'a Value) -> Result<Self, AnalysisError> {
        if !request.is_object() && !request.is_null() {
            return Err(invalid("migration request must be an object"));
        }
        let Some(profile) = request
            .get("private_rules")
            .filter(|value| !value.is_null())
        else {
            return Ok(Self { codes: None });
        };
        for (field, expected) in [
            ("revision", PRIVATE_HEAD),
            ("version_table", PRIVATE_VERSION_TABLE),
            ("backup_table", PRIVATE_BACKUP_TABLE),
            ("dependency", "0013"),
        ] {
            if profile.get(field).and_then(Value::as_str) != Some(expected) {
                return Err(invalid(format!("unsupported private migration {field}")));
            }
        }
        let codes = profile
            .get("codes")
            .filter(|value| value.is_object())
            .ok_or_else(|| invalid("private migration codes must be an object"))?;
        for mappings in codes.as_object().expect("checked object").values() {
            let mappings = mappings
                .as_object()
                .ok_or_else(|| invalid("private migration channel codes must be an object"))?;
            if mappings.values().any(|message| !message.is_string()) {
                return Err(invalid("private migration messages must be strings"));
            }
        }
        Ok(Self { codes: Some(codes) })
    }

    fn targets(&self, request: &Value, explicit: bool) -> Result<Targets, AnalysisError> {
        let public = target_string(request, "public", explicit)?;
        let public = match public {
            "head" => PUBLIC_HEAD_NUMBER,
            "base" if self.codes.is_none() => 0,
            revision => revision_number(revision).ok_or_else(|| self.target_error())?,
        };
        if self.codes.is_some() && public < 12 {
            return Err(self.target_error());
        }
        let private = if self.codes.is_some() {
            Some(match target_string(request, "private", explicit)? {
                "head" | PRIVATE_HEAD => true,
                "base" => false,
                _ => return Err(self.target_error()),
            })
        } else {
            if request.get("private").is_some_and(|value| !value.is_null()) {
                return Err(invalid("private migration targets require private_rules"));
            }
            None
        };
        if private == Some(true) && public < 13 {
            return Err(invalid("axon_0001 requires public 0013 or later"));
        }
        Ok(Targets { public, private })
    }

    fn target_error(&self) -> AnalysisError {
        if self.codes.is_some() {
            invalid("supported targets: public 0012 through 0033 or 0041, private base/axon_0001")
        } else {
            invalid("supported targets: public base/0001 through 0033 or 0041")
        }
    }
}

fn target_string<'a>(
    request: &'a Value,
    field: &str,
    explicit: bool,
) -> Result<&'a str, AnalysisError> {
    match request.get(field) {
        Some(value) => value
            .as_str()
            .ok_or_else(|| invalid(format!("migration {field} target must be a string"))),
        None if !explicit => Ok("head"),
        None => Err(invalid(format!(
            "downgrade requires an explicit {field} target"
        ))),
    }
}

fn revision_number(revision: &str) -> Option<u32> {
    if revision.len() != 4 || !revision.bytes().all(|byte| byte.is_ascii_digit()) {
        return None;
    }
    let number = revision.parse::<u32>().ok()?;
    ((1..=LEGACY_HEAD).contains(&number) || number == PUBLIC_HEAD_NUMBER).then_some(number)
}

fn database_path(path: &str) -> Result<&Path, AnalysisError> {
    if path.is_empty()
        || path == ":memory:"
        || path.starts_with("file:")
        || path.starts_with("sqlite:")
        || path.starts_with("sqlite+")
    {
        return Err(invalid("migration commands require a SQLite file database"));
    }
    Ok(Path::new(path))
}

fn configure(connection: &Connection) -> Result<(), AnalysisError> {
    connection
        .busy_timeout(Duration::from_secs(30))
        .map_err(sql)?;
    let mode: String = connection
        .query_row("PRAGMA journal_mode=WAL", [], |row| row.get(0))
        .map_err(sql)?;
    if !mode.eq_ignore_ascii_case("wal") {
        return Err(AnalysisError::new(
            "MIGRATION_SQL",
            format!("migration requires WAL journal mode, got {mode}"),
        ));
    }
    // 历史重建表沿用旧迁移关闭外键的连接配置。
    connection
        .execute_batch("PRAGMA foreign_keys=OFF; PRAGMA synchronous=FULL;")
        .map_err(sql)
}

fn read_scope<T>(
    path: &str,
    work: impl FnOnce(Option<&Connection>) -> Result<T, AnalysisError>,
) -> Result<T, AnalysisError> {
    let database = database_path(path)?;
    if !database
        .try_exists()
        .map_err(|error| AnalysisError::new("DATABASE_OPEN", error.to_string()))?
    {
        return work(None);
    }
    let mut connection = crate::storage_connection::open_reader(database)?;
    let transaction = connection
        .transaction_with_behavior(TransactionBehavior::Deferred)
        .map_err(sql)?;
    let result = work(Some(&transaction));
    let close = transaction.rollback().map_err(sql);
    match result {
        Ok(value) => close.map(|_| value),
        Err(error) => Err(error),
    }
}

fn write_scope<T>(
    path: &str,
    create: bool,
    work: impl FnOnce(&Connection) -> Result<T, AnalysisError>,
) -> Result<T, AnalysisError> {
    let database = database_path(path)?;
    let _migration = crate::storage_connection::migration_guard(database)?;
    let mut flags = OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_NO_MUTEX;
    if create {
        flags |= OpenFlags::SQLITE_OPEN_CREATE;
    }
    let mut connection = Connection::open_with_flags(database, flags)
        .map_err(|error| AnalysisError::new("DATABASE_OPEN", error.to_string()))?;
    configure(&connection)?;
    let transaction = connection
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(sql)?;
    let value = work(&transaction)?;
    transaction.commit().map_err(sql)?;
    Ok(value)
}

fn version(
    connection: &Connection,
    table: &str,
    chain: &str,
) -> Result<Option<String>, AnalysisError> {
    let exists: bool = connection
        .query_row(
            "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name=?1)",
            [table],
            |row| row.get(0),
        )
        .map_err(sql)?;
    if !exists {
        return Ok(None);
    }
    let mut statement = connection
        .prepare(&format!("SELECT version_num FROM \"{table}\""))
        .map_err(sql)?;
    let heads = statement
        .query_map([], |row| row.get::<_, String>(0))
        .map_err(sql)?
        .collect::<Result<Vec<_>, _>>()
        .map_err(sql)?;
    if heads.len() > 1 {
        return Err(invalid(format!("multiple {chain} migration heads")));
    }
    Ok(heads.into_iter().next())
}

fn state(connection: Option<&Connection>) -> Result<State, AnalysisError> {
    let Some(connection) = connection else {
        return Ok(State::default());
    };
    let public = match version(connection, PUBLIC_VERSION_TABLE, "public")? {
        None => 0,
        Some(revision) => {
            revision_number(&revision).ok_or_else(|| invalid("unknown database revision"))?
        }
    };
    let private = match version(connection, PRIVATE_VERSION_TABLE, "private")?.as_deref() {
        None => false,
        Some(PRIVATE_HEAD) => true,
        _ => return Err(invalid("unknown database revision")),
    };
    if private && public < 13 {
        return Err(invalid("axon_0001 requires public 0013 or later"));
    }
    Ok(State { public, private })
}

fn stamp(
    connection: &Connection,
    table: &str,
    revision: Option<&str>,
) -> Result<(), AnalysisError> {
    connection
        .execute_batch(&format!(
            "CREATE TABLE IF NOT EXISTS \"{table}\" (version_num VARCHAR(32) NOT NULL, \
             CONSTRAINT \"{table}_pkc\" PRIMARY KEY (version_num));"
        ))
        .map_err(sql)?;
    match revision {
        Some(revision) => {
            let updated = connection
                .execute(
                    &format!("UPDATE \"{table}\" SET version_num=?1"),
                    [revision],
                )
                .map_err(sql)?;
            if updated == 0 {
                connection
                    .execute(
                        &format!("INSERT INTO \"{table}\" (version_num) VALUES (?1)"),
                        [revision],
                    )
                    .map_err(sql)?;
            }
        }
        None => {
            connection
                .execute(&format!("DELETE FROM \"{table}\""), [])
                .map_err(sql)?;
        }
    }
    Ok(())
}

fn move_public(
    connection: &Connection,
    current: &mut State,
    target: u32,
    upgrade: bool,
) -> Result<(), AnalysisError> {
    while current.public != target {
        if upgrade && current.public == LEGACY_HEAD {
            structured::upgrade(connection)?;
            current.public = PUBLIC_HEAD_NUMBER;
        } else if !upgrade && current.public == PUBLIC_HEAD_NUMBER {
            structured::downgrade(connection)?;
            current.public = LEGACY_HEAD;
        } else {
            let revision = if upgrade { current.public + 1 } else { current.public };
            if upgrade { legacy::upgrade(connection, revision)?; } else { legacy::downgrade(connection, revision)?; }
            current.public = if upgrade { revision } else { revision - 1 };
        }
        let revision = (current.public != 0).then(|| format!("{:04}", current.public));
        stamp(connection, PUBLIC_VERSION_TABLE, revision.as_deref())?;
    }
    Ok(())
}

fn state_json(current: State) -> Value {
    json!({
        "public": (current.public != 0).then(|| format!("{:04}", current.public)),
        "private": current.private.then_some(PRIVATE_HEAD),
    })
}

fn result_json(current: State, targets: Targets) -> Value {
    let mut result = state_json(current);
    result["public_target"] = json!(if targets.public == 0 {
        "base".to_owned()
    } else {
        format!("{:04}", targets.public)
    });
    result["private_target"] = json!(targets.private.map(|installed| {
        if installed {
            PRIVATE_HEAD
        } else {
            "base"
        }
    }));
    result
}

/// 只读识别双链版本；缺失的数据库保持缺失。
pub fn migration_current(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let policy = Policy::parse(request)?;
    read_scope(path, |connection| {
        let mut result = result_json(
            state(connection)?,
            Targets {
                public: PUBLIC_HEAD_NUMBER,
                private: policy.codes.map(|_| true),
            },
        );
        result["dependency_satisfied"] = json!(true);
        Ok(result)
    })
}

/// 只读扫描错误字段补丁和无损恢复冲突。
pub fn migration_check(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let policy = Policy::parse(request)?;
    let targets = policy.targets(request, false)?;
    read_scope(path, |connection| {
        let current = state(connection)?;
        validate_dependency(current, targets)?;
        let mut result = result_json(current, targets);
        result["public_fields"] = json!(0);
        result["private_fields"] = json!(0);
        result["public_scan"] = json!({});
        result["private_scan"] = json!({});
        if let Some(connection) = connection.filter(|_| current.public >= 12) {
            scan(connection, current, targets, &policy, &mut result)?;
        }
        Ok(result)
    })
}

fn validate_dependency(current: State, targets: Targets) -> Result<(), AnalysisError> {
    if targets.private.unwrap_or(current.private) && targets.public < 13 {
        return Err(invalid("axon_0001 requires public 0013 or later"));
    }
    Ok(())
}

fn scan(
    connection: &Connection,
    current: State,
    targets: Targets,
    policy: &Policy<'_>,
    result: &mut Value,
) -> Result<(), AnalysisError> {
    if current.public > LEGACY_HEAD && targets.public <= LEGACY_HEAD {
        structured::preflight_downgrade(connection)?;
    }
    if current.private && targets.private == Some(false) {
        private::restore_errors(connection, PRIVATE_BACKUP_TABLE, true)?;
    }
    if current.public >= 13 && targets.public < 13 {
        private::restore_errors(connection, PUBLIC_BACKUP_TABLE, true)?;
    }
    if current.public == 12 && targets.public >= 13 {
        let patch = legacy::patch_errors(
            connection,
            PUBLIC_BACKUP_TABLE,
            &legacy::public_codes(),
            true,
        )?;
        result["public_fields"] = patch["fields"].clone();
        result["public_scan"] = patch["scan"].clone();
    }
    if !current.private && targets.private == Some(true) {
        let patch = private::patch_errors(
            connection,
            PRIVATE_BACKUP_TABLE,
            policy.codes.expect("private target requires rules"),
            true,
        )?;
        result["private_fields"] = patch["fields"].clone();
        result["private_scan"] = patch["scan"].clone();
    }
    Ok(())
}

/// 显式建库或升级；公共和私有链全部成功后才提交。
pub fn migration_upgrade(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let policy = Policy::parse(request)?;
    let targets = policy.targets(request, false)?;
    write_scope(path, true, |connection| {
        let mut current = state(Some(connection))?;
        validate_dependency(current, targets)?;
        if targets.public < current.public || current.private && targets.private == Some(false) {
            return Err(invalid("upgrade cannot downgrade a chain"));
        }
        if !current.private && targets.private == Some(true) {
            if current.public <= LEGACY_HEAD {
                move_public(
                    connection,
                    &mut current,
                    targets.public.min(LEGACY_HEAD),
                    true,
                )?;
            }
            private::patch_errors(
                connection,
                PRIVATE_BACKUP_TABLE,
                policy.codes.expect("private target requires rules"),
                false,
            )?;
            stamp(connection, PRIVATE_VERSION_TABLE, Some(PRIVATE_HEAD))?;
            current.private = true;
        }
        move_public(connection, &mut current, targets.public, true)?;
        let mut result = result_json(current, targets);
        result["dependency_satisfied"] = json!(true);
        Ok(result)
    })
}

/// 按明确目标降级，不能升级任一链或丢弃无法恢复的证据。
pub fn migration_downgrade(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let policy = Policy::parse(request)?;
    let targets = policy.targets(request, true)?;
    write_scope(path, false, |connection| {
        let mut current = state(Some(connection))?;
        validate_dependency(current, targets)?;
        let minimum = if policy.codes.is_some() { 12 } else { 1 };
        if current.public < minimum {
            return Err(invalid(if policy.codes.is_some() {
                "downgrade requires public revision 0012 through 0041"
            } else {
                "downgrade requires an existing public revision"
            }));
        }
        if targets.public > current.public || !current.private && targets.private == Some(true) {
            return Err(invalid("downgrade cannot upgrade a chain"));
        }
        if current.public > LEGACY_HEAD && targets.public <= LEGACY_HEAD {
            move_public(connection, &mut current, LEGACY_HEAD, false)?;
        }
        if current.private && targets.private == Some(false) {
            private::restore_errors(connection, PRIVATE_BACKUP_TABLE, false)?;
            stamp(connection, PRIVATE_VERSION_TABLE, None)?;
            current.private = false;
        }
        move_public(connection, &mut current, targets.public, false)?;
        let mut result = result_json(current, targets);
        result["dependency_satisfied"] = json!(true);
        Ok(result)
    })
}

#[cfg(test)]
mod tests {
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicU64, Ordering};

    use super::*;

    static NEXT_PATH: AtomicU64 = AtomicU64::new(0);

    struct DatabasePath(PathBuf);

    impl DatabasePath {
        fn new() -> Self {
            Self(std::env::temp_dir().join(format!(
                "axile-migration-test-{}-{}.db",
                std::process::id(),
                NEXT_PATH.fetch_add(1, Ordering::Relaxed)
            )))
        }

        fn as_str(&self) -> &str {
            self.0.to_str().unwrap()
        }
    }

    impl Drop for DatabasePath {
        fn drop(&mut self) {
            let _ = std::fs::remove_file(&self.0);
        }
    }

    fn dual_request(public: &str, private: &str) -> Value {
        json!({
            "public": public,
            "private": private,
            "private_rules": {
                "revision": PRIVATE_HEAD,
                "version_table": PRIVATE_VERSION_TABLE,
                "backup_table": PRIVATE_BACKUP_TABLE,
                "dependency": "0013",
                "codes": {},
            },
        })
    }

    #[test]
    fn readonly_commands_never_create_a_missing_database() {
        let path = DatabasePath::new();
        let request = dual_request("head", "head");
        let current = migration_current(path.as_str(), &request).unwrap();
        assert_eq!(current["public"], Value::Null);
        assert_eq!(current["public_target"], "0041");
        let checked = migration_check(path.as_str(), &request).unwrap();
        assert_eq!(checked["private_fields"], 0);
        assert!(!path.0.exists());
    }

    #[test]
    fn public_initial_revision_roundtrips_to_base_without_a_private_chain() {
        let path = DatabasePath::new();
        let upgraded = migration_upgrade(path.as_str(), &json!({"public": "0001"})).unwrap();
        assert_eq!(upgraded["public"], "0001");
        assert_eq!(upgraded["private"], Value::Null);
        let mode: String = Connection::open(path.as_str())
            .unwrap()
            .query_row("PRAGMA journal_mode", [], |row| row.get(0))
            .unwrap();
        assert_eq!(mode, "wal");
        let downgraded = migration_downgrade(path.as_str(), &json!({"public": "base"})).unwrap();
        assert_eq!(downgraded["public"], Value::Null);
        let connection = Connection::open(path.as_str()).unwrap();
        assert!(connection
            .prepare("SELECT version_num FROM alembic_version")
            .is_ok());
        assert!(connection
            .prepare("SELECT version_num FROM axon_alembic_version")
            .is_err());
    }

    #[test]
    fn private_stamp_failure_rolls_back_both_chains_and_backup_tables() {
        let path = DatabasePath::new();
        migration_upgrade(path.as_str(), &json!({"public": "0012"})).unwrap();
        write_scope(path.as_str(), false, |connection| {
            connection.execute_batch(
                "CREATE TABLE axon_alembic_version(version_num VARCHAR(32) NOT NULL PRIMARY KEY); \
                 CREATE TRIGGER reject_private_revision BEFORE INSERT ON axon_alembic_version \
                 BEGIN SELECT RAISE(ABORT,'injected private stamp failure'); END;",
            ).map_err(sql)
        })
        .unwrap();
        let failed = migration_upgrade(path.as_str(), &dual_request("head", "head"));
        assert!(failed
            .unwrap_err()
            .error
            .contains("injected private stamp failure"));
        let current = migration_current(path.as_str(), &json!({})).unwrap();
        assert_eq!(current["public"], "0012");
        assert_eq!(current["private"], Value::Null);
        let connection = Connection::open(path.as_str()).unwrap();
        let backups: i64 = connection
            .query_row(
                "SELECT count(*) FROM sqlite_master WHERE name IN \
             ('axile_0013_error_backup','axon_0001_error_backup')",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(backups, 0);
    }

    #[test]
    fn targets_preserve_public_and_private_chain_limits() {
        let request = json!({"public": "0001"});
        assert_eq!(
            Policy::parse(&request)
                .unwrap()
                .targets(&request, true)
                .unwrap()
                .public,
            1
        );
        let request = json!({"public": "base"});
        assert_eq!(
            Policy::parse(&request)
                .unwrap()
                .targets(&request, true)
                .unwrap()
                .public,
            0
        );
        for (public, private) in [("0011", "base"), ("0012", "head"), ("head", "invalid")] {
            let request = dual_request(public, private);
            assert!(Policy::parse(&request)
                .unwrap()
                .targets(&request, false)
                .is_err());
        }
        let request = dual_request("0041", "head");
        assert_eq!(
            Policy::parse(&request)
                .unwrap()
                .targets(&request, true)
                .unwrap()
                .public,
            41
        );
        assert!(Policy::parse(&json!({}))
            .unwrap()
            .targets(&json!({}), true)
            .is_err());
    }

    #[test]
    fn rejects_unknown_multiple_and_orphaned_heads() {
        let connection = Connection::open_in_memory().unwrap();
        connection
            .execute_batch(
                "CREATE TABLE alembic_version(version_num TEXT); \
             INSERT INTO alembic_version VALUES('0038'),('0039');",
            )
            .unwrap();
        assert!(state(Some(&connection))
            .unwrap_err()
            .error
            .contains("multiple public"));
        connection
            .execute_batch(
                "DELETE FROM alembic_version; INSERT INTO alembic_version VALUES('0042');",
            )
            .unwrap();
        assert!(state(Some(&connection))
            .unwrap_err()
            .error
            .contains("unknown database"));
        connection
            .execute_batch(
                "UPDATE alembic_version SET version_num='0012'; \
             CREATE TABLE axon_alembic_version(version_num TEXT); \
             INSERT INTO axon_alembic_version VALUES('axon_0001');",
            )
            .unwrap();
        assert!(state(Some(&connection))
            .unwrap_err()
            .error
            .contains("requires public 0013"));
    }

    #[test]
    fn failed_scope_rolls_back_schema_and_both_version_stamps() {
        let path = DatabasePath::new();
        let failed: Result<(), AnalysisError> = write_scope(path.as_str(), true, |connection| {
            assert_eq!(
                connection
                    .query_row("PRAGMA synchronous", [], |row| row.get::<_, i64>(0))
                    .unwrap(),
                2
            );
            assert_eq!(
                connection
                    .query_row("PRAGMA busy_timeout", [], |row| row.get::<_, i64>(0))
                    .unwrap(),
                30_000
            );
            connection
                .execute_batch("CREATE TABLE partial(value TEXT);")
                .map_err(sql)?;
            stamp(connection, PUBLIC_VERSION_TABLE, Some("0039"))?;
            stamp(connection, PRIVATE_VERSION_TABLE, Some(PRIVATE_HEAD))?;
            Err(invalid("injected migration failure"))
        });
        assert!(failed.is_err());
        let connection = Connection::open(path.as_str()).unwrap();
        let count: i64 = connection
            .query_row(
                "SELECT count(*) FROM sqlite_master WHERE type='table'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(count, 0);
    }

    #[test]
    fn stamps_keep_existing_version_tables_and_base_clears_only_rows() {
        let connection = Connection::open_in_memory().unwrap();
        connection.execute_batch(
            "CREATE TABLE alembic_version(version_num VARCHAR(32) NOT NULL PRIMARY KEY, preserved TEXT DEFAULT 'yes'); \
             INSERT INTO alembic_version(version_num) VALUES('0038');",
        ).unwrap();
        stamp(&connection, PUBLIC_VERSION_TABLE, Some("0041")).unwrap();
        assert_eq!(
            connection
                .query_row("SELECT preserved FROM alembic_version", [], |row| row
                    .get::<_, String>(0),)
                .unwrap(),
            "yes"
        );
        stamp(&connection, PUBLIC_VERSION_TABLE, None).unwrap();
        assert_eq!(
            version(&connection, PUBLIC_VERSION_TABLE, "public").unwrap(),
            None
        );
        assert!(connection
            .prepare("SELECT preserved FROM alembic_version")
            .is_ok());
    }

    #[test]
    fn read_scope_is_query_only_and_cannot_mutate_existing_database() {
        let path = DatabasePath::new();
        let connection = Connection::open(path.as_str()).unwrap();
        stamp(&connection, PUBLIC_VERSION_TABLE, Some("0041")).unwrap();
        let mode: String = connection
            .query_row("PRAGMA journal_mode=DELETE", [], |row| row.get(0))
            .unwrap();
        assert_eq!(mode, "delete");
        drop(connection);
        read_scope(path.as_str(), |connection| {
            let connection = connection.unwrap();
            assert!(connection
                .execute("DELETE FROM alembic_version", [])
                .is_err());
            assert_eq!(state(Some(connection))?.public, 41);
            Ok(())
        })
        .unwrap();
        migration_current(path.as_str(), &json!({})).unwrap();
        migration_check(path.as_str(), &json!({})).unwrap();
        let mode: String = Connection::open(path.as_str())
            .unwrap()
            .query_row("PRAGMA journal_mode", [], |row| row.get(0))
            .unwrap();
        assert_eq!(mode, "delete");
    }

    #[test]
    fn write_scope_converts_existing_delete_database_to_wal() {
        let path = DatabasePath::new();
        let connection = Connection::open(path.as_str()).unwrap();
        connection
            .execute_batch("CREATE TABLE preserved(value TEXT);")
            .unwrap();
        let mode: String = connection
            .query_row("PRAGMA journal_mode=DELETE", [], |row| row.get(0))
            .unwrap();
        assert_eq!(mode, "delete");
        drop(connection);
        write_scope(path.as_str(), false, |connection| {
            let mode: String = connection
                .query_row("PRAGMA journal_mode", [], |row| row.get(0))
                .map_err(sql)?;
            assert_eq!(mode, "wal");
            assert!(connection.prepare("SELECT * FROM preserved").is_ok());
            Ok(())
        })
        .unwrap();
        let mode: String = Connection::open(path.as_str())
            .unwrap()
            .query_row("PRAGMA journal_mode", [], |row| row.get(0))
            .unwrap();
        assert_eq!(mode, "wal");
    }

    #[test]
    fn write_configuration_rejects_a_non_wal_result() {
        let connection = Connection::open_in_memory().unwrap();
        let error = configure(&connection).unwrap_err();
        assert_eq!(error.code, "MIGRATION_SQL");
        assert!(error.error.contains("requires WAL journal mode"));
    }

    #[test]
    fn private_metadata_cannot_supply_sql_identifiers_or_non_string_codes() {
        let mut request = dual_request("head", "head");
        request["private_rules"]["backup_table"] = json!("x; DROP TABLE account;");
        assert!(Policy::parse(&request).is_err());
        let mut request = dual_request("head", "head");
        request["private_rules"]["codes"] = json!({"PRIVATE": {"code": 1}});
        assert!(Policy::parse(&request).is_err());
    }
}
