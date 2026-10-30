//! 公共和发行版私有迁移共用一个 SQLite 事务。

use std::path::Path;
use std::time::Duration;

use rusqlite::{Connection, OpenFlags, TransactionBehavior};
use serde_json::{json, Value};

use crate::error::AnalysisError;

mod error_patch;
mod execution_views;
mod private;
mod structured;

pub const PUBLIC_HEAD: &str = "0034";
pub const PRIVATE_HEAD: &str = "axon_0001";

const PUBLIC_VERSION_TABLE: &str = "alembic_version";
const PRIVATE_VERSION_TABLE: &str = "axon_alembic_version";
const PRIVATE_BACKUP_TABLE: &str = "axon_0001_error_backup";
const BASELINE_REVISION: u32 = 33;
const PUBLIC_HEAD_NUMBER: u32 = 34;

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

            revision => revision_number(revision)
                .filter(|n| matches!(*n, BASELINE_REVISION | PUBLIC_HEAD_NUMBER))
                .ok_or_else(|| self.target_error())?,
        };
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
        Ok(Targets { public, private })
    }

    fn target_error(&self) -> AnalysisError {
        if self.codes.is_some() {
            invalid("supported targets: public 0033/0034, private base/axon_0001")
        } else {
            invalid("supported targets: public 0033/0034")
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
    (1..=9999).contains(&number).then_some(number)
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
    crate::calendar::register_time_function(&connection).map_err(sql)?;
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
        None => {
            let populated: bool = connection
                .query_row(
                    "SELECT EXISTS(SELECT 1 FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%')",
                    [],
                    |row| row.get(0),
                )
                .map_err(sql)?;
            if populated {
                return Err(invalid("unversioned nonempty database cannot be migrated"));
            }
            0
        }
        Some(revision) => {
            let number =
                revision_number(&revision).ok_or_else(|| invalid("unknown database revision"))?;
            if number < BASELINE_REVISION {
                return Err(invalid(format!("当前数据库版本为 {revision}，本版本仅支持从 0033 升级。请先安装 Axile v0.23.1 并完成数据库升级，再安装本版本。")));
            }
            if number > PUBLIC_HEAD_NUMBER {
                return Err(invalid(format!(
                    "unsupported development database revision: {revision}"
                )));
            }
            if number == PUBLIC_HEAD_NUMBER {
                validate_final_schema(connection)?;
            }
            number
        }
    };
    let private = match version(connection, PRIVATE_VERSION_TABLE, "private")?.as_deref() {
        None => false,
        Some(PRIVATE_HEAD) => true,
        _ => return Err(invalid("unknown database revision")),
    };
    if private && public < BASELINE_REVISION {
        return Err(invalid("axon_0001 requires public 0033 or later"));
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

fn validate_final_schema(connection: &Connection) -> Result<(), AnalysisError> {
    let final_layout: bool = connection.query_row(
        "SELECT EXISTS(SELECT 1 FROM sqlite_schema WHERE type='table' AND name='analysis_state')
        AND EXISTS(SELECT 1 FROM pragma_table_info('account_analysis') WHERE name='dirty_record')
        AND EXISTS(SELECT 1 FROM pragma_table_info('executerecord') WHERE name='result_staged')
        AND NOT EXISTS(SELECT 1 FROM pragma_table_info('executerecord') WHERE name IN ('raw_result','history_visible'))
        AND NOT EXISTS(SELECT 1 FROM sqlite_schema WHERE type='table' AND name='performance_snapshot')",
        [], |row| row.get(0),
    ).map_err(sql)?;
    if !final_layout {
        return Err(invalid("database revision 0034 has an unsupported development schema; use the final 0034 schema or a published 0033 database"));
    }
    Ok(())
}

fn move_public(
    connection: &Connection,
    current: &mut State,
    target: u32,
    upgrade: bool,
) -> Result<(), AnalysisError> {
    if current.public == target {
        return Ok(());
    }
    match (upgrade, current.public, target) {
        (true, 0, PUBLIC_HEAD_NUMBER) => structured::create(connection)?,
        (true, BASELINE_REVISION, PUBLIC_HEAD_NUMBER) => structured::upgrade(connection)?,
        (false, PUBLIC_HEAD_NUMBER, BASELINE_REVISION) => structured::downgrade(connection)?,
        _ => {
            return Err(invalid(
                "supported migration paths: empty/0033 to 0034, 0034 to 0033",
            ))
        }
    }
    current.public = target;
    stamp(
        connection,
        PUBLIC_VERSION_TABLE,
        Some(&format!("{target:04}")),
    )
}

fn state_json(current: State) -> Value {
    json!({
        "public": (current.public != 0).then(|| format!("{:04}", current.public)),
        "private": current.private.then_some(PRIVATE_HEAD),
    })
}

fn result_json(current: State, targets: Targets) -> Value {
    let mut result = state_json(current);
    result["public_target"] = json!(format!("{:04}", targets.public));
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

fn scan(
    connection: &Connection,
    current: State,
    targets: Targets,
    policy: &Policy<'_>,
    result: &mut Value,
) -> Result<(), AnalysisError> {
    if current.public > BASELINE_REVISION && targets.public <= BASELINE_REVISION {
        structured::preflight_downgrade(connection)?;
    }
    if current.private && targets.private == Some(false) {
        private::restore_errors(connection, PRIVATE_BACKUP_TABLE, true)?;
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
    read_scope(path, |connection| {
        let current = state(connection)?;
        if current.public == 0 && targets.public != PUBLIC_HEAD_NUMBER {
            return Err(invalid("empty databases can only be initialized at 0034"));
        }
        if current.public == BASELINE_REVISION && targets.public == PUBLIC_HEAD_NUMBER {
            structured::check_analysis_dependencies(
                connection.expect("existing baseline"),
                current.public,
            )?;
        }
        Ok(())
    })?;
    write_scope(path, true, |connection| {
        let mut current = state(Some(connection))?;
        if targets.public < current.public || current.private && targets.private == Some(false) {
            return Err(invalid("upgrade cannot downgrade a chain"));
        }
        // 写锁已阻止其他写入；独立读连接备份尚未修改的已提交数据与 WAL。
        let crosses_structured =
            current.public == BASELINE_REVISION && targets.public == PUBLIC_HEAD_NUMBER;
        let backup = if current.public > 0 && crosses_structured {
            let database = database_path(path)?;
            let backup = crate::maintenance_ops::backup_path(
                database,
                &json!({"backup_suffix":std::process::id().to_string()}),
                "before-0034",
            )?;
            crate::maintenance_ops::backup(database, &backup)?;
            Some(backup)
        } else {
            None
        };
        if current.public == 0 {
            move_public(connection, &mut current, targets.public, true)?;
        }
        if !current.private && targets.private == Some(true) {
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
        if crosses_structured {
            connection
                .execute_batch(include_str!("migrations/cleanup_backups.sql"))
                .map_err(sql)?;
        }
        let mut result = result_json(current, targets);
        result["dependency_satisfied"] = json!(true);
        if let Some(backup) = backup {
            result["backup"] = json!(backup);
        }
        Ok(result)
    })
}

/// 按明确目标降级，不能升级任一链或丢弃无法恢复的证据。
pub fn migration_downgrade(path: &str, request: &Value) -> Result<Value, AnalysisError> {
    let policy = Policy::parse(request)?;
    let targets = policy.targets(request, true)?;
    read_scope(path, |connection| {
        let current = state(connection)?;
        if current.public == PUBLIC_HEAD_NUMBER && targets.public == BASELINE_REVISION {
            structured::check_analysis_dependencies(
                connection.expect("existing final schema"),
                current.public,
            )?;
        }
        Ok(())
    })?;
    write_scope(path, false, |connection| {
        let mut current = state(Some(connection))?;
        let minimum = BASELINE_REVISION;
        if current.public < minimum {
            return Err(invalid(if policy.codes.is_some() {
                "downgrade requires public revision 0033/0034"
            } else {
                "downgrade requires an existing public revision"
            }));
        }
        if targets.public > current.public || !current.private && targets.private == Some(true) {
            return Err(invalid("downgrade cannot upgrade a chain"));
        }
        if current.private && targets.private == Some(false) {
            private::restore_errors(connection, PRIVATE_BACKUP_TABLE, true)?;
        }
        if current.public > BASELINE_REVISION && targets.public <= BASELINE_REVISION {
            move_public(connection, &mut current, BASELINE_REVISION, false)?;
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
        assert_eq!(current["public_target"], PUBLIC_HEAD);
        let checked = migration_check(path.as_str(), &request).unwrap();
        assert_eq!(checked["private_fields"], 0);
        assert!(!path.0.exists());
    }

    #[test]
    fn final_schema_discards_backups_for_new_and_upgraded_databases() {
        for baseline in [false, true] {
            let path = DatabasePath::new();
            if baseline {
                let connection = Connection::open(path.as_str()).unwrap();
                connection
                    .execute_batch(include_str!("testdata/migration_0033.sql"))
                    .unwrap();
                stamp(&connection, PUBLIC_VERSION_TABLE, Some("0033")).unwrap();
                migration_upgrade(path.as_str(), &dual_request("0033", "head")).unwrap();
            }
            migration_upgrade(path.as_str(), &dual_request("head", "head")).unwrap();
            let connection = Connection::open(path.as_str()).unwrap();
            let backups: i64 = connection.query_row(
                "SELECT count(*) FROM sqlite_master WHERE type='table' AND name IN ('_futures_close_policy_backup','axile_0013_error_backup','axile_0014_market_value_backup','axile_0015_tq_market_value_backup','axon_0001_error_backup')",
                [], |row| row.get(0)).unwrap();
            assert_eq!(backups, 0);
            let error = migration_check(path.as_str(), &dual_request("head", "base")).unwrap_err();
            assert!(error.error.contains("rollback is unavailable"));
            migration_downgrade(path.as_str(), &dual_request("0033", "head")).unwrap();
            migration_upgrade(path.as_str(), &dual_request("head", "head")).unwrap();
        }
    }

    #[test]
    fn already_0034_upgrade_preserves_backups_for_explicit_cleanup() {
        let path = DatabasePath::new();
        migration_upgrade(path.as_str(), &json!({})).unwrap();
        let connection = Connection::open(path.as_str()).unwrap();
        let tables = [
            "_futures_close_policy_backup",
            "axile_0013_error_backup",
            "axile_0014_market_value_backup",
            "axile_0015_tq_market_value_backup",
            "axon_0001_error_backup",
        ];
        for table in tables {
            connection
                .execute_batch(&format!(
                    "CREATE TABLE {table}(value TEXT); INSERT INTO {table} VALUES ('historical');"
                ))
                .unwrap();
        }
        for _ in 0..2 {
            let receipt = migration_upgrade(path.as_str(), &dual_request("head", "head")).unwrap();
            assert!(receipt.get("backup").is_none());
            for table in tables {
                let value: String = connection
                    .query_row(&format!("SELECT value FROM {table}"), [], |row| row.get(0))
                    .unwrap();
                assert_eq!(value, "historical");
            }
        }
    }

    #[test]
    fn private_stamp_failure_rolls_back_both_chains_and_backup_tables() {
        let path = DatabasePath::new();
        write_scope(path.as_str(), true, |connection| {
            connection
                .execute_batch(include_str!("testdata/migration_0033.sql"))
                .map_err(sql)?;
            stamp(connection, PUBLIC_VERSION_TABLE, Some("0033"))
        })
        .unwrap();
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
        assert_eq!(current["public"], "0033");
        assert_eq!(current["private"], Value::Null);
        let connection = Connection::open(path.as_str()).unwrap();
        let backups: i64 = connection
            .query_row(
                "SELECT count(*) FROM sqlite_master WHERE name='axon_0001_error_backup'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(backups, 0);
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
            stamp(connection, PUBLIC_VERSION_TABLE, Some("0034"))?;
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
             INSERT INTO alembic_version(version_num) VALUES('0033');",
        ).unwrap();
        stamp(&connection, PUBLIC_VERSION_TABLE, Some("0034")).unwrap();
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
        stamp(&connection, PUBLIC_VERSION_TABLE, Some("0033")).unwrap();
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
            assert_eq!(state(Some(connection))?.public, 33);
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

    #[test]
    fn targets_only_allow_the_published_baseline_and_final_revision() {
        for target in ["0033", "0034", "head"] {
            let request = dual_request(target, "head");
            assert!(Policy::parse(&request)
                .unwrap()
                .targets(&request, true)
                .is_ok());
        }
        for target in ["base", "0001", "0032", "0035", "0039"] {
            let request = dual_request(target, "base");
            assert!(Policy::parse(&request)
                .unwrap()
                .targets(&request, true)
                .is_err());
        }
        assert!(Policy::parse(&json!({}))
            .unwrap()
            .targets(&json!({}), true)
            .is_err());
    }

    #[test]
    fn old_revisions_require_the_published_bridge_before_a_write() {
        for revision in 1..33 {
            let path = DatabasePath::new();
            let connection = Connection::open(path.as_str()).unwrap();
            stamp(
                &connection,
                PUBLIC_VERSION_TABLE,
                Some(&format!("{revision:04}")),
            )
            .unwrap();
            drop(connection);
            let before = std::fs::read(&path.0).unwrap();
            let error = migration_upgrade(path.as_str(), &json!({})).unwrap_err();
            assert!(error.error.contains("v0.23.1"), "{error:?}");
            assert_eq!(std::fs::read(&path.0).unwrap(), before);
        }
    }

    #[test]
    fn final_revision_is_not_a_stamp_only_shortcut() {
        let path = DatabasePath::new();
        let connection = Connection::open(path.as_str()).unwrap();
        stamp(&connection, PUBLIC_VERSION_TABLE, Some(PUBLIC_HEAD)).unwrap();
        drop(connection);
        let before = std::fs::read(&path.0).unwrap();
        assert!(migration_upgrade(path.as_str(), &json!({}))
            .unwrap_err()
            .error
            .contains("development schema"));
        assert_eq!(std::fs::read(&path.0).unwrap(), before);
    }
}
