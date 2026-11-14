use std::collections::HashMap;
use wasm_bindgen::prelude::*;
mod account;

/// Normalized depth intervals: start, end, price, quantity (NaN means unknown).
#[wasm_bindgen]
pub struct Heat {
    rows: Vec<f64>,
}

#[wasm_bindgen]
impl Heat {
    #[wasm_bindgen(constructor)]
    pub fn new(rows: Vec<f64>) -> Result<Heat, JsError> {
        if rows.len() % 4 != 0
            || rows.chunks_exact(4).any(|r| {
                !r[0].is_finite()
                    || !r[1].is_finite()
                    || r[0].fract() != 0.0
                    || r[1].fract() != 0.0
                    || r[0].abs() > 9007199254740991.0
                    || r[1].abs() > 9007199254740991.0
                    || r[1] < r[0]
                    || !r[2].is_finite()
                    || r[2] <= 0.0
                    || (!r[3].is_nan() && (!r[3].is_finite() || r[3] < 0.0))
            })
            || rows
                .chunks_exact(4)
                .zip(rows.chunks_exact(4).skip(1))
                .any(|(a, b)| a[1] > b[1])
        {
            return Err(JsError::new("Invalid depth intervals"));
        }
        Ok(Heat { rows })
    }

    /// Output: x, y, quantity, unknown flag, min price, max price.
    pub fn query(
        &self,
        start: f64,
        end: f64,
        min: f64,
        max: f64,
        tick: f64,
        left: f64,
        right: f64,
        top: f64,
        bottom: f64,
    ) -> Result<Vec<f64>, JsError> {
        if ![start, end, min, max, tick, left, right, top, bottom]
            .iter()
            .all(|v| v.is_finite())
            || end <= start
            || max <= min
            || tick <= 0.0
            || right <= left
            || bottom <= top
        {
            return Err(JsError::new("Invalid heat viewport"));
        }
        let row_height = ((bottom - top) * tick / (max - min)).clamp(2.0, 18.0);
        let mut lo = 0;
        let mut hi = self.rows.len() / 4;
        while lo < hi {
            let mid = (lo + hi) / 2;
            if self.rows[mid * 4 + 1] < start {
                lo = mid + 1;
            } else {
                hi = mid;
            }
        }
        let mut indexes = HashMap::new();
        let mut cells: Vec<f64> = Vec::new();
        for r in self.rows[lo * 4..].chunks_exact(4) {
            if r[0] > end {
                break;
            }
            if r[2] < min || r[2] > max {
                continue;
            }
            let from = left + (r[0].max(start) - start) / (end - start) * (right - left);
            let until = (left + (r[1] - start) / (end - start) * (right - left)).min(right);
            let raw_y = top + (max - r[2]) / (max - min) * (bottom - top);
            let y = if row_height == 2.0 {
                (raw_y / 2.0).floor() * 2.0 + 1.0
            } else {
                raw_y
            };
            let mut x = (from / 3.0).floor() * 3.0;
            while x <= from.max(until - 1.0) {
                let key = (x as i64, if y == 0.0 { 0 } else { y.to_bits() });
                if let Some(&i) = indexes.get(&key) {
                    let i: usize = i;
                    if !r[3].is_nan() {
                        cells[i + 2] = if cells[i + 2].is_nan() {
                            r[3].max(0.0)
                        } else {
                            cells[i + 2].max(r[3])
                        };
                    }
                    if r[3].is_nan() {
                        cells[i + 3] = 1.0;
                    }
                    cells[i + 4] = cells[i + 4].min(r[2]);
                    cells[i + 5] = cells[i + 5].max(r[2]);
                } else {
                    indexes.insert(key, cells.len());
                    cells.extend_from_slice(&[x, y, r[3], f64::from(r[3].is_nan()), r[2], r[2]]);
                }
                x += 3.0;
            }
        }
        Ok(cells)
    }
}
