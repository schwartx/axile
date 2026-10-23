//! SQLite 连接配置；业务入口只能打开已有文件。

use std::path::Path;
use std::time::Duration;

use rusqlite::{Connection, OpenFlags};

use crate::error::AnalysisError;

fn config(error: rusqlite::Error) -> AnalysisError {
    AnalysisError::new("DATABASE_CONFIG", error.to_string())
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
        .map_err(|error| AnalysisError::new("DATABASE_OPEN", error.to_string()))?;
    connection
        .busy_timeout(Duration::from_secs(30))
        .map_err(config)?;
    connection
        .execute_batch(
            "PRAGMA cache_size=-4096;
             PRAGMA mmap_size=0;
             PRAGMA temp_store=FILE;",
        )
        .map_err(config)?;
    Ok(connection)
}

pub(crate) fn open_reader(database: &Path) -> Result<Connection, AnalysisError> {
    let connection = open(database, OpenFlags::SQLITE_OPEN_READ_ONLY)?;
    connection
        .execute_batch("PRAGMA query_only=ON; PRAGMA foreign_keys=ON;")
        .map_err(config)?;
    Ok(connection)
}

pub(crate) fn open_writer(
    database: &Path,
    foreign_keys: bool,
) -> Result<Connection, AnalysisError> {
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

#[cfg(test)]
mod tests {
    use super::*;

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
        std::fs::remove_dir_all(directory).unwrap();
    }
}
