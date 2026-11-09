//! Rebuild missing or damaged directory indexes from verified sealed MCAP parts.

use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, File};
use std::io::{Cursor, Read, Seek, SeekFrom};
use std::path::Path;

use mcap::records::{op, Record};
use mcap::sans_io::{LinearReadEvent, LinearReader, LinearReaderOptions};
use serde_json::Value;

use crate::execution_replay::{
    child_file, day_directory, digest, failure, identifier, io_error, read_bounded,
    read_mcap_summary, validate_committed_records, Catalog, CatalogRecording, PartDescriptor,
    RecoveryPartMeta, ReplayResult, CATALOG_LIMIT, PART_FILE_LIMIT, RECOVERY_METADATA_LIMIT,
    RECOVERY_METADATA_NAME, JS_SAFE, SCAN_FILES,
};
use crate::execution_replay_block::read_block;
use crate::execution_replay_index::BlockIndex;
use crate::execution_replay_reader::{
    collect_part_symbols, validate_descriptor, validate_part_bytes_budget, Budget,
};

#[derive(Default)]
pub(crate) struct ReadIndex {
    pub catalogs: Vec<Catalog>,
    pub partial_recordings: BTreeSet<String>,
    pub partial_parts: Vec<(String, String, u32)>,
}

pub(crate) fn load_read_index(
    account: &Path,
    execution: &str,
    execution_end_us: Option<u64>,
    budget: &mut Budget,
) -> ReplayResult<ReadIndex> {
    budget.charge(0, 0)?;
    if !account.exists() {
        return Ok(ReadIndex::default());
    }
    let index_path = child_file(account, ".replay-index.sqlite")?;
    if index_path.exists() {
        budget.indexed = true;
        return load_sqlite_index(
            account,
            execution,
            execution_end_us,
            &BlockIndex::open(&index_path)?,
        );
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
        let catalog = read_day(
            account,
            &day,
            budget,
            &mut index.partial_recordings,
            &mut index.partial_parts,
        )?;
        index.catalogs.push(catalog);
    }
    Ok(index)
}

fn load_sqlite_index(
    account: &Path,
    execution: &str,
    execution_end_us: Option<u64>,
    sqlite: &BlockIndex,
) -> ReplayResult<ReadIndex> {
    let recordings = sqlite.recording_metadata(execution)?;
    for metadata in recordings.values() {
        if metadata
            .context
            .get("schema_version")
            .and_then(Value::as_u64)
            != Some(1)
        {
            return Err(failure("SCHEMA", "unsupported frozen replay context"));
        }
        if let Some(previous) = &metadata.previous_recording_id {
            identifier(previous)?;
        }
        crate::execution_replay::validate_symbols(&metadata.symbols)?;
    }
    let mut index = ReadIndex::default();
    let mut days: BTreeMap<String, Vec<PartDescriptor>> = BTreeMap::new();
    let is_later_unassociated = |metadata: &CatalogRecording| {
        metadata.executions.is_empty()
            && metadata
                .unsealed_start_us
                .zip(execution_end_us)
                .is_some_and(|(start_us, end_us)| start_us <= JS_SAFE && start_us > end_us)
    };
    let mut files = sqlite.files(execution)?;
    files.retain(|(recording, ..)| !recordings.get(recording).is_some_and(is_later_unassociated));
    let recorded: BTreeSet<_> = files.iter().map(|(recording, _, _)| recording).collect();
    for recording in recorded {
        for reference in sqlite.fragment_references(recording)? {
            let day = reference["day"]
                .as_str()
                .ok_or_else(|| failure("CORRUPT", "fragment has no day"))?;
            if reference["reference"]["__axile_fragment__"]["recording"].as_str() != Some(recording)
            {
                return Err(failure("CORRUPT", "fragment belongs to another recording"));
            }
            let directory = day_directory(account, day, false)?;
            crate::execution_replay_fragments::check_present(&directory, &reference["reference"])?;
        }
    }
    for (recording, part, day) in files {
        identifier(&recording)?;
        let metadata = recordings
            .get(&recording)
            .ok_or_else(|| failure("CORRUPT", "file has no recording metadata"))?;
        let directory = day_directory(account, &day, false)?;
        let name = format!("recording-{recording}-{part:06}.mcap");
        let sealed = child_file(&directory, &name)?;
        let partial = child_file(&directory, &format!("{name}.partial"))?;
        let descriptor = sqlite.sealed_file(&recording, part)?;
        if let Some((descriptor, context, previous)) = descriptor {
            if descriptor.recording_id != recording
                || descriptor.part != part
                || descriptor.day != day
                || context != metadata.context
                || previous != metadata.previous_recording_id
            {
                return Err(failure(
                    "CORRUPT",
                    "file metadata differs from its recording",
                ));
            }
            validate_descriptor(&descriptor)?;
            if sealed.exists() || !partial.exists() {
                if sealed.exists()
                    && fs::metadata(&sealed).map_err(io_error)?.len() == descriptor.size_bytes
                {
                    let summary = read_mcap_summary(&sealed, false)?;
                    if summary.stats.as_ref().is_none_or(|stats| {
                        stats.message_count != descriptor.last_seq - descriptor.first_seq + 2
                    }) {
                        return Err(failure(
                            "CORRUPT",
                            "sealed replay summary disagrees with its index",
                        ));
                    }
                }
                days.entry(day).or_default().push(descriptor);
                continue;
            }
        }
        index.partial_recordings.insert(recording.clone());
        index.partial_parts.push((recording, day, part));
    }
    for (recording, metadata) in &recordings {
        if metadata.unsealed_start_us.is_some() && !is_later_unassociated(metadata) {
            index.partial_recordings.insert(recording.clone());
        }
    }
    index.catalogs.push(Catalog {
        schema_version: 1,
        day: "1970-01-01".into(),
        recordings,
        parts: Vec::new(),
    });
    for (day, parts) in days {
        index.catalogs.push(Catalog {
            schema_version: 1,
            day,
            recordings: BTreeMap::new(),
            parts,
        });
    }
    Ok(index)
}

fn read_day(
    account: &Path,
    day: &str,
    budget: &mut Budget,
    partials: &mut BTreeSet<String>,
    partial_parts: &mut Vec<(String, String, u32)>,
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
            let (recording, part) = part_identity(stem)?;
            partial_parts.push((recording.clone(), day.to_owned(), part));
            day_partials.insert(recording);
        } else if name.ends_with(".mcap") {
            part_identity(name.strip_suffix(".mcap").unwrap())?;
            files.push(name);
        }
    }
    partials.extend(day_partials.iter().cloned());
    let catalog_path = child_file(&directory, "catalog.json")?;
    let indexed = if catalog_path.exists() {
        let size = fs::metadata(&catalog_path).map_err(io_error)?.len();
        budget.charge(size, 0)?;
        let bytes = read_bounded(&catalog_path, CATALOG_LIMIT)?;
        decode_catalog(&bytes, day)?
    } else {
        None
    };
    let (mut catalog, known) = indexed.unwrap_or_else(|| {
        (
            Catalog {
                schema_version: 1,
                day: day.into(),
                recordings: BTreeMap::new(),
                parts: Vec::new(),
            },
            BTreeSet::new(),
        )
    });
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

fn decode_catalog(bytes: &[u8], day: &str) -> ReplayResult<Option<(Catalog, BTreeSet<String>)>> {
    let value: Value = match serde_json::from_slice(bytes) {
        Ok(value) => value,
        Err(_) => return Ok(None),
    };
    if value
        .get("schema_version")
        .and_then(Value::as_u64)
        .is_some_and(|version| version != 1)
    {
        return Err(failure("SCHEMA", "unsupported replay catalog version"));
    }
    let catalog: Catalog = match serde_json::from_value(value) {
        Ok(catalog) => catalog,
        Err(_) => return Ok(None),
    };
    if catalog.schema_version != 1 || catalog.day != day {
        return Ok(None);
    }
    if catalog.parts.len() > SCAN_FILES || catalog.recordings.len() > SCAN_FILES {
        return Err(failure("BUDGET", "catalog contains too many entries"));
    }
    let known = match known_parts(&catalog) {
        Ok(known) => known,
        Err(error) if matches!(error.code.as_str(), "REPLAY_SCHEMA" | "REPLAY_BUDGET") => {
            return Err(error)
        }
        Err(_) => return Ok(None),
    };
    Ok(Some((catalog, known)))
}

fn known_parts(catalog: &Catalog) -> ReplayResult<BTreeSet<String>> {
    let mut names = BTreeSet::new();
    for (identity, metadata) in &catalog.recordings {
        identifier(identity)?;
        if let Some(previous) = &metadata.previous_recording_id {
            identifier(previous)?;
        }
        if !metadata.context.is_object() {
            return Err(failure("CORRUPT", "recording has no frozen context"));
        }
        match metadata
            .context
            .get("schema_version")
            .and_then(Value::as_u64)
        {
            Some(1) => (),
            Some(_) => return Err(failure("SCHEMA", "unsupported frozen replay context")),
            None => return Err(failure("CORRUPT", "frozen replay context has no version")),
        }
        crate::execution_replay::validate_symbols(&metadata.symbols)?;
    }
    for part in &catalog.parts {
        validate_descriptor(part)?;
        if part.day != catalog.day
            || !catalog.recordings.contains_key(&part.recording_id)
            || !names.insert(part.file_name())
        {
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
    let index_path = child_file(account, ".replay-index.sqlite")?;
    if index_path.exists() {
        let index = BlockIndex::open(&index_path)?;
        if let Some((part, context, previous)) = index.sealed_file(&recording, number)? {
            return recover_indexed_part(
                account, day, &path, size, &index, part, context, previous, budget,
            );
        }
    }
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
    collect_part_symbols(&data, &mut part.symbols)?;
    let metadata = CatalogRecording {
        context: data.context,
        previous_recording_id: recovery.previous_recording_id,
        executions: part.executions.clone(),
        symbols: part.symbols.clone(),
        state: "unknown".into(),
        unsealed_start_us: None,
        error: None,
    };
    Ok((part, metadata))
}

fn recover_indexed_part(
    account: &Path,
    day: &str,
    path: &Path,
    size: u64,
    index: &BlockIndex,
    part: PartDescriptor,
    context: Value,
    previous: Option<String>,
    budget: &mut Budget,
) -> ReplayResult<(PartDescriptor, CatalogRecording)> {
    validate_descriptor(&part)?;
    if part.day != day
        || part.committed_prefix
        || part.size_bytes != size
        || path.file_name().and_then(|name| name.to_str()) != Some(part.file_name().as_str())
        || context["schema_version"] != 1
    {
        return Err(failure(
            "CORRUPT",
            "sealed replay index disagrees with its file",
        ));
    }
    if let Some(previous) = &previous {
        identifier(previous)?;
    }
    let summary = read_mcap_summary(path, true)?;
    if summary
        .stats
        .as_ref()
        .is_none_or(|stats| stats.message_count != part.last_seq - part.first_seq + 2)
    {
        return Err(failure(
            "CORRUPT",
            "sealed replay summary disagrees with its index",
        ));
    }
    let mut metadata = summary
        .metadata_indexes
        .iter()
        .filter(|item| item.name == RECOVERY_METADATA_NAME);
    let entry = metadata
        .next()
        .ok_or_else(|| failure("INCOMPLETE", "sealed replay recovery metadata is missing"))?;
    if metadata.next().is_some()
        || entry.length > (RECOVERY_METADATA_LIMIT + 9) as u64
        || entry
            .offset
            .checked_add(entry.length)
            .is_none_or(|end| end > size)
    {
        return Err(failure("CORRUPT", "invalid replay recovery metadata index"));
    }
    budget.charge(entry.length, 0)?;
    let mut file = File::open(path).map_err(io_error)?;
    file.seek(SeekFrom::Start(entry.offset)).map_err(io_error)?;
    let mut bytes = vec![0; entry.length as usize];
    file.read_exact(&mut bytes).map_err(io_error)?;
    if bytes.len() < 9
        || bytes[0] != op::METADATA
        || u64::from_le_bytes(bytes[1..9].try_into().unwrap()) != (bytes.len() - 9) as u64
    {
        return Err(failure("CORRUPT", "invalid replay recovery record"));
    }
    let recovery = decode_recovery_record(&bytes[9..])?
        .ok_or_else(|| failure("CORRUPT", "missing replay recovery record"))?;
    let account_id = account
        .file_name()
        .and_then(|name| name.to_str())
        .and_then(|name| name.strip_prefix("account-"))
        .and_then(|name| name.parse::<u64>().ok())
        .ok_or_else(|| failure("PATH", "invalid account directory"))?;
    let mut original = part.clone();
    original.sha256.clear();
    original.size_bytes = 0;
    if recovery.schema_version != 1
        || recovery.account_id != account_id
        || recovery.previous_recording_id != previous
        || recovery.descriptor != original
    {
        return Err(failure(
            "CORRUPT",
            "sealed replay recovery metadata disagrees with its index",
        ));
    }
    let (first, last) = index
        .file_bounds(&part.recording_id, part.part, part.last_seq)?
        .ok_or_else(|| failure("INCOMPLETE", "sealed replay block index is missing"))?;
    if first.first_seq != part.first_seq
        || first.first_us != part.start_us
        || last.last_seq != part.last_seq
    {
        return Err(failure(
            "CORRUPT",
            "sealed replay block index has invalid bounds",
        ));
    }
    budget.charge(first.range.length, 0)?;
    let records = read_block(path, &first.range)?;
    let (snapshot, messages) = validate_committed_records(
        &records,
        &part,
        account_id,
        &context,
        first.first_seq,
        first.last_seq,
        first.first_us,
        first.last_us,
        &mut None,
        &mut BTreeMap::new(),
        None,
    )?;
    let checkpoint = index
        .checkpoint_at(&part.recording_id, part.part, part.checkpoint_through_seq)?
        .ok_or_else(|| failure("INCOMPLETE", "indexed replay checkpoint is missing"))?;
    let hash = digest(&serde_json::to_vec(&checkpoint.state).map_err(io_error)?);
    if snapshot
        .as_ref()
        .and_then(|value| value["projection"]["sha256"].as_str())
        != Some(hash.as_str())
    {
        return Err(failure(
            "CORRUPT",
            "checkpoint disagrees with its replay file",
        ));
    }
    budget.charge(0, messages.len())?;
    let recording = CatalogRecording {
        context,
        previous_recording_id: previous,
        executions: part.executions.clone(),
        symbols: part.symbols.clone(),
        state: "unknown".into(),
        unsealed_start_us: None,
        error: None,
    };
    Ok((part, recording))
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
        existing.symbols.extend(metadata.symbols);
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
            let before = manifest(&self.0, 1, "7", None).unwrap();
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
    fn indexed_replay_needs_no_daily_catalog_and_reports_a_truncated_file() {
        let fixture = Fixture::new();
        let (catalog, sealed) = fixture.write_part();
        let original = fs::read(&sealed).unwrap();
        assert!(!catalog.exists());
        fixture.assert_replay();
        fs::write(&sealed, &original[..original.len() - 8]).unwrap();
        let result = manifest(&fixture.0, 1, "7", None).unwrap();
        assert_eq!(result["availability"], "partial");
        assert!(window(
            &fixture.0,
            1,
            "7",
            r#"{"recording_id":"recovery-A","start_us":100,"end_us":101}"#
        )
        .is_err());
    }

    #[test]
    fn indexed_replay_rejects_a_corrupt_summary_crc() {
        use std::io::Write;
        let fixture = Fixture::new();
        let (_, sealed) = fixture.write_part();
        let mut file = fs::OpenOptions::new()
            .read(true)
            .write(true)
            .open(&sealed)
            .unwrap();
        file.seek(SeekFrom::End(-10)).unwrap();
        let mut byte = [0_u8];
        file.read_exact(&mut byte).unwrap();
        file.seek(SeekFrom::End(-10)).unwrap();
        file.write_all(&[byte[0] ^ 0x80]).unwrap();
        drop(file);
        assert_eq!(
            manifest(&fixture.0, 1, "7", None).unwrap_err().code,
            "REPLAY_CORRUPT"
        );
    }

    #[test]
    fn stray_catalog_cannot_forge_an_execution_mapping() {
        let fixture = Fixture::new();
        let (catalog, sealed) = fixture.write_part();
        let original = fs::read(&sealed).unwrap();
        let forged = br#"{"recordings":{"recovery-A":{"executions":["forged"]}}}"#;
        fs::write(&catalog, forged).unwrap();
        fixture.assert_replay();
        assert_eq!(
            manifest(&fixture.0, 1, "forged", None).unwrap()["availability"],
            "not_recorded"
        );
        assert_eq!(fs::read(&catalog).unwrap(), forged);
        assert_eq!(fs::read(&sealed).unwrap(), original);
    }

    #[test]
    fn corrupted_indexed_block_is_not_used_for_replay() {
        let fixture = Fixture::new();
        fixture.write_part();
        let db =
            rusqlite::Connection::open(fixture.0.join("account-1/.replay-index.sqlite")).unwrap();
        db.execute("UPDATE blocks SET hash=?1", ["0".repeat(64)])
            .unwrap();
        assert!(window(
            &fixture.0,
            1,
            "7",
            r#"{"recording_id":"recovery-A","start_us":100,"end_us":101}"#
        )
        .is_err());
    }

    #[test]
    fn unsupported_index_version_fails_without_changing_the_recording() {
        let fixture = Fixture::new();
        let (_, sealed) = fixture.write_part();
        let original = fs::read(&sealed).unwrap();
        let db =
            rusqlite::Connection::open(fixture.0.join("account-1/.replay-index.sqlite")).unwrap();
        for version in [1, 2, 4] {
            db.execute("UPDATE identity SET version=?1", [version])
                .unwrap();
            assert_eq!(
                manifest(&fixture.0, 1, "7", None).unwrap_err().code,
                "REPLAY_CORRUPT"
            );
        }
        assert_eq!(fs::read(&sealed).unwrap(), original);
    }

    #[test]
    fn missing_index_does_not_claim_the_recording_is_absent() {
        let fixture = Fixture::new();
        let (_, sealed) = fixture.write_part();
        let original = fs::read(&sealed).unwrap();
        fs::remove_file(fixture.0.join("account-1/.replay-index.sqlite")).unwrap();
        assert!(manifest(&fixture.0, 1, "7", None).is_err());
        let error = match ExecutionReplayWriter::new(
            &fixture.0,
            1,
            "new-recording",
            r#"{"schema_version":1}"#,
        ) {
            Ok(_) => panic!("writer recreated an empty index"),
            Err(error) => error,
        };
        assert_eq!(error.code, "REPLAY_INCOMPLETE");
        assert!(!fixture.0.join("account-1/.replay-index.sqlite").exists());
        assert_eq!(fs::read(&sealed).unwrap(), original);
    }

    #[test]
    fn disabled_account_and_missing_root_return_not_recorded_without_creation() {
        let fixture = Fixture::new();
        let absent = fixture.0.join("absent");
        for root in [&absent, &fixture.0] {
            let response = manifest(root, 1, "7", None).unwrap();
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
            manifest(&fixture.0, 1, "7", Some(100)).unwrap_err().code,
            "REPLAY_INCOMPLETE"
        );
        assert_eq!(fs::read(partial).unwrap(), b"not a readable MCAP");
        assert!(!directory.join("catalog.json").exists());
    }
}
