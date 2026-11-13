use wasm_bindgen::prelude::*;

#[wasm_bindgen]
pub struct AccountAnalysis(axile_analysis_core::account::Account);
#[wasm_bindgen]
impl AccountAnalysis {
    #[wasm_bindgen(constructor)]
    pub fn new(input: &str) -> Result<Self, JsError> {
        if input.len() > 16 * 1024 * 1024 {
            return Err(JsError::new("窗口超过 16 MiB，请缩短范围"));
        }
        let input = serde_json::from_str(input).map_err(|e| JsError::new(&e.to_string()))?;
        axile_analysis_core::account::Account::new(input)
            .map(Self)
            .map_err(|e| JsError::new(&e))
    }
    pub fn query(&mut self, query: &str) -> Result<String, JsError> {
        let query = serde_json::from_str(query).map_err(|e| JsError::new(&e.to_string()))?;
        let result = self.0.query(query).map_err(|e| JsError::new(&e))?;
        serde_json::to_string(&result).map_err(|e| JsError::new(&e.to_string()))
    }
}

#[wasm_bindgen]
pub struct ReplayAnalysis(axile_analysis_core::book::Book);
#[wasm_bindgen]
impl ReplayAnalysis {
    #[wasm_bindgen(constructor)]
    pub fn new(input: &str) -> Result<Self, JsError> {
        if input.len() > 16 * 1024 * 1024 {
            return Err(JsError::new("回放窗口超过 16 MiB，请缩短范围"));
        }
        let input: axile_analysis_core::book::Book =
            serde_json::from_str(input).map_err(|e| JsError::new(&e.to_string()))?;
        input.validate().map_err(|e| JsError::new(&e))?;
        Ok(Self(input))
    }
    pub fn query(&self, query: &str) -> Result<String, JsError> {
        let query = serde_json::from_str(query).map_err(|e| JsError::new(&e.to_string()))?;
        let result = self.0.query(query).map_err(|e| JsError::new(&e))?;
        serde_json::to_string(&result).map_err(|e| JsError::new(&e.to_string()))
    }
}
