//! 进程内共享 SQLite 空闲连接；业务入口只能打开已有文件。

use std::collections::HashMap;
use std::ops::{Deref, DerefMut};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicPtr, AtomicU64, Ordering};
use std::sync::{Arc, Mutex, MutexGuard};
use std::time::Duration;

use rusqlite::{Connection, OpenFlags};

use crate::error::AnalysisError;

const MAX_IDLE: usize = 8;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum Mode {
    Reader,
    Writer(bool),
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct FileIdentity {
    device: u64,
    file: u128,
}

#[cfg(unix)]
fn file_identity(path: &Path) -> std::io::Result<FileIdentity> {
    use std::os::unix::fs::MetadataExt;

    let metadata = std::fs::metadata(path)?;
    Ok(FileIdentity {
        device: metadata.dev(),
        file: u128::from(metadata.ino()),
    })
}

#[cfg(windows)]
fn file_identity(path: &Path) -> std::io::Result<FileIdentity> {
    use std::ffi::c_void;
    use std::os::windows::{fs::OpenOptionsExt, io::AsRawHandle};

    #[repr(C)]
    #[derive(Default)]
    struct FileIdInfo {
        volume_serial_number: u64,
        file_id: [u8; 16],
    }

    #[link(name = "kernel32")]
    extern "system" {
        fn GetFileInformationByHandleEx(
            handle: *mut c_void,
            information_class: i32,
            information: *mut FileIdInfo,
            size: u32,
        ) -> i32;
    }

    let file = std::fs::OpenOptions::new().access_mode(0).open(path)?;
    let mut info = FileIdInfo::default();
    // SAFETY: the live handle and writable repr(C) buffer match FILE_ID_INFO.
    let success = unsafe {
        GetFileInformationByHandleEx(
            file.as_raw_handle(),
            18, // FileIdInfo uses the full 128-bit ID, including on ReFS.
            &mut info,
            std::mem::size_of::<FileIdInfo>() as u32,
        )
    };
    if success == 0 {
        return Err(std::io::Error::last_os_error());
    }
    Ok(FileIdentity {
        device: info.volume_serial_number,
        file: u128::from_le_bytes(info.file_id),
    })
}

#[derive(Debug)]
struct Session {
    database: PathBuf,
    identity: FileIdentity,
    mode: Mode,
    generation: Arc<AtomicU64>,
    epoch: u64,
    connection: Connection,
}

#[derive(Debug, Default)]
struct State {
    idle: Vec<Session>,
    generations: HashMap<PathBuf, Arc<AtomicU64>>,
}

#[derive(Debug)]
struct Registry {
    pid: u32,
    state: Mutex<State>,
}

static REGISTRY: AtomicPtr<Registry> = AtomicPtr::new(std::ptr::null_mut());

fn registry() -> &'static Registry {
    loop {
        let pointer = REGISTRY.load(Ordering::Acquire);
        if !pointer.is_null() {
            // Registries live until process exit; fork children never lock or close inherited ones.
            let current = unsafe { &*pointer };
            if current.pid == std::process::id() {
                return current;
            }
        }
        let fresh = Box::into_raw(Box::new(Registry {
            pid: std::process::id(),
            state: Mutex::new(State::default()),
        }));
        if REGISTRY
            .compare_exchange(pointer, fresh, Ordering::AcqRel, Ordering::Acquire)
            .is_ok()
        {
            return unsafe { &*fresh };
        }
        // Only this unpublished, empty registry is reclaimed.
        unsafe { drop(Box::from_raw(fresh)) };
    }
}

impl Registry {
    fn lock(&self) -> Result<MutexGuard<'_, State>, AnalysisError> {
        self.state
            .lock()
            .map_err(|_| AnalysisError::new("DATABASE_LOCK", "connection registry mutex poisoned"))
    }
}

impl State {
    fn generation(&mut self, database: &Path) -> Arc<AtomicU64> {
        self.generations
            .retain(|_, generation| Arc::strong_count(generation) > 1);
        Arc::clone(
            self.generations
                .entry(database.to_owned())
                .or_insert_with(|| Arc::new(AtomicU64::new(0))),
        )
    }

    fn invalidate(&mut self, database: &Path) {
        self.idle.retain(|session| session.database != database);
    }
}

/// 一次业务操作独占一条连接；结束后只归还无快照、附库和临时对象的连接。
#[derive(Debug)]
pub(crate) struct ConnectionLease {
    session: Option<Session>,
    registry: &'static Registry,
}

impl Deref for ConnectionLease {
    type Target = Connection;

    fn deref(&self) -> &Self::Target {
        &self.session.as_ref().expect("leased connection").connection
    }
}

impl DerefMut for ConnectionLease {
    fn deref_mut(&mut self) -> &mut Self::Target {
        &mut self.session.as_mut().expect("leased connection").connection
    }
}

fn clean(connection: &Connection) -> bool {
    connection.progress_handler(0, None::<fn() -> bool>);
    if !connection.is_autocommit() && connection.execute_batch("ROLLBACK;").is_err() {
        return false;
    }
    connection
        .query_row(
            "SELECT NOT EXISTS(SELECT 1 FROM pragma_database_list WHERE name NOT IN ('main', 'temp'))
                AND NOT EXISTS(SELECT 1 FROM sqlite_temp_schema)",
            [],
            |row| row.get::<_, bool>(0),
        )
        .unwrap_or(false)
}

impl Drop for ConnectionLease {
    fn drop(&mut self) {
        let Some(session) = self.session.take() else {
            return;
        };
        if self.registry.pid != std::process::id() {
            std::mem::forget(session);
            return;
        }
        if !clean(&session.connection)
            || file_identity(&session.database).ok() != Some(session.identity)
            || session.generation.load(Ordering::Acquire) != session.epoch
        {
            return;
        }
        if let Ok(mut state) = self.registry.lock() {
            if session.generation.load(Ordering::Acquire) != session.epoch {
                return;
            }
            let evicted = (state.idle.len() == MAX_IDLE).then(|| state.idle.remove(0));
            state.idle.push(session);
            drop(state);
            drop(evicted);
        }
    }
}

/// 迁移前后让同路径缓存失效；活动操作仍由 SQLite 保持原有事务并发语义。
pub(crate) struct MigrationGuard {
    database: PathBuf,
    generation: Arc<AtomicU64>,
    registry: &'static Registry,
}

impl Drop for MigrationGuard {
    fn drop(&mut self) {
        if self.registry.pid != std::process::id() {
            return;
        }
        self.generation.fetch_add(1, Ordering::AcqRel);
        if let Ok(mut state) = self.registry.lock() {
            state.invalidate(&self.database);
        }
    }
}

fn database_path(database: &Path) -> Result<PathBuf, AnalysisError> {
    if database.as_os_str().is_empty() || database == Path::new(":memory:") {
        return Err(AnalysisError::new(
            "DATABASE_OPEN",
            "A file-backed SQLite database is required",
        ));
    }
    database
        .canonicalize()
        .or_else(|error| {
            if error.kind() != std::io::ErrorKind::NotFound {
                return Err(error);
            }
            let parent = database
                .parent()
                .filter(|path| !path.as_os_str().is_empty());
            let parent = parent.unwrap_or_else(|| Path::new(".")).canonicalize()?;
            database
                .file_name()
                .map(|name| parent.join(name))
                .ok_or(error)
        })
        .map_err(|error| AnalysisError::caused_by("DATABASE_OPEN", error))
}

impl Registry {
    fn migration_guard(&'static self, database: &Path) -> Result<MigrationGuard, AnalysisError> {
        let database = database_path(database)?;
        let registry = self;
        let mut state = registry.lock()?;
        let generation = state.generation(&database);
        generation.fetch_add(1, Ordering::AcqRel);
        state.invalidate(&database);
        Ok(MigrationGuard {
            database,
            generation,
            registry,
        })
    }
}

pub(crate) fn migration_guard(database: &Path) -> Result<MigrationGuard, AnalysisError> {
    registry().migration_guard(database)
}

fn config(error: rusqlite::Error) -> AnalysisError {
    AnalysisError::caused_by("DATABASE_CONFIG", error)
}

fn open(database: &Path, flags: OpenFlags) -> Result<Connection, AnalysisError> {
    if database.as_os_str().is_empty() || database == Path::new(":memory:") {
        return Err(AnalysisError::new(
            "DATABASE_OPEN",
            "A file-backed SQLite database is required",
        ));
    }
    // 不传 CREATE，路径错误不能悄悄生成空业务库。
    let connection = Connection::open_with_flags(database, flags | OpenFlags::SQLITE_OPEN_NO_MUTEX)
        .map_err(|error| AnalysisError::caused_by("DATABASE_OPEN", error))?;
    connection
        .busy_timeout(Duration::from_secs(30))
        .map_err(config)?;
    crate::calendar::register_time_function(&connection).map_err(config)?;
    connection
        .execute_batch(
            "PRAGMA cache_size=-4096;
             PRAGMA mmap_size=0;
             PRAGMA temp_store=FILE;",
        )
        .map_err(config)?;
    Ok(connection)
}

fn reader(database: &Path) -> Result<Connection, AnalysisError> {
    let connection = open(database, OpenFlags::SQLITE_OPEN_READ_ONLY)?;
    connection
        .execute_batch("PRAGMA query_only=ON; PRAGMA foreign_keys=ON;")
        .map_err(config)?;
    Ok(connection)
}

fn writer(database: &Path, foreign_keys: bool) -> Result<Connection, AnalysisError> {
    let connection = open(database, OpenFlags::SQLITE_OPEN_READ_WRITE)?;
    let mode: String = connection
        .query_row("PRAGMA journal_mode=WAL;", [], |row| row.get(0))
        .map_err(config)?;
    if !mode.eq_ignore_ascii_case("wal") {
        return Err(AnalysisError::new(
            "DATABASE_CONFIG",
            format!("WAL journal mode is required, got {mode}"),
        ));
    }
    connection
        .execute_batch("PRAGMA synchronous=FULL;")
        .map_err(config)?;
    // 事实终态替换需要删除 RESTRICT 引用行，沿用原有 OFF；其他写入保留 ON。
    connection
        .pragma_update(None, "foreign_keys", foreign_keys)
        .map_err(config)?;
    Ok(connection)
}

impl Registry {
    fn checkout(
        &'static self,
        database: &Path,
        mode: Mode,
    ) -> Result<ConnectionLease, AnalysisError> {
        let database = database_path(database)?;
        let registry = self;
        let identity = match file_identity(&database) {
            Ok(identity) => identity,
            Err(error) => {
                registry.lock()?.invalidate(&database);
                return Err(AnalysisError::caused_by("DATABASE_OPEN", error));
            }
        };
        let mut state = registry.lock()?;
        state
            .idle
            .retain(|session| session.database != database || session.identity == identity);
        let generation = state.generation(&database);
        let epoch = generation.load(Ordering::Acquire);
        let session = state
            .idle
            .iter()
            .position(|session| {
                session.database == database && session.mode == mode && session.epoch == epoch
            })
            .map(|index| state.idle.swap_remove(index));
        drop(state);
        let session = match session {
            Some(session) => session,
            None => Session {
                connection: match mode {
                    Mode::Reader => reader(&database)?,
                    Mode::Writer(foreign_keys) => writer(&database, foreign_keys)?,
                },
                database,
                identity,
                mode,
                generation,
                epoch,
            },
        };
        if file_identity(&session.database).ok() != Some(identity) {
            return Err(AnalysisError::new(
                "DATABASE_OPEN",
                "database changed while opening",
            ));
        }
        Ok(ConnectionLease {
            session: Some(session),
            registry,
        })
    }
}

pub(crate) fn open_reader(database: &Path) -> Result<ConnectionLease, AnalysisError> {
    registry().checkout(database, Mode::Reader)
}

pub(crate) fn open_writer(
    database: &Path,
    foreign_keys: bool,
) -> Result<ConnectionLease, AnalysisError> {
    registry().checkout(database, Mode::Writer(foreign_keys))
}

#[cfg(test)]
mod tests {
    use super::*;
    use rusqlite::functions::FunctionFlags;

    struct Database {
        directory: PathBuf,
        path: PathBuf,
        registry: &'static Registry,
    }

    impl Database {
        fn new(name: &str) -> Self {
            let directory = std::env::temp_dir().join(format!(
                "axile-connection-reuse-{name}-{}",
                std::process::id()
            ));
            std::fs::create_dir(&directory).unwrap();
            let path = directory.join("storage.sqlite");
            Connection::open(&path)
                .unwrap()
                .execute_batch(
                    "CREATE TABLE entries(value INTEGER); INSERT INTO entries VALUES(1);",
                )
                .unwrap();
            Self {
                directory,
                path,
                registry: Box::leak(Box::new(Registry {
                    pid: std::process::id(),
                    state: Mutex::new(State::default()),
                })),
            }
        }

        fn reader(&self) -> ConnectionLease {
            self.registry.checkout(&self.path, Mode::Reader).unwrap()
        }

        fn writer(&self) -> ConnectionLease {
            self.registry
                .checkout(&self.path, Mode::Writer(true))
                .unwrap()
        }
    }

    impl Drop for Database {
        fn drop(&mut self) {
            self.registry.lock().unwrap().idle.clear();
            std::fs::remove_dir_all(&self.directory).unwrap();
        }
    }

    fn mark(connection: &Connection) {
        connection
            .create_scalar_function("lease_marker", 0, FunctionFlags::SQLITE_UTF8, |_| {
                Ok(42_i64)
            })
            .unwrap();
    }

    fn marked(connection: &Connection) -> bool {
        connection
            .query_row("SELECT lease_marker()", [], |row| row.get::<_, i64>(0))
            .is_ok()
    }

    fn value(connection: &Connection) -> i64 {
        connection
            .query_row("SELECT value FROM entries", [], |row| row.get(0))
            .unwrap()
    }

    #[test]
    fn reuse_is_scoped_by_database_and_mode_and_idle_storage_is_bounded() {
        let database = Database::new("scope");
        let reader = database.reader();
        mark(&reader);
        drop(reader);
        let reused = database.reader();
        assert!(marked(&reused));
        assert!(!marked(&database.writer()));
        let other = database.directory.join("other.sqlite");
        drop(Connection::open(&other).unwrap());
        let separate = database.registry.checkout(&other, Mode::Reader).unwrap();
        assert!(!marked(&separate));
        drop((separate, reused));

        let readers = (0..MAX_IDLE + 2)
            .map(|_| database.reader())
            .collect::<Vec<_>>();
        drop(readers);
        assert_eq!(database.registry.lock().unwrap().idle.len(), MAX_IDLE);
    }

    #[test]
    fn returned_connections_end_snapshots_and_roll_back_unfinished_writes() {
        let database = Database::new("snapshot");
        let writer = database.writer();
        let reader = database.reader();
        mark(&reader);
        reader.execute_batch("BEGIN DEFERRED;").unwrap();
        assert_eq!(value(&reader), 1);
        writer.execute("UPDATE entries SET value=2", []).unwrap();
        assert_eq!(value(&reader), 1);
        reader.progress_handler(1, Some(|| true));
        drop(reader);
        let reader = database.reader();
        assert!(marked(&reader));
        assert!(reader.is_autocommit());
        assert_eq!(value(&reader), 2);

        writer
            .execute_batch("BEGIN IMMEDIATE; UPDATE entries SET value=3;")
            .unwrap();
        drop(writer);
        let writer = database.writer();
        assert!(writer.is_autocommit());
        assert_eq!(value(&writer), 2);
    }

    #[test]
    fn temporary_objects_and_leftover_attachments_discard_the_connection() {
        let database = Database::new("dirty");
        let writer = database.writer();
        mark(&writer);
        writer
            .execute_batch("CREATE TEMP TABLE leftover(value INTEGER);")
            .unwrap();
        drop(writer);
        let writer = database.writer();
        assert!(!marked(&writer));
        mark(&writer);
        writer
            .execute_batch("ATTACH DATABASE '' AS leftover;")
            .unwrap();
        drop(writer);
        assert!(!marked(&database.writer()));
    }

    #[test]
    fn migration_evicts_idle_and_preexisting_active_leases() {
        let database = Database::new("migration");
        let active = database.reader();
        mark(&active);
        let idle = database.reader();
        drop(idle);
        let guard = database.registry.migration_guard(&database.path).unwrap();
        assert!(database.registry.lock().unwrap().idle.is_empty());
        let during = database.reader();
        mark(&during);
        drop(during);
        drop(guard);
        drop(active);
        assert!(database.registry.lock().unwrap().idle.is_empty());
        assert!(!marked(&database.reader()));
    }

    #[cfg(unix)]
    #[test]
    fn replaced_and_missing_files_never_reuse_old_handles() {
        let database = Database::new("replacement");
        let reader = database.reader();
        mark(&reader);
        drop(reader);
        let replacement = database.directory.join("replacement.sqlite");
        Connection::open(&replacement)
            .unwrap()
            .execute_batch("CREATE TABLE entries(value INTEGER); INSERT INTO entries VALUES(2);")
            .unwrap();
        std::fs::rename(replacement, &database.path).unwrap();
        let reader = database.reader();
        assert_eq!(value(&reader), 2);
        assert!(!marked(&reader));
        drop(reader);
        std::fs::remove_file(&database.path).unwrap();
        assert_eq!(
            database
                .registry
                .checkout(&database.path, Mode::Reader)
                .unwrap_err()
                .code,
            "DATABASE_OPEN"
        );
        assert!(database.registry.lock().unwrap().idle.is_empty());
        assert!(!database.path.exists());
    }

    #[test]
    fn file_connections_share_limits_and_preserve_foreign_key_modes() {
        let directory =
            std::env::temp_dir().join(format!("axile-storage-connection-{}", std::process::id()));
        std::fs::create_dir(&directory).unwrap();
        let path = directory.join("storage.sqlite");
        assert_eq!(open_reader(&path).unwrap_err().code, "DATABASE_OPEN");
        assert_eq!(open_writer(&path, true).unwrap_err().code, "DATABASE_OPEN");
        assert!(!path.exists());
        for invalid in [Path::new(""), Path::new(":memory:")] {
            assert_eq!(open_reader(invalid).unwrap_err().code, "DATABASE_OPEN");
            assert_eq!(
                open_writer(invalid, true).unwrap_err().code,
                "DATABASE_OPEN"
            );
        }

        Connection::open(&path)
            .unwrap()
            .execute_batch(
                "CREATE TABLE parent(id INTEGER PRIMARY KEY);
                 CREATE TABLE child(parent_id INTEGER REFERENCES parent(id) ON DELETE RESTRICT);
                 INSERT INTO parent VALUES (1);
                 INSERT INTO child VALUES (1);",
            )
            .unwrap();
        let reader = open_reader(&path).unwrap();
        let mode: String = reader
            .query_row("PRAGMA journal_mode;", [], |row| row.get(0))
            .unwrap();
        assert_eq!(mode, "delete");
        assert!(reader.execute("DELETE FROM parent", []).is_err());
        drop(reader);

        let writer = open_writer(&path, true).unwrap();
        let facts = open_writer(&path, false).unwrap();
        let reader = open_reader(&path).unwrap();
        for connection in [&writer, &facts, &reader] {
            for (pragma, expected) in [
                ("busy_timeout", 30_000),
                ("cache_size", -4096),
                ("mmap_size", 0),
                ("temp_store", 1),
            ] {
                let actual: i64 = connection
                    .pragma_query_value(None, pragma, |row| row.get(0))
                    .unwrap();
                assert_eq!(actual, expected, "{pragma}");
            }
        }
        for connection in [&writer, &facts] {
            let synchronous: i64 = connection
                .pragma_query_value(None, "synchronous", |row| row.get(0))
                .unwrap();
            let mode: String = connection
                .pragma_query_value(None, "journal_mode", |row| row.get(0))
                .unwrap();
            assert_eq!(synchronous, 2);
            assert_eq!(mode, "wal");
        }
        for (connection, expected) in [(&writer, 1), (&facts, 0), (&reader, 1)] {
            let foreign_keys: i64 = connection
                .pragma_query_value(None, "foreign_keys", |row| row.get(0))
                .unwrap();
            assert_eq!(foreign_keys, expected);
            assert!(connection.is_autocommit());
        }
        let query_only: i64 = reader
            .pragma_query_value(None, "query_only", |row| row.get(0))
            .unwrap();
        assert_eq!(query_only, 1);
        assert!(writer.execute("DELETE FROM parent", []).is_err());
        assert_eq!(facts.execute("DELETE FROM parent", []).unwrap(), 1);
        assert!(reader.execute("INSERT INTO parent VALUES (2)", []).is_err());
        drop((reader, facts, writer));
        drop(migration_guard(&path).unwrap());
        std::fs::remove_dir_all(directory).unwrap();
    }
}
