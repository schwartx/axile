//! 传送目录、原子状态文件及与 Python v1 回执一致的清单摘要。
use crate::error::AnalysisError;
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::fs::{self, File};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};

pub(super) type Result<T> = std::result::Result<T, AnalysisError>;
pub(super) const MAX_BYTES: u64 = 4 * 1024 * 1024 * 1024;
pub(super) const MAX_FILES: usize = 100_000;

pub(super) fn invalid(error: impl std::fmt::Display) -> AnalysisError {
    AnalysisError::new("TRANSFER_PACKAGE", error.to_string())
}
pub(super) fn base(database: &str) -> Result<PathBuf> {
    let database = Path::new(database).canonicalize().map_err(invalid)?;
    Ok(database
        .parent()
        .ok_or_else(|| invalid("数据库缺少目录"))?
        .to_owned())
}
pub(super) fn root(database: &str) -> Result<PathBuf> {
    let directory = base(database)?.join("transfer");
    mkdir(&directory)?;
    sync_directory(directory.parent().unwrap())?;
    Ok(directory)
}
pub(super) fn reject_links(path: &Path) -> Result<()> {
    for parent in path.ancestors() {
        match fs::symlink_metadata(parent) {
            Ok(metadata) if metadata.file_type().is_symlink() => {
                return Err(invalid("传送路径不能包含符号链接"))
            }
            Ok(_) => {}
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
            Err(e) => return Err(invalid(e)),
        }
    }
    Ok(())
}
pub(super) fn mkdir(path: &Path) -> Result<()> {
    reject_links(path)?;
    let missing = path
        .ancestors()
        .take_while(|parent| !parent.exists())
        .collect::<Vec<_>>();
    let mut builder = fs::DirBuilder::new();
    builder.recursive(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::DirBuilderExt;
        builder.mode(0o700);
    }
    builder.create(path).map_err(invalid)?;
    for directory in missing.iter().rev() {
        sync_directory(directory)?;
        sync_directory(directory.parent().unwrap())?;
    }
    Ok(())
}
pub(super) fn archive_name(path: &Path) -> Result<String> {
    Ok(path
        .iter()
        .map(|part| part.to_str().ok_or_else(|| invalid("文件名不是 UTF-8")))
        .collect::<Result<Vec<_>>>()?
        .join("/"))
}
pub(super) fn sync_directory(path: &Path) -> Result<()> {
    crate::execution_replay::sync_directory(path).map_err(invalid)
}
pub(super) fn write_json(path: &Path, value: &Value) -> Result<()> {
    reject_links(path)?;
    let parent = path.parent().ok_or_else(|| invalid("状态文件缺少目录"))?;
    let mut temporary = tempfile::NamedTempFile::new_in(parent).map_err(invalid)?;
    serde_json::to_writer(temporary.as_file_mut(), value).map_err(invalid)?;
    temporary.as_file().sync_all().map_err(invalid)?;
    temporary.persist(path).map_err(invalid)?;
    sync_directory(parent)
}
pub(super) fn read_json(path: &Path) -> Result<Value> {
    reject_links(path)?;
    let file = File::open(path).map_err(invalid)?;
    if file.metadata().map_err(invalid)?.len() > 16 * 1024 * 1024 {
        return Err(invalid("状态文件超过预算"));
    }
    serde_json::from_reader(file).map_err(invalid)
}
pub(super) fn remove_file(path: &Path) -> Result<()> {
    reject_links(path)?;
    match fs::remove_file(path) {
        Ok(()) => sync_directory(path.parent().unwrap()),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(e) => Err(invalid(e)),
    }
}
pub(super) fn digest(path: &Path) -> Result<String> {
    reject_links(path)?;
    let mut file = File::open(path).map_err(invalid)?;
    let mut hash = Sha256::new();
    let mut buffer = [0u8; 64 * 1024];
    loop {
        let n = file.read(&mut buffer).map_err(invalid)?;
        if n == 0 {
            break;
        }
        hash.update(&buffer[..n]);
    }
    Ok(format!("{:x}", hash.finalize()))
}
pub(super) fn identity(value: &str) -> Result<()> {
    if value.len() != 32
        || !value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    {
        return Err(invalid("无效数据包身份"));
    }
    Ok(())
}

// v1 用 json.dumps(sort_keys=True) 计算回执，必须保留空格和 ASCII 转义。
pub(super) fn manifest_hash(value: &Value) -> Result<String> {
    let mut bytes = Vec::new();
    canonical(value, &mut bytes)?;
    Ok(format!("{:x}", Sha256::digest(&bytes)))
}
fn string(value: &str, out: &mut Vec<u8>) -> Result<()> {
    for character in serde_json::to_string(value).map_err(invalid)?.chars() {
        if character >= '\u{7f}' {
            let mut buffer = [0u16; 2];
            for unit in character.encode_utf16(&mut buffer) {
                write!(out, "\\u{unit:04x}").map_err(invalid)?;
            }
        } else {
            out.push(character as u8);
        }
    }
    Ok(())
}
fn canonical(value: &Value, out: &mut Vec<u8>) -> Result<()> {
    match value {
        Value::Object(fields) => {
            out.push(b'{');
            let mut keys = fields.keys().collect::<Vec<_>>();
            keys.sort();
            for (index, key) in keys.iter().enumerate() {
                if index > 0 {
                    out.extend_from_slice(b", ");
                }
                string(key, out)?;
                out.extend_from_slice(b": ");
                canonical(&fields[*key], out)?;
            }
            out.push(b'}');
        }
        Value::Array(values) => {
            out.push(b'[');
            for (index, value) in values.iter().enumerate() {
                if index > 0 {
                    out.extend_from_slice(b", ");
                }
                canonical(value, out)?;
            }
            out.push(b']');
        }
        Value::String(value) => string(value, out)?,
        _ => out.extend_from_slice(value.to_string().as_bytes()),
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    #[test]
    fn python_v1_ascii_and_ordering() {
        let mut bytes = Vec::new();
        canonical(&json!({"中😀": [null, true, 2], "a": "\u{7f}"}), &mut bytes).unwrap();
        assert_eq!(
            String::from_utf8(bytes).unwrap(),
            r#"{"a": "\u007f", "\u4e2d\ud83d\ude00": [null, true, 2]}"#
        );
    }
}
