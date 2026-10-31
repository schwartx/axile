//! Rebuild missing directory indexes in memory from verified sealed MCAP parts.

use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::io::{Cursor, Read};
use std::path::Path;

use mcap::records::{op, Record};
use mcap::sans_io::{LinearReadEvent, LinearReader, LinearReaderOptions};

use crate::execution_replay::{
    child_file, day_directory, digest, failure, identifier, io_error, read_bounded, Catalog,
    CatalogRecording, PartDescriptor, RecoveryPartMeta, ReplayResult, CATALOG_LIMIT,
    PART_FILE_LIMIT, RECOVERY_METADATA_LIMIT, RECOVERY_METADATA_NAME,
};
use crate::execution_replay_reader::{validate_part_bytes_budget, Budget};

#[derive(Default)]
pub(crate) struct ReadIndex {
    pub catalogs: Vec<Catalog>,
    pub partial_recordings: BTreeSet<String>,
}

pub(crate) fn load_read_index(account: &Path, budget: &mut Budget) -> ReplayResult<ReadIndex> {
    budget.charge(0, 0)?;
    if !account.exists() {
        return Ok(ReadIndex::default());
    }
    let mut days = Vec::new();
    for entry in fs::read_dir(account).map_err(io_error)? {
        budget.directory_entry()?;
        let entry = entry.map_err(io_error)?;
        let day = entry.file_name().to_string_lossy().into_owned();
        if chrono::NaiveDate::parse_from_str(&day, "%Y-%m-%d").is_ok() && day.len() == 10 {
            day_directory(account, &day, false)?;
            budget.day()?;
            days.push(day);
        }
    }
    days.sort();
    let mut index = ReadIndex::default();
    for day in days {
        let catalog = read_day(account, &day, budget, &mut index.partial_recordings)?;
        index.catalogs.push(catalog);
    }
    Ok(index)
}

fn read_day(
    account: &Path,
    day: &str,
    budget: &mut Budget,
    partials: &mut BTreeSet<String>,
) -> ReplayResult<Catalog> {
    let directory = day_directory(account, day, false)?;
    let mut files = Vec::new();
    let mut day_partials = BTreeSet::new();
    for entry in fs::read_dir(&directory).map_err(io_error)? {
        budget.directory_entry()?;
        let name = entry
            .map_err(io_error)?
            .file_name()
            .to_string_lossy()
            .into_owned();
        if let Some(stem) = name.strip_suffix(".mcap.partial") {
            let (recording, _) = part_identity(stem)?;
            day_partials.insert(recording);
        } else if name.ends_with(".mcap") {
            part_identity(name.strip_suffix(".mcap").unwrap())?;
            files.push(name);
        }
    }
    partials.extend(day_partials.iter().cloned());
    let catalog_path = child_file(&directory, "catalog.json")?;
    let mut catalog = if catalog_path.exists() {
        let size = fs::metadata(&catalog_path).map_err(io_error)?.len();
        budget.charge(size, 0)?;
        let bytes = read_bounded(&catalog_path, CATALOG_LIMIT)?;
        let catalog: Catalog = serde_json::from_slice(&bytes)
            .map_err(|error| failure("CORRUPT", error.to_string()))?;
        if catalog.schema_version != 1 || catalog.day != day {
            return Err(failure("CORRUPT", "invalid replay catalog version or day"));
        }
        catalog
    } else {
        Catalog {
            schema_version: 1,
            day: day.into(),
            recordings: BTreeMap::new(),
            parts: Vec::new(),
        }
    };
    let known = known_parts(&catalog)?;
    files.sort();
    for name in files {
        budget.charge(0, 0)?;
        if known.contains(&name) {
            continue;
        }
        let (part, metadata) = recover_sealed_part(account, day, &name, budget)?;
        merge_recovered(&mut catalog, part, metadata, &day_partials)?;
    }
    Ok(catalog)
}

fn known_parts(catalog: &Catalog) -> ReplayResult<BTreeSet<String>> {
    let mut names = BTreeSet::new();
    for part in &catalog.parts {
        identifier(&part.recording_id)?;
        if part.day != catalog.day || part.part == 0 || !names.insert(part.file_name()) {
            return Err(failure(
                "CORRUPT",
                "invalid or duplicate indexed replay part",
            ));
        }
    }
    Ok(names)
}

fn part_identity(stem: &str) -> ReplayResult<(String, u32)> {
    let (recording, number) = stem
        .strip_prefix("recording-")
        .and_then(|stem| stem.rsplit_once('-'))
        .ok_or_else(|| failure("CORRUPT", "unrecognized sealed replay filename"))?;
    identifier(recording)?;
    let number = number
        .parse::<u32>()
        .ok()
        .filter(|number| *number > 0)
        .ok_or_else(|| failure("CORRUPT", "invalid replay part number"))?;
    if stem != format!("recording-{recording}-{number:06}") {
        return Err(failure("CORRUPT", "noncanonical replay part filename"));
    }
    Ok((recording.into(), number))
}

fn recover_sealed_part(
    account: &Path,
    day: &str,
    name: &str,
    budget: &mut Budget,
) -> ReplayResult<(PartDescriptor, CatalogRecording)> {
    let (recording, number) = part_identity(
        name.strip_suffix(".mcap")
            .ok_or_else(|| failure("PATH", "only sealed MCAP files may be recovered"))?,
    )?;
    let directory = day_directory(account, day, false)?;
    let path = child_file(&directory, name)?;
    let size = fs::metadata(&path).map_err(io_error)?.len();
    budget.charge(size, 0)?;
    let bytes = read_bounded(&path, PART_FILE_LIMIT as usize)?;
    let recovery = read_recovery_metadata(&bytes, budget)?;
    let account_id = account
        .file_name()
        .and_then(|name| name.to_str())
        .and_then(|name| name.strip_prefix("account-"))
        .and_then(|name| name.parse::<u64>().ok())
        .ok_or_else(|| failure("PATH", "invalid account directory"))?;
    let mut part = recovery.descriptor;
    if recovery.schema_version != 1
        || recovery.account_id != account_id
        || part.recording_id != recording
        || part.part != number
        || part.day != day
        || !part.sha256.is_empty()
        || part.size_bytes != 0
    {
        return Err(failure(
            "CORRUPT",
            "recovery metadata disagrees with its account or filename",
        ));
    }
    if let Some(previous) = &recovery.previous_recording_id {
        identifier(previous)?;
    }
    part.sha256 = digest(&bytes);
    part.size_bytes = bytes.len() as u64;
    let data = validate_part_bytes_budget(&bytes, &part, account_id, budget)?;
    let metadata = CatalogRecording {
        context: data.context,
        previous_recording_id: recovery.previous_recording_id,
        executions: part.executions.clone(),
        state: "unknown".into(),
        unsealed_start_us: None,
        error: None,
    };
    Ok((part, metadata))
}

fn read_recovery_metadata(bytes: &[u8], budget: &mut Budget) -> ReplayResult<RecoveryPartMeta> {
    let options = LinearReaderOptions::default()
        .with_emit_chunks(true)
        .with_validate_data_section_crc(true)
        .with_validate_summary_section_crc(true)
        .with_check_finishes_after_end_magic(true)
        .with_record_length_limit(8 * 1024 * 1024);
    let mut reader = LinearReader::new_with_options(options);
    let mut source = Cursor::new(bytes);
    let mut result = None;
    while let Some(event) = reader.next_event() {
        budget.charge(0, 1)?;
        match event.map_err(|error| failure("CORRUPT", error.to_string()))? {
            LinearReadEvent::ReadRequest(need) => {
                let read = source
                    .read(reader.insert(need.min(64 * 1024)))
                    .map_err(io_error)?;
                reader.notify_read(read);
            }
            LinearReadEvent::Record { opcode, data } if opcode == op::METADATA => {
                if data.len() > RECOVERY_METADATA_LIMIT {
                    return Err(failure(
                        "BUDGET",
                        "recovery metadata exceeds its byte budget",
                    ));
                }
                if let Some(metadata) = decode_recovery_record(data)? {
                    if result.replace(metadata).is_some() {
                        return Err(failure("CORRUPT", "duplicate part recovery metadata"));
                    }
                }
            }
            _ => (),
        }
    }
    result.ok_or_else(|| {
        failure(
            "INCOMPLETE",
            "sealed MCAP has no supported recovery metadata; its execution cannot be inferred",
        )
    })
}

fn decode_recovery_record(data: &[u8]) -> ReplayResult<Option<RecoveryPartMeta>> {
    let Record::Metadata(metadata) = mcap::parse_record(op::METADATA, data)
        .map_err(|error| failure("CORRUPT", error.to_string()))?
    else {
        return Err(failure("CORRUPT", "invalid MCAP metadata record"));
    };
    if metadata.name != RECOVERY_METADATA_NAME {
        if metadata.name.starts_with("axile.execution_replay.part.") {
            return Err(failure(
                "SCHEMA",
                "unsupported part recovery metadata version",
            ));
        }
        return Ok(None);
    }
    let json = metadata
        .metadata
        .get("json")
        .filter(|_| metadata.metadata.len() == 1)
        .ok_or_else(|| failure("CORRUPT", "invalid part recovery metadata fields"))?;
    serde_json::from_str(json)
        .map(Some)
        .map_err(|error| failure("CORRUPT", error.to_string()))
}

fn merge_recovered(
    catalog: &mut Catalog,
    part: PartDescriptor,
    metadata: CatalogRecording,
    partials: &BTreeSet<String>,
) -> ReplayResult<()> {
    let identity = part.recording_id.clone();
    if let Some(existing) = catalog.recordings.get_mut(&identity) {
        if existing.context != metadata.context
            || existing.previous_recording_id != metadata.previous_recording_id
        {
            return Err(failure(
                "CORRUPT",
                "recovered part disagrees with the frozen recording context",
            ));
        }
        existing.executions.extend(metadata.executions);
        if existing
            .unsealed_start_us
            .is_some_and(|start| part.start_us <= start && start < part.end_us)
            && !partials.contains(&identity)
        {
            existing.unsealed_start_us = None;
            existing.state = "unknown".into();
        }
    } else {
        catalog.recordings.insert(identity, metadata);
    }
    catalog.parts.push(part);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::execution_replay::ExecutionReplayWriter;
    use crate::execution_replay_reader::{manifest, window};
    use serde_json::{json, Value};
    use std::sync::atomic::{AtomicU64, Ordering};

    static CASE: AtomicU64 = AtomicU64::new(0);

    struct Fixture(std::path::PathBuf);
    impl Fixture {
        fn new() -> Self {
            let path = std::env::temp_dir().join(format!(
                "axile-replay-recovery-{}-{}",
                std::process::id(),
                CASE.fetch_add(1, Ordering::Relaxed)
            ));
            fs::create_dir(&path).unwrap();
            Self(path)
        }
        fn write_part(&self) -> (std::path::PathBuf, std::path::PathBuf) {
            let mut writer =
                ExecutionReplayWriter::new(&self.0, 1, "recovery-A", r#"{"schema_version":1}"#)
                    .unwrap();
            let event = json!({"schema_version":1,"recording_id":"recovery-A","capture_seq":1,"time_us":100,
                "topic":"/execution","data":{"execution_id":"7","phase":"started","target_quantity":10}});
            writer.append(&event.to_string()).unwrap();
            writer.finish().unwrap();
            drop(writer);
            let day = self.0.join("account-1/1970-01-01");
            (
                day.join("catalog.json"),
                day.join("recording-recovery-A-000001.mcap"),
            )
        }
        fn assert_replay(&self) {
            let before = manifest(&self.0, 1, "7").unwrap();
            assert_eq!(before["recordings"][0]["recording_id"], "recovery-A");
            assert_eq!(
                before["recordings"][0]["coverage"],
                json!([{"start_us":100,"end_us":101}])
            );
            let page = window(
                &self.0,
                1,
                "7",
                r#"{"recording_id":"recovery-A","start_us":100,"end_us":101}"#,
            )
            .unwrap();
            assert_eq!(page["state_before_start"]["state_through_seq"], 0);
            assert_eq!(page["messages"][0]["data"]["target_quantity"], 10);
            assert_eq!(page["messages"].as_array().unwrap().len(), 1);
            assert_eq!(page["complete"], true);
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn missing_catalog_recovers_verified_mcap_without_writing_index() {
        let fixture = Fixture::new();
        let (catalog, sealed) = fixture.write_part();
        let original = fs::read(&sealed).unwrap();
        fs::remove_file(&catalog).unwrap();
        fixture.assert_replay();
        assert!(!catalog.exists());
        assert_eq!(fs::read(&sealed).unwrap(), original);
        fs::write(&sealed, &original[..original.len() - 8]).unwrap();
        assert_eq!(
            manifest(&fixture.0, 1, "7").unwrap_err().code,
            "REPLAY_CORRUPT"
        );
    }

    #[test]
    fn orphan_sealed_part_is_added_only_to_the_read_view() {
        let fixture = Fixture::new();
        let (path, _) = fixture.write_part();
        let mut catalog: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
        catalog["parts"] = json!([]);
        fs::write(&path, serde_json::to_vec(&catalog).unwrap()).unwrap();
        let unchanged = fs::read(&path).unwrap();
        fixture.assert_replay();
        assert_eq!(fs::read(path).unwrap(), unchanged);
    }

    #[test]
    fn disabled_account_and_missing_root_return_not_recorded_without_creation() {
        let fixture = Fixture::new();
        let absent = fixture.0.join("absent");
        for root in [&absent, &fixture.0] {
            let response = manifest(root, 1, "7").unwrap();
            assert_eq!(response["availability"], "not_recorded");
            assert_eq!(response["recordings"], json!([]));
            assert!(!root.join("account-1").exists());
        }
        assert!(!absent.exists());
    }

    #[test]
    fn unknown_partial_is_reported_without_parsing_or_publishing_it() {
        let fixture = Fixture::new();
        let directory = fixture.0.join("account-1/1970-01-01");
        fs::create_dir_all(&directory).unwrap();
        let partial = directory.join("recording-pending-000001.mcap.partial");
        fs::write(&partial, b"not a readable MCAP").unwrap();
        assert_eq!(
            manifest(&fixture.0, 1, "7").unwrap_err().code,
            "REPLAY_INCOMPLETE"
        );
        assert_eq!(fs::read(partial).unwrap(), b"not a readable MCAP");
        assert!(!directory.join("catalog.json").exists());
    }
}
