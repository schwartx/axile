//! Shared replay errors, identities and safe filesystem access.

use std::fs::{self, File};
use std::path::{Path, PathBuf};

use sha2::{Digest, Sha256};

use crate::error::AnalysisError;

pub(crate) const JS_SAFE: u64 = 9_007_199_254_740_991;

pub(crate) type ReplayResult<T> = Result<T, AnalysisError>;

pub(crate) fn failure(code: &str, message: impl Into<String>) -> AnalysisError {
    AnalysisError::new(format!("REPLAY_{code}"), message)
}

pub(crate) fn io_error(error: impl std::fmt::Display) -> AnalysisError {
    failure("IO", error.to_string())
}

pub(crate) fn digest(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

pub(crate) fn identifier(value: &str) -> ReplayResult<()> {
    if value.is_empty()
        || value.len() > 128
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_'))
    {
        return Err(failure("INVALID_REQUEST", "invalid recording identity"));
    }
    Ok(())
}

pub(crate) fn account_directory(
    root: &Path,
    account_id: u64,
    create: bool,
) -> ReplayResult<PathBuf> {
    if account_id == 0 || account_id > JS_SAFE {
        return Err(failure("INVALID_REQUEST", "invalid account identity"));
    }
    if create {
        fs::create_dir_all(root).map_err(io_error)?;
    }
    let root = root.canonicalize().map_err(io_error)?;
    let account = root.join(format!("account-{account_id}"));
    if create && !account.exists() {
        fs::create_dir(&account).map_err(io_error)?;
    }
    if let Ok(metadata) = fs::symlink_metadata(&account) {
        if metadata.file_type().is_symlink() || !metadata.is_dir() {
            return Err(failure(
                "PATH",
                "account replay directory must not be a symlink",
            ));
        }
    }
    Ok(account)
}

pub(crate) fn child_file(directory: &Path, name: &str) -> ReplayResult<PathBuf> {
    if name.is_empty() || name.contains(['/', '\\']) || matches!(name, "." | "..") {
        return Err(failure("PATH", "invalid replay file name"));
    }
    let path = directory.join(name);
    if fs::symlink_metadata(&path).is_ok_and(|metadata| metadata.file_type().is_symlink()) {
        return Err(failure("PATH", "replay files must not be symbolic links"));
    }
    Ok(path)
}

pub(crate) fn sync_directory(path: &Path) -> ReplayResult<()> {
    #[cfg(unix)]
    File::open(path)
        .and_then(|file| file.sync_all())
        .map_err(io_error)?;
    #[cfg(not(unix))]
    let _ = path;
    Ok(())
}
