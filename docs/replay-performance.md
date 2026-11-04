# 回放图拖动与热力图计算

主图拖动按动画帧合并视图更新，手势中复用热力图、成交气泡和委托离屏图层，松手后精确补绘。时间范围和价格范围分别记录预览坐标；轴和网格按当前视图绘制。新增视野尚未补绘时，图例标识为历史预览，不能把空白解释为没有挂量。结果更新期间不使用过期坐标进行详情命中。

热力图由单个 Dedicated Worker 中的 Rust WASM 内核计算。每次加载标的数据时，按录制段规范化盘口有效区间，打包为 Float64Array，再转移到 Worker；不转移业务投影自身的数据。内核保留数据，按有序区间二分查询，再对可见档位做像素聚合。不同录制段独立聚合，保留峰值、未知标记和价格范围。

Worker 调度只保留一个正在执行的请求和一个最新待处理视图。正在执行的同步 WASM 计算不会被取消；旧结果不会覆盖新视图。切换数据或卸载会终止 Worker。加载或计算失败显示错误，不将失败解释为没有行情。

委托生命周期按已读取的完整范围构建，视图移动不再重新整理全部生命周期。成交分组和历史图层在手势中固定，按新坐标变换；播放游标及同微秒采集序号规则保留。

## 构建

需要 Rust、wasm32-unknown-unknown target，以及与 crate 精确匹配的 wasm-bindgen CLI：

```bash
rustup target add wasm32-unknown-unknown
cargo install wasm-bindgen-cli --version 0.2.128 --locked
cd ui
bun run build:heat
bun test src/features/replay
bun run build
```

`dev` 和 `build` 自动生成 WASM。直接运行 `bun test` 前需先运行 `build:heat`。生成的 JS、声明和 WASM 在 `ui/src/features/replay/heat-wasm/`，不提交。Vite 将 Worker 和 WASM 作为有哈希的 assets 输出到现有 `_static`，随 wheel 一起打包。

构建默认使用系统临时目录中按工作树路径区分的 `axile-replay-heat-*` 目录；可用 `REPLAY_HEAT_TARGET_DIR` 覆盖，不与原生 Python 扩展共享 target。构建入口清空 RUSTC_WRAPPER 并禁用增量编译。

## 验证与测量

```bash
cd ui
bun scripts/benchmark-replay-heat.ts
./node_modules/.bin/playwright test --config playwright.replay.config.ts --reporter=line
```

2026-11-04 本机 Chromium headless、默认 1280×720/DPR 1 合成数据验收：

| 场景 | 本次结果 |
| --- | --- |
| 1 万条盘口、20 万档位、5 千笔成交的主图拖动 | 35 个采样帧，帧间隔 P95 约 16.8 ms |
| 2 万条盘口、40 万档位，JS 热力图聚合 | 7 次热查询中位数 34.1 ms |
| 同数据 Worker/WASM 查询，含回传及结果整理 | 7 次热查询中位数 9.2 ms |
| 同数据一次性初始化，含打包、Worker/WASM 加载 | 109 ms |

纯内核 Bun 测量（不包含 Worker 消息传输），同次执行：

| 盘口数 / 总档位数 | JS 中位数 | WASM 中位数 | 打包及装载 |
| --- | --- | --- | --- |
| 1 万 / 20 万 | 15.93 ms | 4.59 ms | 34.61 ms |
| 5 万 / 100 万 | 68.94 ms | 19.24 ms | 142.21 ms |
| 10 万 / 200 万 | 130.53 ms | 36.86 ms | 482.08 ms |

这些是合成数据、热查询的单次环境记录，不是用户真实录制或所有设备的性能保证。浏览器拖动没有改动前的同环境帧率基线，不能据此宣称帧率提升倍数。普通 CI 验证语义、缓存和请求调度，不使用毫秒阈值作为门禁。

初始化打包仍在主线程，数据特别大时会产生一次性停顿。Canvas 栅格化也仍在主线程；本次没有引入 WebGL、OffscreenCanvas、共享内存或永久多视口缓存。
