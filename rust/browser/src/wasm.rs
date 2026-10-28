use crate::BrowserStore;
use serde_json::Value;
use sqlite_wasm_rs::vfs::VfsFilesManager;
use sqlite_wasm_vfs::sahpool::{install, OpfsSAHPoolCfgBuilder, OpfsSAHPoolUtil};
use wasm_bindgen::prelude::*;

fn error(value: impl std::fmt::Display) -> JsValue {
    js_sys::Error::new(&value.to_string()).into()
}
fn parse(value: &str) -> Result<Value, JsValue> {
    serde_json::from_str(value).map_err(error)
}

#[wasm_bindgen]
pub struct AnalysisDatabase {
    store: Option<BrowserStore>,
    pool: OpfsSAHPoolUtil,
}
impl Drop for AnalysisDatabase {
    fn drop(&mut self) {
        self.store = None;
        let _ = self.pool.pause();
    }
}
#[wasm_bindgen]
pub async fn open_analysis(
    key: String,
    account: i64,
    build: String,
) -> Result<AnalysisDatabase, JsValue> {
    if key.len() != 64 || !key.bytes().all(|c| c.is_ascii_hexdigit()) {
        return Err(error("Invalid browser database key"));
    }
    let cfg = OpfsSAHPoolCfgBuilder::new()
        .directory(".axile-analysis-incremental")
        .build();
    let pool = install::<sqlite_wasm_rs::WasmOsCallback>(&cfg, true)
        .await
        .map_err(error)?;
    pool.resume().await.map_err(error)?;
    let opened = async {
        pool.ensure_capacity(pool.len().map_err(error)? + 2)
            .await
            .map_err(error)?;
        BrowserStore::new(key, account, build).map_err(error)
    }
    .await;
    match opened {
        Ok(store) => Ok(AnalysisDatabase {
            store: Some(store),
            pool,
        }),
        Err(err) => {
            let _ = pool.pause();
            Err(err)
        }
    }
}
#[wasm_bindgen]
impl AnalysisDatabase {
    pub fn metadata(&self, state: &str) -> Result<String, JsValue> {
        self.store
            .as_ref()
            .ok_or_else(|| error("Database closed"))?
            .metadata(&parse(state)?)
            .map(|v| v.to_string())
            .map_err(error)
    }
    pub fn begin(&mut self, state: &str) -> Result<String, JsValue> {
        self.store
            .as_mut()
            .ok_or_else(|| error("Database closed"))?
            .begin(parse(state)?)
            .map(|v| v.to_string())
            .map_err(error)
    }
    pub fn page(&mut self, page: &str) -> Result<String, JsValue> {
        let page = parse(page)?;
        self.store
            .as_mut()
            .ok_or_else(|| error("Database closed"))?
            .page(page)
            .map(|v| v.to_string())
            .map_err(error)
    }
    pub fn job(&self) -> Result<String, JsValue> {
        self.store
            .as_ref()
            .ok_or_else(|| error("Database closed"))?
            .job()
            .map(|v| v.to_string())
            .map_err(error)
    }
    pub fn calculate(&mut self, computed_at: &str) -> Result<String, JsValue> {
        self.store
            .as_mut()
            .ok_or_else(|| error("Database closed"))?
            .calculate(computed_at)
            .map(|v| v.to_string())
            .map_err(error)
    }
    pub fn query(&self, request: &str) -> Result<String, JsValue> {
        self.store
            .as_ref()
            .ok_or_else(|| error("Database closed"))?
            .query(&parse(request)?)
            .map(|v| v.to_string())
            .map_err(error)
    }
    pub fn close(&mut self) -> Result<(), JsValue> {
        self.store = None;
        self.pool.pause().map_err(error)
    }
}
