//! Browser-only analysis over an OPFS SQLite database.
#![allow(dead_code)]

#[path = "../../storage/src/calendar.rs"]
mod calendar;
#[path = "../../storage/src/contributions.rs"]
mod contributions;
#[path = "../../storage/src/cost_summary.rs"]
mod cost_summary;
#[path = "../../storage/src/costs.rs"]
mod costs;
#[path = "../../storage/src/error.rs"]
mod error;
#[path = "../../storage/src/execution_summary.rs"]
mod execution_summary;
#[path = "../../storage/src/execution_symbol_display.rs"]
mod execution_symbol_display;
#[path = "../../storage/src/fact_attempts.rs"]
mod fact_attempts;
#[path = "../../storage/src/fact_cost_rows.rs"]
mod fact_cost_rows;
#[path = "../../storage/src/fact_events.rs"]
mod fact_events;
#[path = "../../storage/src/fact_headers.rs"]
mod fact_headers;
#[path = "../../storage/src/fact_integrity.rs"]
mod fact_integrity;
#[path = "../../storage/src/fact_observation.rs"]
mod fact_observation;
#[path = "../../storage/src/fact_positions.rs"]
mod fact_positions;
#[path = "../../storage/src/fact_prices.rs"]
mod fact_prices;
#[path = "../../storage/src/fact_sizing.rs"]
mod fact_sizing;
#[path = "../../storage/src/fact_ticks.rs"]
mod fact_ticks;
#[path = "../../storage/src/fact_trades.rs"]
mod fact_trades;
#[path = "../../storage/src/fact_transactions.rs"]
mod fact_transactions;
#[path = "../../storage/src/performance.rs"]
mod performance;
#[path = "../../storage/src/performance_ranges.rs"]
mod performance_ranges;
#[path = "../../storage/src/publication.rs"]
mod publication;
#[path = "../../storage/src/replay.rs"]
mod replay;
#[path = "../../storage/src/result_normalization.rs"]
mod result_normalization;
#[path = "../../storage/src/result_reader.rs"]
mod result_reader;
#[path = "../../storage/src/snapshots.rs"]
mod snapshots;
#[path = "../../storage/src/views.rs"]
mod views;

#[path = "../../storage/src/native_publication/task.rs"]
pub(crate) mod calculation_task;
mod native_publication {
    pub(crate) use crate::calculation_task as task;
}
#[path = "../../storage/src/native_publication/staging.rs"]
mod staging;

#[path = "../../storage/src/browser_fact_schema.rs"]
mod browser_fact_schema;
mod engine;
mod local;
mod store;
pub use store::BrowserStore;
#[cfg(target_arch = "wasm32")]
mod wasm;
