//! Native calculation, bounded disk staging and atomic SQLite publication.
//!
//! Only a small receipt crosses the Python boundary; result rows stay in Rust.

pub(crate) mod copy;
mod staging;
mod task;
mod writer;

use std::path::Path;

use serde_json::{json, Value};

use crate::error::AnalysisError;
use crate::{publication, with_read_snapshot, writer_scope};

use staging::Staging;
use task::Task;

/// Calculate outside the durable writer transaction, then compare-and-publish.
pub fn publish(database: &Path, request: &Value) -> Result<Value, AnalysisError> {
    let task = Task::parse(request)?;
    let connection = writer_scope::open_writer(database)?;
    if let Some(receipt) = writer::already_published(&connection, &task)? {
        return Ok(receipt);
    }
    let mut staged = Staging::new(&connection)?;
    let result = with_read_snapshot(database, |source| {
        if let Some(reason) = writer::stale_reason(source, &task)? {
            return Ok(Err(reason));
        }
        let mut calculation = request.clone();
        calculation["include_execution_summaries"] = json!(true);
        let complete = publication::write(source, &calculation, &mut |table, columns, rows| {
            staged
                .insert_rows(table, columns, rows)
                .map_err(|error| format!("{}:{}", error.code, error.error))
        })
        .map_err(AnalysisError::from)?;
        Ok(Ok(complete))
    })?;
    let complete = match result {
        Ok(complete) => complete,
        Err(reason) => {
            // The staging transaction never wrote main. End it before obtaining
            // main's writer slot so no old main reader is upgraded into a writer.
            connection
                .execute_batch("ROLLBACK;")
                .map_err(|error| AnalysisError::new("PUBLICATION_STAGE", error.to_string()))?;
            return writer::skip(&connection, &task, reason);
        }
    };
    let complete = staged.finish(complete, task.account_id, task.source_version)?;
    writer::publish(&connection, &task, &complete)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn invalid_store_request_never_opens_database() {
        let error = publish(
            Path::new("/nonexistent.sqlite"),
            &json!({"command":"publish_store"}),
        )
        .unwrap_err();
        assert_eq!(error.code, "INVALID_REQUEST");
    }
}
