//! Bounded raw-block decoder; never owns a recording or a global identity set.

use axile_analysis_core::replay::{Batch, BatchRequest, Derivation};
use axile_analysis_core::replay_interval::Interval;
use flate2::read::ZlibDecoder;
use serde::Deserialize;
use serde_json::value::RawValue;
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::io::Read;
use wasm_bindgen::prelude::*;

const RAW_LIMIT: usize = 512 * 1024 + 1;

#[derive(Deserialize)]
struct Event<'a> {
    schema_version: u8,
    capture_seq: u64,
    time_us: u64,
    topic: &'a str,
    #[serde(borrow)]
    data: &'a RawValue,
}

fn decode(bytes: &[u8], expected: usize) -> Result<Vec<u8>, String> {
    if bytes.len() > 600 * 1024 || expected > RAW_LIMIT || expected == 0 {
        return Err("REPLAY_DECODE_BUDGET".into());
    }
    let mut output = Vec::with_capacity(expected);
    ZlibDecoder::new(bytes)
        .take(expected as u64 + 1)
        .read_to_end(&mut output)
        .map_err(|e| e.to_string())?;
    if output.len() != expected || output.last() != Some(&b'\n') {
        return Err("REPLAY_DECODE_LENGTH".into());
    }
    Ok(output)
}

#[wasm_bindgen]
pub struct RawBlock {
    bytes: Vec<u8>,
    offset: usize,
}

#[wasm_bindgen]
impl RawBlock {
    #[wasm_bindgen(constructor)]
    pub fn new(bytes: &[u8], expected: usize) -> Result<RawBlock, JsError> {
        Ok(Self {
            bytes: decode(bytes, expected).map_err(|e| JsError::new(&e))?,
            offset: 0,
        })
    }

    /// At most 16 events per event-loop turn; only explicit bounded preview fields leave WASM.
    pub fn step(&mut self, request: &str) -> Result<String, JsError> {
        self.advance(request).map_err(|e| JsError::new(&e))
    }

    /// A stream keeps only its current raw block; heads determine the global capture order.
    pub fn head(&self) -> Result<Option<f64>, JsError> {
        self.next()
            .map(|row| row.map(|(_, event)| event.capture_seq as f64))
            .map_err(|e| JsError::new(&e))
    }

    pub fn apply_next(&mut self, batch: &mut BusinessBatch) -> Result<(), JsError> {
        self.apply_one(&mut batch.inner)
            .map_err(|e| JsError::new(&e))
    }
    pub fn apply_interval_next(&mut self, interval: &mut IntervalAnalysis) -> Result<(), JsError> {
        let Some((size, _)) = self.next().map_err(|e| JsError::new(&e))? else {
            return Ok(());
        };
        let event: Value = serde_json::from_slice(&self.bytes[self.offset..self.offset + size])
            .map_err(|e| JsError::new(&e.to_string()))?;
        self.offset += size + 1;
        interval.inner.observe(&event).map_err(|e| JsError::new(&e))
    }
}

#[wasm_bindgen]
pub struct BusinessBatch {
    inner: Batch,
}

#[wasm_bindgen]
impl BusinessBatch {
    pub fn links(&mut self, seq: f64, rows: &str) -> Result<(), JsError> {
        if rows.len() > 16 * 1024
            || !seq.is_finite()
            || seq.fract() != 0.0
            || seq < 1.0
            || seq > 9_007_199_254_740_991.0
        {
            return Err(JsError::new("REPLAY_BATCH_LINKS"));
        }
        let rows: Vec<Value> =
            serde_json::from_str(rows).map_err(|_| JsError::new("REPLAY_BATCH_LINKS"))?;
        let rows = rows
            .iter()
            .map(|v| v["object"].as_u64().map(|id| (id, v["conflict"] == true)))
            .collect::<Option<Vec<_>>>()
            .ok_or_else(|| JsError::new("REPLAY_BATCH_LINKS"))?;
        self.inner
            .links(seq as u64, rows)
            .map_err(|e| JsError::new(&e))
    }
    #[wasm_bindgen(constructor)]
    pub fn new(request: &str) -> Result<BusinessBatch, JsError> {
        if request.len() > 256 * 1024 {
            return Err(JsError::new("REPLAY_BATCH_BUDGET"));
        }
        let request: BatchRequest =
            serde_json::from_str(request).map_err(|_| JsError::new("REPLAY_BATCH_REQUEST"))?;
        Ok(Self {
            inner: Batch::new(request).map_err(|e| JsError::new(&e))?,
        })
    }
    pub fn results(&self) -> Result<String, JsError> {
        let result = serde_json::to_string(&self.inner.results())
            .map_err(|_| JsError::new("REPLAY_BATCH_RESULT"))?;
        if result.len() > 256 * 1024 {
            return Err(JsError::new("REPLAY_BATCH_RESULT_BUDGET"));
        }
        Ok(result)
    }
    pub fn fences(&mut self) -> Result<String, JsError> {
        serde_json::to_string(&self.inner.fences()).map_err(|e| JsError::new(&e.to_string()))
    }
    pub fn snapshot(&self) -> Result<String, JsError> {
        serde_json::to_string(self.inner.states()).map_err(|e| JsError::new(&e.to_string()))
    }
    pub fn version(&mut self, generation: f64) -> Result<(), JsError> {
        if !generation.is_finite()
            || generation < 0.0
            || generation > 9_007_199_254_740_991.0
            || generation.fract() != 0.0
        {
            return Err(JsError::new("REPLAY_GENERATION"));
        }
        self.inner.version(generation as u64);
        Ok(())
    }
}

#[wasm_bindgen]
pub struct IntervalAnalysis {
    inner: Interval,
}
#[wasm_bindgen]
impl IntervalAnalysis {
    #[wasm_bindgen(constructor)]
    pub fn new(input: &str) -> Result<IntervalAnalysis, JsError> {
        if input.len() > 16 * 1024 {
            return Err(JsError::new("REPLAY_INTERVAL_BUDGET"));
        }
        let input =
            serde_json::from_str(input).map_err(|_| JsError::new("REPLAY_INTERVAL_REQUEST"))?;
        Ok(Self {
            inner: Interval::new(&input).map_err(|e| JsError::new(&e))?,
        })
    }
    pub fn feed(&mut self, input: &str) -> Result<(), JsError> {
        if input.len() > 256 * 1024 {
            return Err(JsError::new("REPLAY_INTERVAL_BUDGET"));
        }
        let input =
            serde_json::from_str(input).map_err(|_| JsError::new("REPLAY_INTERVAL_REQUEST"))?;
        self.inner.feed(&input).map_err(|e| JsError::new(&e))
    }
    pub fn begin_second(&mut self) -> Result<(), JsError> {
        self.inner.begin_second().map_err(|e| JsError::new(&e))
    }
    pub fn results(&mut self) -> Result<String, JsError> {
        let result = self
            .inner
            .results()
            .map_err(|e| JsError::new(&e))?
            .to_string();
        if result.len() > 256 * 1024 {
            return Err(JsError::new("REPLAY_INTERVAL_RESULT_BUDGET"));
        }
        Ok(result)
    }
}

#[wasm_bindgen]
pub struct BusinessDerivation {
    inner: Derivation,
}
#[wasm_bindgen]
impl BusinessDerivation {
    pub fn needed(&self) -> bool {
        self.inner.needed()
    }
    #[wasm_bindgen(constructor)]
    pub fn new(request: &str) -> Result<BusinessDerivation, JsError> {
        if request.len() > 256 * 1024 {
            return Err(JsError::new("REPLAY_DERIVE_BUDGET"));
        }
        let value: Value =
            serde_json::from_str(request).map_err(|e| JsError::new(&e.to_string()))?;
        Ok(Self {
            inner: Derivation::new(&value).map_err(|e| JsError::new(&e))?,
        })
    }
    pub fn feed(&mut self, request: &str) -> Result<(), JsError> {
        if request.len() > 256 * 1024 {
            return Err(JsError::new("REPLAY_DERIVE_BUDGET"));
        }
        let value: Value =
            serde_json::from_str(request).map_err(|e| JsError::new(&e.to_string()))?;
        self.inner.feed(&value).map_err(|e| JsError::new(&e))
    }
    pub fn results(&self) -> Result<String, JsError> {
        let result = serde_json::to_string(&self.inner.results())
            .map_err(|e| JsError::new(&e.to_string()))?;
        if result.len() > 256 * 1024 {
            return Err(JsError::new("REPLAY_DERIVE_RESULT_BUDGET"));
        }
        Ok(result)
    }
}

impl RawBlock {
    fn next(&self) -> Result<Option<(usize, Event<'_>)>, String> {
        if self.offset == self.bytes.len() {
            return Ok(None);
        }
        let size = self.bytes[self.offset..]
            .iter()
            .position(|b| *b == b'\n')
            .ok_or("REPLAY_EVENT_FRAMING")?;
        let event: Event<'_> = serde_json::from_slice(&self.bytes[self.offset..self.offset + size])
            .map_err(|e| e.to_string())?;
        if event.schema_version != 2 {
            return Err("REPLAY_EVENT_SCHEMA".into());
        }
        Ok(Some((size, event)))
    }
    fn apply_one(&mut self, batch: &mut Batch) -> Result<(), String> {
        let Some((size, _)) = self.next()? else {
            return Err("REPLAY_BLOCK_EXHAUSTED".into());
        };
        let event: Value = serde_json::from_slice(&self.bytes[self.offset..self.offset + size])
            .map_err(|e| e.to_string())?;
        batch.apply(&event)?;
        self.offset += size + 1;
        Ok(())
    }
    fn advance(&mut self, request: &str) -> Result<String, String> {
        let input: Value = serde_json::from_str(request).map_err(|e| e.to_string())?;
        let wanted = input["sequences"]
            .as_array()
            .ok_or("REPLAY_PREVIEW_REQUEST")?;
        if wanted.len() > 33 {
            return Err("REPLAY_PREVIEW_BUDGET".into());
        }
        let mut events = Vec::new();
        for _ in 0..16 {
            if self.offset == self.bytes.len() {
                break;
            }
            let size = self.bytes[self.offset..]
                .iter()
                .position(|b| *b == b'\n')
                .ok_or("REPLAY_EVENT_FRAMING")?;
            let event: Event<'_> =
                serde_json::from_slice(&self.bytes[self.offset..self.offset + size])
                    .map_err(|e| e.to_string())?;
            self.offset += size + 1;
            if event.schema_version != 2 {
                return Err("REPLAY_EVENT_SCHEMA".into());
            }
            if !wanted
                .iter()
                .any(|seq| seq.as_u64() == Some(event.capture_seq))
            {
                continue;
            }
            let data: BTreeMap<&str, &RawValue> =
                serde_json::from_str(event.data.get()).map_err(|e| e.to_string())?;
            let mut preview = json!({"capture_seq":event.capture_seq,"time_us":event.time_us,"topic":event.topic,"data":{}});
            for name in [
                "symbol",
                "execution_id",
                "order_id",
                "trade_key",
                "identity_quality",
                "source",
                "status",
                "phase",
                "boundary",
                "book_valid",
                "valid_until_us",
                "last_price",
                "price",
                "quantity",
                "direction",
                "position_side",
                "offset",
            ] {
                if let Some(value) = data.get(name) {
                    if value.get().len() > 2048 {
                        let text: String = serde_json::from_str(value.get())
                            .map_err(|_| "REPLAY_PREVIEW_FIELD_BUDGET")?;
                        preview["data"][name] = json!(format!(
                            "{}…（预览截断）",
                            text.chars().take(256).collect::<String>()
                        ));
                        continue;
                    }
                    preview["data"][name] =
                        serde_json::from_str(value.get()).map_err(|e| e.to_string())?;
                }
            }
            if Some(event.capture_seq) == input["quote_seq"].as_u64() {
                for side in ["bids", "asks"] {
                    if let Some(value) = data.get(side) {
                        let rows: Vec<&RawValue> =
                            serde_json::from_str(value.get()).map_err(|e| e.to_string())?;
                        let mut output = Vec::new();
                        for row in rows.iter().take(20) {
                            if row.get().len() > 2048 {
                                return Err("REPLAY_DEPTH_ROW_BUDGET".into());
                            }
                            output
                                .push(serde_json::from_str(row.get()).map_err(|e| e.to_string())?);
                        }
                        preview["data"][side] = Value::Array(output);
                    }
                }
                for name in ["available_depth", "field_quality"] {
                    if let Some(value) = data.get(name) {
                        if value.get().len() > 2048 {
                            return Err("REPLAY_PREVIEW_FIELD_BUDGET".into());
                        }
                        preview["data"][name] =
                            serde_json::from_str(value.get()).map_err(|e| e.to_string())?;
                    }
                }
            }
            events.push(preview);
        }
        let result = json!({"done":self.offset == self.bytes.len(),"events":events}).to_string();
        if result.len() > 128 * 1024 {
            return Err("REPLAY_PREVIEW_BUDGET".into());
        }
        Ok(result)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    #[test]
    fn raw_numbers_preserve_the_original_float() {
        let value: Value = serde_json::from_str(r#"{"quantity":192512.80118503195}"#).unwrap();
        assert_eq!(
            value["quantity"].as_f64().unwrap().to_bits(),
            192512.80118503195_f64.to_bits()
        );
    }
    #[test]
    fn long_identity_preview_does_not_reject_or_replace_the_original_identity() {
        let original = "单".repeat(1000);
        let text=json!({"schema_version":2,"recording_id":"R","capture_seq":1,"time_us":1,"topic":"/trade","data":{"trade_key":"T","order_id":original,"identity_quality":"stable","price":10,"quantity":1}}).to_string()+"\n";
        let mut preview = RawBlock {
            bytes: text.as_bytes().to_vec(),
            offset: 0,
        };
        let result: Value = serde_json::from_str(
            &preview
                .advance(r#"{"sequences":[1],"quote_seq":null}"#)
                .unwrap(),
        )
        .unwrap();
        assert!(result["events"][0]["data"]["order_id"]
            .as_str()
            .unwrap()
            .ends_with("…（预览截断）"));
        let mut batch=Batch::new(serde_json::from_value(json!({"recording":"R","through_seq":1,"cutoff_seq":1,"subjects":[{"id":1,"kind":"trade","key":"T"}]})).unwrap()).unwrap();
        let mut full = RawBlock {
            bytes: text.into_bytes(),
            offset: 0,
        };
        full.apply_one(&mut batch).unwrap();
        assert_eq!(batch.results()[0]["data"]["order_id"], original);
    }

    #[test]
    fn bounded_decode_and_exact_observation_selection() {
        let text = (1..=34).map(|seq| json!({"schema_version":2,"capture_seq":seq,"time_us":10,"topic":"/trade","data":{"trade_key":"T","quantity":seq}}).to_string()+"\n").collect::<String>();
        let mut compressor =
            flate2::write::ZlibEncoder::new(Vec::new(), flate2::Compression::fast());
        compressor.write_all(text.as_bytes()).unwrap();
        let compressed = compressor.finish().unwrap();
        assert!(decode(&compressed, text.len() - 1).is_err());
        assert!(decode(&compressed, RAW_LIMIT + 1).is_err());
        let mut block = RawBlock {
            bytes: decode(&compressed, text.len()).unwrap(),
            offset: 0,
        };
        let mut found = Vec::new();
        for _ in 0..3 {
            let result: Value = serde_json::from_str(
                &block
                    .advance(r#"{"sequences":[1,33],"quote_seq":null}"#)
                    .unwrap(),
            )
            .unwrap();
            found.extend(result["events"].as_array().unwrap().iter().cloned());
        }
        assert_eq!(found.len(), 2);
        assert_eq!(found[1]["data"]["quantity"], 33);
        assert_eq!(block.offset, text.len());
    }
}
