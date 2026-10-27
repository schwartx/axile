//! Process-local, lossy diagnostics. No database or Python callback is used by the writer.

use chrono::{DateTime, SecondsFormat, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};
use std::cell::RefCell;
use std::collections::{HashMap, HashSet};
use std::fs::{File, OpenOptions};
use std::io::{self, BufWriter, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::mpsc::{self, Receiver, SyncSender, TrySendError};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use crate::logging_rotation::Rotation;

static CURRENT: Mutex<Option<Arc<Logger>>> = Mutex::new(None);
static NEXT_SPAN: AtomicU64 = AtomicU64::new(1);
static PENDING_DROPS: Mutex<DropCounts> = Mutex::new(DropCounts {
    full: 0,
    unavailable: 0,
    invalid: 0,
});
thread_local! { static CONTEXT: RefCell<Value> = RefCell::new(json!({"context":{},"spans":[]})); }

pub fn scope(value: Value) -> Value {
    CONTEXT.with(|context| context.replace(value))
}

pub fn operation<T>(
    name: &str,
    request: &Value,
    task: impl FnOnce() -> Result<T, crate::error::AnalysisError>,
) -> Result<T, crate::error::AnalysisError> {
    if current().is_none() {
        return task();
    }
    let saved = CONTEXT.with(|context| context.borrow().clone());
    let mut context = saved
        .get("context")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    for key in ["account_id", "execution_id", "symbol", "channel"] {
        if let Some(value) = request.get(key).filter(|value| !value.is_null()) {
            context.insert(key.into(), value.clone());
        }
    }
    let mut parents: Vec<Span> = saved
        .get("spans")
        .cloned()
        .and_then(|value| serde_json::from_value(value).ok())
        .unwrap_or_default();
    let own = span(
        name.into(),
        parents.last().map(|parent| parent.id),
        context.clone(),
    );
    parents.push(own);
    native_event(name, context.clone(), "DEBUG", None, &parents, None);
    let result = task();
    native_event(
        name,
        context,
        if result.is_ok() { "DEBUG" } else { "WARNING" },
        Some(if result.is_ok() { "ok" } else { "error" }),
        &parents,
        result.as_ref().err().map(|error| error.to_string()),
    );
    result
}

fn lock<T>(value: &Mutex<T>) -> std::sync::MutexGuard<'_, T> {
    value
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

fn now_us() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_micros()
        .min(i64::MAX as u128) as u64
}

fn iso_time(micros: u64) -> String {
    DateTime::<Utc>::from_timestamp_micros(micros as i64)
        .unwrap_or(DateTime::<Utc>::UNIX_EPOCH)
        .to_rfc3339_opts(SecondsFormat::Micros, true)
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
pub struct Span {
    pub id: u64,
    pub parent: Option<u64>,
    pub name: String,
    pub context: Map<String, Value>,
    pub start_us: u64,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Event {
    pub level: String,
    pub template: String,
    pub fields: Vec<String>,
    pub args: Vec<Value>,
    #[serde(default)]
    pub metadata: Map<String, Value>,
    #[serde(default)]
    pub context: Map<String, Value>,
    #[serde(default)]
    pub spans: Vec<Span>,
}

impl Event {
    fn validate(&self) -> bool {
        if self.fields.len() != self.args.len()
            || self.fields.iter().any(String::is_empty)
            || self.fields.iter().collect::<HashSet<_>>().len() != self.fields.len()
            || level(&self.level).is_none()
            || !valid_template(&self.template, &self.fields)
            || self.metadata.get("kind").is_some_and(|kind| {
                !matches!(kind.as_str(), Some("event" | "span_start" | "span_end"))
            })
        {
            return false;
        }
        let mut parent = None;
        let mut ids = HashSet::new();
        for span in &self.spans {
            if span.id == 0 || span.parent != parent || !ids.insert(span.id) {
                return false;
            }
            parent = Some(span.id);
        }
        let kind = self
            .metadata
            .get("kind")
            .and_then(Value::as_str)
            .unwrap_or("event");
        if kind != "event" && self.spans.is_empty() {
            return false;
        }
        if kind == "span_end" {
            return self
                .fields
                .iter()
                .position(|field| field == "outcome")
                .is_some_and(|index| {
                    matches!(
                        self.args[index].as_str(),
                        Some("ok" | "error" | "cancelled")
                    )
                });
        }
        true
    }
}

fn valid_template(template: &str, fields: &[String]) -> bool {
    let mut chars = template.chars().peekable();
    while let Some(character) = chars.next() {
        match character {
            '{' if chars.peek() == Some(&'{') => {
                chars.next();
            }
            '}' if chars.peek() == Some(&'}') => {
                chars.next();
            }
            '{' => {
                let mut name = String::new();
                loop {
                    match chars.next() {
                        Some('}') => break,
                        Some(value) if value != '{' => name.push(value),
                        _ => return false,
                    }
                }
                if name.chars().all(char::is_numeric)
                    || name.contains(['.', '[', ']', ':', '!'])
                    || !fields.contains(&name)
                {
                    return false;
                }
            }
            '}' => return false,
            _ => {}
        }
    }
    true
}

fn level(value: &str) -> Option<u8> {
    Some(match value {
        "TRACE" => 5,
        "DEBUG" => 10,
        "INFO" => 20,
        "SUCCESS" => 25,
        "WARNING" | "WARN" => 30,
        "ERROR" => 40,
        "CRITICAL" => 50,
        _ => return None,
    })
}

#[derive(Clone, Copy)]
enum DropReason {
    Full,
    Unavailable,
    Invalid,
}

#[derive(Clone, Copy, Default)]
struct DropCounts {
    full: u64,
    unavailable: u64,
    invalid: u64,
}

impl DropCounts {
    fn from_stats(stats: &Value) -> Self {
        let reasons = &stats["dropped_by_reason"];
        Self {
            full: reasons["queue_full"].as_u64().unwrap_or(0),
            unavailable: reasons["unavailable"].as_u64().unwrap_or(0),
            invalid: reasons["invalid"].as_u64().unwrap_or(0),
        }
    }

    fn add_to(self, counters: &Counters) {
        counters.full.fetch_add(self.full, Ordering::Relaxed);
        counters
            .unavailable
            .fetch_add(self.unavailable, Ordering::Relaxed);
        counters.invalid.fetch_add(self.invalid, Ordering::Relaxed);
    }

    fn add(&mut self, counts: Self) {
        self.full += counts.full;
        self.unavailable += counts.unavailable;
        self.invalid += counts.invalid;
    }

    fn increment(&mut self, reason: DropReason) {
        match reason {
            DropReason::Full => self.full += 1,
            DropReason::Unavailable => self.unavailable += 1,
            DropReason::Invalid => self.invalid += 1,
        }
    }

    fn unsaved(self, saved: Self) -> Self {
        Self {
            full: self.full - saved.full,
            unavailable: self.unavailable - saved.unavailable,
            invalid: self.invalid - saved.invalid,
        }
    }
}

#[derive(Default)]
struct Counters {
    accepted: AtomicU64,
    written: AtomicU64,
    full: AtomicU64,
    unavailable: AtomicU64,
    invalid: AtomicU64,
    write_errors: AtomicU64,
    unconfirmed: AtomicU64,
    console_dropped: AtomicU64,
    archive_errors: AtomicU64,
}

impl Counters {
    fn snapshot(&self) -> Value {
        let get = |counter: &AtomicU64| counter.load(Ordering::Relaxed);
        let reasons = json!({"queue_full": get(&self.full), "unavailable": get(&self.unavailable), "invalid": get(&self.invalid)});
        json!({
            "accepted_total": get(&self.accepted), "written_total": get(&self.written),
            "dropped_total": reasons.as_object().into_iter().flatten().map(|(_, n)| n.as_u64().unwrap_or(0)).sum::<u64>(),
            "dropped_by_reason": reasons, "write_errors_total": get(&self.write_errors),
            "unconfirmed_total": get(&self.unconfirmed), "console_dropped_total": get(&self.console_dropped),
            "archive_errors_total": get(&self.archive_errors),
        })
    }
}

pub struct Logger {
    sender: Mutex<Option<SyncSender<Event>>>,
    console: Mutex<Receiver<String>>,
    archives: Mutex<Receiver<PathBuf>>,
    stopped: AtomicBool,
    finished: Mutex<Receiver<()>>,
    counters: Counters,
    recovered: Mutex<bool>,
    run_id: String,
    base_us: u64,
    minimum_level: u8,
    directory: PathBuf,
}

impl Logger {
    fn open(
        directory: &Path,
        capacity: usize,
        max_bytes: u64,
        rotation_seconds: u64,
        minimum: &str,
        rotation_json: Option<&str>,
    ) -> Result<Arc<Self>, String> {
        let minimum_level = level(minimum).ok_or("unknown logging level")?;
        if capacity == 0
            || max_bytes == 0
            || rotation_seconds == 0
            || rotation_seconds > u64::MAX / 1_000_000
        {
            return Err("logging capacity and rotation limits must be positive".into());
        }
        let base_us = now_us();
        let run_id = format!(
            "{:x}-{:x}-{:x}",
            base_us,
            std::process::id(),
            NEXT_SPAN.fetch_add(1, Ordering::Relaxed)
        );
        let rotation = Rotation::new(rotation_json, rotation_seconds, base_us)?;
        let writer = Writer::open(directory, &run_id, base_us, max_bytes, rotation)
            .map_err(|error| error.to_string())?;
        let (sender, receiver) = mpsc::sync_channel(capacity);
        let (console_sender, console) = mpsc::sync_channel(capacity);
        let (archive_sender, archives) = mpsc::sync_channel(32);
        let (finished_sender, finished) = mpsc::sync_channel(1);
        let logger = Arc::new(Self {
            sender: Mutex::new(Some(sender)),
            console: Mutex::new(console),
            archives: Mutex::new(archives),
            stopped: AtomicBool::new(false),
            finished: Mutex::new(finished),
            counters: Counters::default(),
            recovered: Mutex::new(false),
            run_id,
            base_us,
            minimum_level,
            directory: directory.to_path_buf(),
        });
        let worker_logger = Arc::clone(&logger);
        thread::Builder::new()
            .name("axile-log-writer".into())
            .spawn(move || {
                let saved_drops = run(
                    writer,
                    receiver,
                    console_sender,
                    archive_sender,
                    &worker_logger,
                );
                worker_logger.stopped.store(true, Ordering::Release);
                worker_logger.recover_drops(saved_drops);
                let _ = finished_sender.try_send(());
            })
            .map_err(|error| error.to_string())?;
        Ok(logger)
    }

    fn emit(&self, event: Event) -> bool {
        if level(&event.level).is_some_and(|severity| severity < self.minimum_level) {
            return true;
        }
        if !event.validate() {
            self.record_drop(DropReason::Invalid);
            return false;
        }
        if self.stopped.load(Ordering::Acquire) {
            self.record_drop(DropReason::Unavailable);
            return false;
        }
        let sender = lock(&self.sender);
        let Some(sender) = sender.as_ref() else {
            self.record_drop(DropReason::Unavailable);
            return false;
        };
        match sender.try_send(event) {
            Ok(()) => {
                self.counters.accepted.fetch_add(1, Ordering::Relaxed);
                true
            }
            Err(TrySendError::Full(_)) => {
                self.record_drop(DropReason::Full);
                false
            }
            Err(TrySendError::Disconnected(_)) => {
                self.record_drop(DropReason::Unavailable);
                false
            }
        }
    }

    fn stats(&self) -> Value {
        let recovered = lock(&self.recovered);
        if !*recovered {
            std::mem::take(&mut *lock(&PENDING_DROPS)).add_to(&self.counters);
        }
        let mut stats = self.counters.snapshot();
        stats["run_id"] = json!(self.run_id);
        stats["time_base"] = json!(iso_time(self.base_us));
        stats["stopped"] = json!(self.stopped.load(Ordering::Acquire));
        stats
    }

    fn record_drop(&self, reason: DropReason) {
        let recovered = lock(&self.recovered);
        let counter = match reason {
            DropReason::Full => &self.counters.full,
            DropReason::Unavailable => &self.counters.unavailable,
            DropReason::Invalid => &self.counters.invalid,
        };
        counter.fetch_add(1, Ordering::Relaxed);
        if *recovered {
            lock(&PENDING_DROPS).increment(reason);
        }
    }

    fn recover_drops(&self, saved: DropCounts) {
        let mut recovered = lock(&self.recovered);
        if !*recovered {
            let outstanding = DropCounts::from_stats(&self.counters.snapshot()).unsaved(saved);
            lock(&PENDING_DROPS).add(outstanding);
            *recovered = true;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    static TEST_STATE: Mutex<()> = Mutex::new(());

    fn event() -> Event {
        Event {
            level: "INFO".into(),
            template: "订单 {id}".into(),
            fields: vec!["id".into()],
            args: vec![json!(7)],
            metadata: Map::new(),
            context: Map::new(),
            spans: vec![],
        }
    }

    fn held_queue(capacity: usize) -> (Arc<Logger>, Receiver<Event>) {
        let (sender, receiver) = mpsc::sync_channel(capacity);
        let (_, console) = mpsc::sync_channel(1);
        let (_, archives) = mpsc::sync_channel(1);
        let (_, finished) = mpsc::sync_channel(1);
        (
            Arc::new(Logger {
                sender: Mutex::new(Some(sender)),
                console: Mutex::new(console),
                archives: Mutex::new(archives),
                stopped: AtomicBool::new(false),
                finished: Mutex::new(finished),
                counters: Counters::default(),
                recovered: Mutex::new(false),
                run_id: "test".into(),
                base_us: 0,
                minimum_level: 20,
                directory: PathBuf::new(),
            }),
            receiver,
        )
    }

    #[test]
    fn full_queue_counts_each_rejected_event_once_without_waiting() {
        let _state = lock(&TEST_STATE);
        let (logger, receiver) = held_queue(1);
        assert!(logger.emit(event()));
        let threads: Vec<_> = (0..4)
            .map(|_| {
                let logger = Arc::clone(&logger);
                thread::spawn(move || {
                    for _ in 0..1000 {
                        assert!(!logger.emit(event()));
                    }
                })
            })
            .collect();
        for thread in threads {
            thread.join().unwrap();
        }
        assert_eq!(logger.stats()["dropped_total"], 4000);
        assert_eq!(logger.stats()["accepted_total"], 1);
        assert!(receiver.try_recv().is_ok());
        assert!(logger.emit(event()));
    }

    #[test]
    fn filtering_invalid_input_and_unavailable_writer_have_separate_counts() {
        let _state = lock(&TEST_STATE);
        let (logger, receiver) = held_queue(1);
        let mut filtered = event();
        filtered.level = "DEBUG".into();
        assert!(logger.emit(filtered));
        let mut invalid = event();
        invalid.template = "{id.private}".into();
        assert!(!logger.emit(invalid));
        let mut positional = event();
        positional.template = "{0}".into();
        positional.fields = vec!["0".into()];
        assert!(!logger.emit(positional));
        drop(receiver);
        assert!(!logger.emit(event()));
        assert_eq!(logger.stats()["dropped_total"], 3);
        assert_eq!(logger.stats()["dropped_by_reason"]["invalid"], 2);
        assert_eq!(logger.stats()["dropped_by_reason"]["unavailable"], 1);
    }

    #[test]
    fn stale_consumers_do_not_read_or_count_against_new_run() {
        let _state = lock(&TEST_STATE);
        let (mut logger, _events) = held_queue(1);
        let (console_sender, console_receiver) = mpsc::sync_channel(1);
        let (archive_sender, archive_receiver) = mpsc::sync_channel(1);
        {
            let logger = Arc::get_mut(&mut logger).unwrap();
            logger.run_id = "new-run".into();
            logger.console = Mutex::new(console_receiver);
            logger.archives = Mutex::new(archive_receiver);
        }
        let previous = lock(&CURRENT).replace(Arc::clone(&logger));
        console_sender.send("new output".into()).unwrap();
        archive_sender.send(PathBuf::from("new.jsonl")).unwrap();

        assert!(console(Some("old-run")).is_none());
        assert!(archive(Some("old-run")).is_none());
        console_failed(Some("old-run"));
        archive_failed(Some("old-run"));
        assert_eq!(logger.counters.snapshot()["console_dropped_total"], 0);
        assert_eq!(logger.counters.snapshot()["archive_errors_total"], 0);

        assert_eq!(console(Some("new-run")).as_deref(), Some("new output"));
        assert_eq!(archive(None).as_deref(), Some("new.jsonl"));
        console_failed(Some("new-run"));
        archive_failed(None);
        assert_eq!(logger.counters.snapshot()["console_dropped_total"], 1);
        assert_eq!(logger.counters.snapshot()["archive_errors_total"], 1);
        *lock(&CURRENT) = previous;
    }

    #[test]
    fn close_drains_accepted_records_and_rotation_repeats_definitions() {
        let _state = lock(&TEST_STATE);
        let directory =
            std::env::temp_dir().join(format!("axile-logging-{}-{}", std::process::id(), now_us()));
        let logger = Logger::open(&directory, 128, 1, 86400, "INFO", None).unwrap();
        for _ in 0..20 {
            assert!(logger.emit(event()));
        }
        logger.stopped.store(true, Ordering::Release);
        lock(&logger.sender).take();
        lock(&logger.finished)
            .recv_timeout(Duration::from_secs(2))
            .unwrap();
        assert_eq!(logger.stats()["written_total"], 20);
        assert_eq!(logger.stats()["dropped_total"], 0);
        let mut count = 0;
        for entry in std::fs::read_dir(&directory).unwrap() {
            let text = std::fs::read_to_string(entry.unwrap().path()).unwrap();
            let lines: Vec<Value> = text
                .lines()
                .map(|line| serde_json::from_str(line).unwrap())
                .collect();
            assert_eq!(lines[0][0], "H");
            let mut defined = HashSet::new();
            for line in lines {
                if line[0] == "T" {
                    defined.insert(line[1].as_u64().unwrap());
                }
                if line[0] == "E" {
                    assert!(defined.contains(&line[3].as_u64().unwrap()));
                    count += 1;
                }
            }
        }
        assert!(count >= 20);
        std::fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn late_writer_and_late_rejections_transfer_only_unsaved_drops_once() {
        let _state = lock(&TEST_STATE);
        let (old, _old_events) = held_queue(1);
        old.record_drop(DropReason::Invalid);
        let saved = DropCounts::from_stats(&old.stats());
        old.record_drop(DropReason::Invalid);
        old.stopped.store(true, Ordering::Release);
        let (replacement, _new_events) = held_queue(1);
        assert_eq!(replacement.stats()["dropped_total"], 0);

        let threads: Vec<_> = (0..4)
            .map(|_| {
                let old = Arc::clone(&old);
                thread::spawn(move || {
                    for _ in 0..1000 {
                        old.record_drop(DropReason::Unavailable);
                    }
                })
            })
            .collect();
        old.recover_drops(saved);
        for thread in threads {
            thread.join().unwrap();
        }
        assert_eq!(old.stats()["dropped_total"], 4002);
        assert_eq!(replacement.stats()["dropped_total"], 4001);
        old.recover_drops(saved);
        assert_eq!(replacement.stats()["dropped_total"], 4001);
        assert_eq!(replacement.stats()["dropped_by_reason"]["invalid"], 1);
        assert_eq!(
            replacement.stats()["dropped_by_reason"]["unavailable"],
            4000
        );
    }

    #[test]
    #[cfg(unix)]
    fn flushed_checkpoint_is_not_recovered_again_when_sync_fails() {
        let _state = lock(&TEST_STATE);
        let directory = std::env::temp_dir().join(format!(
            "axile-logging-sync-{}-{}",
            std::process::id(),
            now_us()
        ));
        let (old, _events) = held_queue(1);
        old.record_drop(DropReason::Invalid);
        let base = now_us();
        let rotation = Rotation::new(None, 86400, base).unwrap();
        let mut writer = Writer::open(&directory, "sync-failure", base, 1024, rotation).unwrap();
        writer.file = BufWriter::new(OpenOptions::new().write(true).open("/dev/null").unwrap());
        let mut saved = DropCounts::default();
        let (console, _console) = mpsc::sync_channel(1);
        let (archives, _archives) = mpsc::sync_channel(1);
        assert!(writer
            .checkpoint(&old.stats(), &mut saved, &console, &archives, &old.counters)
            .is_err());
        assert_eq!(saved.invalid, 1);
        old.recover_drops(saved);
        let (replacement, _events) = held_queue(1);
        assert_eq!(replacement.stats()["dropped_total"], 0);
        std::fs::remove_dir_all(directory).unwrap();
    }
}

fn current() -> Option<Arc<Logger>> {
    lock(&CURRENT).clone()
}

fn current_for(run_id: Option<&str>) -> Option<Arc<Logger>> {
    current().filter(|logger| run_id.is_none_or(|run_id| logger.run_id == run_id))
}

pub fn start(
    directory: &str,
    capacity: usize,
    max_bytes: u64,
    rotation_seconds: u64,
    minimum: &str,
    rotation_json: Option<&str>,
) -> Result<(), String> {
    if capacity == 0
        || max_bytes == 0
        || rotation_seconds == 0
        || rotation_seconds > u64::MAX / 1_000_000
        || level(minimum).is_none()
    {
        return Err("invalid logging configuration".into());
    }
    if current().is_some_and(|logger| {
        !logger.stopped.load(Ordering::Acquire) && logger.directory == Path::new(directory)
    }) {
        return Ok(());
    }
    close(100);
    let logger = Logger::open(
        Path::new(directory),
        capacity,
        max_bytes,
        rotation_seconds,
        minimum,
        rotation_json,
    )?;
    *lock(&CURRENT) = Some(logger);
    Ok(())
}

pub fn emit_json(payload: &str) -> bool {
    let Some(logger) = current() else {
        lock(&PENDING_DROPS).increment(DropReason::Unavailable);
        return false;
    };
    match serde_json::from_str::<Event>(payload) {
        Ok(event) => logger.emit(event),
        Err(_) => {
            logger.record_drop(DropReason::Invalid);
            false
        }
    }
}

pub fn invalid() {
    if let Some(logger) = current() {
        logger.record_drop(DropReason::Invalid);
    } else {
        lock(&PENDING_DROPS).increment(DropReason::Unavailable);
    }
}
pub fn console_failed(run_id: Option<&str>) {
    if let Some(logger) = current_for(run_id) {
        logger
            .counters
            .console_dropped
            .fetch_add(1, Ordering::Relaxed);
    }
}
pub fn archive_failed(run_id: Option<&str>) {
    if let Some(logger) = current_for(run_id) {
        logger
            .counters
            .archive_errors
            .fetch_add(1, Ordering::Relaxed);
    }
}
pub fn stats() -> Value {
    current().map(|logger| logger.stats()).unwrap_or_else(
        || {
            let pending = lock(&PENDING_DROPS);
            json!({"dropped_total": pending.full + pending.unavailable + pending.invalid, "stopped": true})
        },
    )
}
pub fn console(run_id: Option<&str>) -> Option<String> {
    current_for(run_id).and_then(|logger| lock(&logger.console).try_recv().ok())
}
pub fn archive(run_id: Option<&str>) -> Option<String> {
    current_for(run_id)
        .and_then(|logger| lock(&logger.archives).try_recv().ok())
        .map(|path| path.to_string_lossy().into_owned())
}

pub fn close(timeout_ms: u64) {
    if let Some(logger) = current() {
        logger.stopped.store(true, Ordering::Release);
        lock(&logger.sender).take();
        let _ = lock(&logger.finished).recv_timeout(Duration::from_millis(timeout_ms));
    }
}

pub fn span(name: String, parent: Option<u64>, context: Map<String, Value>) -> Span {
    let base = current()
        .map(|logger| logger.base_us)
        .unwrap_or_else(now_us);
    Span {
        id: NEXT_SPAN.fetch_add(1, Ordering::Relaxed),
        parent,
        name,
        context,
        start_us: now_us().saturating_sub(base),
    }
}

pub fn native_event(
    name: &str,
    context: Map<String, Value>,
    severity: &str,
    outcome: Option<&str>,
    spans: &[Span],
    error: Option<String>,
) {
    if let Some(logger) = current() {
        if level(severity).is_some_and(|severity| severity < logger.minimum_level) {
            return;
        }
        let (template, mut fields, mut args, kind) = match outcome {
            Some(value) => (
                format!("{name}完成"),
                vec!["outcome".into()],
                vec![json!(value)],
                "span_end",
            ),
            None => (format!("{name}开始"), vec![], vec![], "span_start"),
        };
        if let Some(error) = error {
            fields.push("error".into());
            args.push(json!(error));
        }
        logger.emit(Event { level: severity.into(), template, fields, args, metadata: json!({"component":"数据库", "kind":kind, "source":{"name":"axile.native", "function":name}}).as_object().cloned().unwrap_or_default(), context, spans: spans.to_vec() });
    }
}

fn run(
    mut writer: Writer,
    receiver: Receiver<Event>,
    console: SyncSender<String>,
    archives: SyncSender<PathBuf>,
    logger: &Logger,
) -> DropCounts {
    let mut last_stats = Value::Null;
    let mut saved_drops = DropCounts::default();
    loop {
        let event = match receiver.recv_timeout(Duration::from_millis(100)) {
            Ok(event) => Some(event),
            Err(mpsc::RecvTimeoutError::Timeout) => None,
            Err(mpsc::RecvTimeoutError::Disconnected) => break,
        };
        if let Some(event) = event {
            for event in std::iter::once(event).chain(receiver.try_iter().take(255)) {
                if writer
                    .write(&event, &console, &archives, &logger.counters)
                    .is_err()
                {
                    fail_writer(
                        &receiver,
                        logger,
                        writer.pending_records + writer.unsynced_records + 1,
                    );
                    return saved_drops;
                }
                writer.pending_records += 1;
            }
            if writer.flush_events(&logger.counters).is_err() {
                fail_writer(
                    &receiver,
                    logger,
                    writer.pending_records + writer.unsynced_records,
                );
                return saved_drops;
            }
        }
        let snapshot = logger.stats();
        if snapshot != last_stats
            && (writer.last_sync.elapsed() >= Duration::from_secs(1)
                || logger.stopped.load(Ordering::Acquire))
        {
            if writer
                .checkpoint(
                    &snapshot,
                    &mut saved_drops,
                    &console,
                    &archives,
                    &logger.counters,
                )
                .is_err()
            {
                fail_writer(&receiver, logger, writer.unsynced_records);
                return saved_drops;
            }
            last_stats = snapshot;
        }
    }
    let snapshot = logger.stats();
    if writer
        .checkpoint(
            &snapshot,
            &mut saved_drops,
            &console,
            &archives,
            &logger.counters,
        )
        .is_err()
    {
        fail_writer(&receiver, logger, writer.unsynced_records);
    }
    saved_drops
}

fn fail_writer(receiver: &Receiver<Event>, logger: &Logger, unconfirmed: u64) {
    logger.counters.write_errors.fetch_add(1, Ordering::Relaxed);
    logger
        .counters
        .unconfirmed
        .fetch_add(unconfirmed, Ordering::Relaxed);
    logger.stopped.store(true, Ordering::Release);
    lock(&logger.sender).take();
    while receiver.try_recv().is_ok() {
        logger.record_drop(DropReason::Unavailable);
    }
}

struct Writer {
    directory: PathBuf,
    run_id: String,
    base_us: u64,
    file: BufWriter<File>,
    path: PathBuf,
    index: u64,
    bytes: u64,
    max_bytes: u64,
    rotation: Rotation,
    last_time: u64,
    sequence: u64,
    // shortcut: dictionaries live for one run; bound interning if long-lived context cardinality grows.
    templates: HashMap<String, u64>,
    contexts: HashMap<String, u64>,
    spans: HashMap<u64, Span>,
    seen: HashSet<(char, u64)>,
    last_sync: std::time::Instant,
    pending_records: u64,
    unsynced_records: u64,
}

impl Writer {
    fn open(
        directory: &Path,
        run_id: &str,
        base_us: u64,
        max_bytes: u64,
        rotation: Rotation,
    ) -> io::Result<Self> {
        std::fs::create_dir_all(directory)?;
        let path = Self::path(directory, run_id, base_us, 0);
        let file = Self::create_file(&path)?;
        let mut writer = Self {
            directory: directory.into(),
            run_id: run_id.into(),
            base_us,
            file,
            path,
            index: 0,
            bytes: 0,
            max_bytes,
            rotation,
            last_time: 0,
            sequence: 0,
            templates: HashMap::new(),
            contexts: HashMap::new(),
            spans: HashMap::new(),
            seen: HashSet::new(),
            last_sync: std::time::Instant::now(),
            pending_records: 0,
            unsynced_records: 0,
        };
        writer.header()?;
        writer.file.flush()?;
        Ok(writer)
    }

    fn create_file(path: &Path) -> io::Result<BufWriter<File>> {
        let mut options = OpenOptions::new();
        options.write(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        options
            .open(path)
            .map(|file| BufWriter::with_capacity(65536, file))
    }

    fn flush_events(&mut self, counters: &Counters) -> io::Result<()> {
        self.file.flush()?;
        counters
            .written
            .fetch_add(self.pending_records, Ordering::Relaxed);
        self.unsynced_records += self.pending_records;
        self.pending_records = 0;
        Ok(())
    }

    fn path(directory: &Path, run_id: &str, time: u64, index: u64) -> PathBuf {
        let stamp = iso_time(time).replace([':', '-', '.'], "");
        directory.join(format!("axile-{stamp}-{run_id}-{index:06}.jsonl"))
    }

    fn line(&mut self, value: &Value) -> io::Result<()> {
        let mut bytes = serde_json::to_vec(value)?;
        bytes.push(b'\n');
        self.file.write_all(&bytes)?;
        self.bytes += bytes.len() as u64;
        Ok(())
    }

    fn header(&mut self) -> io::Result<()> {
        self.line(&json!(["H",1,{"run_id":self.run_id,"time_base":iso_time(self.base_us),"time_unit":"us","process_id":std::process::id()}]))
    }

    fn context(&mut self, context: &Map<String, Value>) -> io::Result<u64> {
        if context.is_empty() {
            return Ok(0);
        }
        let key = serde_json::to_string(context)?;
        let next = self.contexts.len() as u64 + 1;
        let id = *self.contexts.entry(key).or_insert(next);
        if self.seen.insert(('C', id)) {
            self.line(&json!(["C", id, context]))?;
        }
        Ok(id)
    }

    fn template(&mut self, event: &Event) -> io::Result<u64> {
        let definition = json!([event.level, event.template, event.fields, event.metadata]);
        let key = serde_json::to_string(&definition)?;
        let next = self.templates.len() as u64 + 1;
        let id = *self.templates.entry(key).or_insert(next);
        if self.seen.insert(('T', id)) {
            self.line(&json!([
                "T",
                id,
                event.level,
                event.template,
                event.fields,
                event.metadata
            ]))?;
        }
        Ok(id)
    }

    fn write(
        &mut self,
        event: &Event,
        console: &SyncSender<String>,
        archives: &SyncSender<PathBuf>,
        counters: &Counters,
    ) -> io::Result<()> {
        let now = now_us();
        if self.rotation.due(now) || self.bytes >= self.max_bytes {
            self.flush_events(counters)?;
            self.file.get_ref().sync_data()?;
            self.unsynced_records = 0;
            let previous = self.path.clone();
            self.index += 1;
            self.path = Self::path(&self.directory, &self.run_id, now, self.index);
            self.file = Self::create_file(&self.path)?;
            self.bytes = 0;
            self.seen.clear();
            self.header()?;
            if archives.try_send(previous).is_err() {
                counters.archive_errors.fetch_add(1, Ordering::Relaxed);
            }
        }
        let template = self.template(event)?;
        let context = self.context(&event.context)?;
        for span in &event.spans {
            if let Some(existing) = self.spans.get(&span.id) {
                if existing != span {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidData,
                        "conflicting span definition",
                    ));
                }
            } else {
                self.spans.insert(span.id, span.clone());
            }
            if self.seen.insert(('S', span.id)) {
                let own_context = self.context(&span.context)?;
                self.line(&json!([
                    "S",
                    span.id,
                    span.parent,
                    span.name,
                    own_context,
                    span.start_us
                ]))?;
            }
        }
        self.last_time = self.last_time.max(now.saturating_sub(self.base_us));
        self.sequence += 1;
        self.line(&json!([
            "E",
            self.sequence,
            self.last_time,
            template,
            context,
            event.spans.last().map(|span| span.id),
            event.args
        ]))?;
        let record = json!({"run_id":self.run_id,"seq":self.sequence,"timestamp":iso_time(self.base_us+self.last_time),"level":event.level,"template":event.template,"fields":event.fields,"args":event.args,"metadata":event.metadata,"context":event.context,"span_id":event.spans.last().map(|span|span.id)});
        if console.try_send(record.to_string()).is_err() {
            counters.console_dropped.fetch_add(1, Ordering::Relaxed);
        }
        Ok(())
    }

    fn checkpoint(
        &mut self,
        stats: &Value,
        saved_drops: &mut DropCounts,
        console: &SyncSender<String>,
        archives: &SyncSender<PathBuf>,
        counters: &Counters,
    ) -> io::Result<()> {
        let dropped = stats["dropped_total"].as_u64().unwrap_or(0);
        let previous = saved_drops.full + saved_drops.unavailable + saved_drops.invalid;
        if dropped > previous {
            let warning = Event {
                level: "WARNING".into(),
                template: "日志丢弃 {delta} 条，累计 {total} 条".into(),
                fields: vec!["delta".into(), "total".into()],
                args: vec![json!(dropped - previous), json!(dropped)],
                metadata: json!({"component":"系统","kind":"event"})
                    .as_object()
                    .cloned()
                    .unwrap_or_default(),
                context: Map::new(),
                spans: vec![],
            };
            self.write(&warning, console, archives, counters)?;
        }
        let event = Event {
            level: "INFO".into(),
            template: "日志统计 {stats}".into(),
            fields: vec!["stats".into()],
            args: vec![stats.clone()],
            metadata: json!({"component":"系统","kind":"event","log_stats":true})
                .as_object()
                .cloned()
                .unwrap_or_default(),
            context: Map::new(),
            spans: vec![],
        };
        let template = self.template(&event)?;
        self.sequence += 1;
        self.last_time = self.last_time.max(now_us().saturating_sub(self.base_us));
        self.line(&json!([
            "E",
            self.sequence,
            self.last_time,
            template,
            0,
            null,
            event.args
        ]))?;
        self.file.flush()?;
        // A readable checkpoint must not be counted again if its later sync fails.
        *saved_drops = DropCounts::from_stats(stats);
        self.file.get_ref().sync_data()?;
        self.unsynced_records = 0;
        self.last_sync = std::time::Instant::now();
        Ok(())
    }
}
