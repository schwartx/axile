//! A single current-directory snapshot. Reading declarations never executes Python.
use super::{checked_source, discover};
use serde::Serialize;
use std::fs::{self, File};
use std::io::{self, Read};
use std::path::Path;

#[derive(Serialize)]
pub struct Entry {
    name: String,
    path: String,
}

#[derive(Serialize)]
pub struct Algorithm {
    file: String,
    name: String,
    function: String,
}

#[derive(Serialize)]
pub struct Issue {
    file: String,
    message: String,
}

#[derive(Default, Serialize)]
pub struct Discovery {
    files: usize,
    algorithms: Vec<Algorithm>,
    issues: Vec<Issue>,
    can_use: bool,
}

#[derive(Serialize)]
pub struct Preview {
    path: String,
    parent: Option<String>,
    entries: Vec<Entry>,
    discovery: Discovery,
}

fn scan_file(path: &Path, buffer: &mut Vec<u8>, output: &mut Discovery) -> io::Result<()> {
    buffer.clear();
    File::open(path)?.read_to_end(buffer)?;
    let source = checked_source(buffer, true)
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "脚本需要使用 UTF-8 编码"))?;
    let file = path
        .file_name()
        .and_then(|s| s.to_str())
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "脚本文件名不是有效 UTF-8"))?;
    let found = discover(source);
    if !found.unsupported.is_empty() {
        output.issues.push(Issue {
            file: file.to_owned(),
            message: "算法名称需要直接写成字符串，例如 @register_algorithm(\"名称\")".to_owned(),
        });
    }
    output
        .algorithms
        .extend(found.algorithms.into_iter().map(|a| Algorithm {
            file: file.to_owned(),
            name: a.name,
            function: a.function,
        }));
    Ok(())
}

pub fn scan_directory(path: &Path) -> io::Result<Preview> {
    let path = fs::canonicalize(path)?;
    let mut files = Vec::new();
    let mut entries = Vec::new();
    for entry in fs::read_dir(&path)? {
        let entry = entry?;
        let kind = entry.file_type()?;
        if kind.is_dir() || (kind.is_symlink() && entry.path().is_dir()) {
            entries.push(Entry {
                name: entry.file_name().to_string_lossy().into_owned(),
                path: entry.path().to_string_lossy().into_owned(),
            });
        } else if kind.is_file() && entry.path().extension().is_some_and(|e| e == "py") {
            files.push(entry.path());
        }
    }
    files.sort();
    entries.sort_by_cached_key(|entry| entry.name.to_lowercase());
    let mut discovery = Discovery {
        files: files.len(),
        ..Discovery::default()
    };
    let mut buffer = Vec::new();
    for file in files {
        if let Err(error) = scan_file(&file, &mut buffer, &mut discovery) {
            discovery.issues.push(Issue {
                file: file
                    .file_name()
                    .unwrap_or_default()
                    .to_string_lossy()
                    .into_owned(),
                message: error.to_string(),
            });
        }
    }
    discovery.can_use = !discovery.algorithms.is_empty() && discovery.issues.is_empty();
    Ok(Preview {
        parent: path.parent().map(|p| p.to_string_lossy().into_owned()),
        path: path.to_string_lossy().into_owned(),
        entries,
        discovery,
    })
}
