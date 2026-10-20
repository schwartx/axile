# 冻结快照复用原生执行值，降低组装峰值

基线为 Axile main `85216de`。生产改动仅在 `performance_analysis.compute_batch`：执行列表已由数据库 JSON 值、原转换函数和标量构成，冻结时直接使用该列表，省去 `AccountPerformance.model_dump` 对整份执行历史的再次复制。

绩效模型的其他字段仍按原方式序列化，执行字段保留原位置；短区间仍不重复存储执行列表。没有改公式、精度、顺序、缺失数据处理、表结构、版本、接口或前端。写入器只读取这些投影值，不在发布时修改嵌套字典。

## 验证方法的修正

原 ASGI 基准中，验证客户端与应用在同一个进程，RSS 包含客户端解析、规范化和哈希响应的开销。它可用于原版/候选的同条件比较，但不能直接称为纯服务端占用。本次原 ASGI 初测的整进程峰值约 332 → 334 MiB，没有改善；不能只凭计算阶段少了约 42 MiB 就判定整链路通过。

新增 `benchmark_analysis_http.py`：服务进程运行真实 AnalysisManager、完整 FastAPI 应用和 Uvicorn，绑定独立 loopback socket；验证客户端在另一进程发起真实 HTTP 请求。两组均使用新方法重测，采样服务进程 RSS，系统峰值取服务进程 `ru_maxrss`，另保存验证客户端峰值。两种方法的绝对数值不直接相减。

完整链路包含历史读取、计算、发布、三个区间快照、完整绩效及成本汇总的 HTTP 读取。稳定 RSS 在请求完成、服务仍监听时采样 1 秒。不强制 GC，不启用分配分析器。交易、订阅及调度生命周期关闭，禁止服务进程出站连接；验证端只连接该服务的临时本地端口。

同一 macOS、Python 3.12.12、SQLite 3.50.4、SQLAlchemy 2.0.52，基线和候选交替各三轮，每轮全新服务/客户端进程和独立数据库副本。HTTP 链路耗时包括启动验证客户端、读取及验证响应，不能当成纯服务处理延迟。

## 数据与一致性

三类数据保持区分，沿用上一阶段封存副本，未清空输入：

| 来源 | 执行 | 成交 | SHA256 |
|---|---:|---:|---|
| 真实 CHEN | 26 | 5 | `27745d4b7722e5c5769ca56d9f502503503b0eba4edd0cefd6f7e0dbd7222a99` |
| CHEN 完整载荷合成扩展 | 2,000 | 385 | `747a5688626c02b815af3c07b6c853658b4b410e166e6328a6994c1926a599a3` |
| 20 品种独立合成压力样本 | 5,000 | 0 | `889f394beeb0907b41567864e0498828fe5f760f82b00225c34f017db666af44` |

后两类不是独立真实交易历史，20 品种压力样本不作为成交兼容性的唯一证据。所有运行均对照接口全部 JSON 业务值和数组顺序，仅规范化对象键顺序并排除新生成的顶层 snapshot_id/computed_at。另逐行哈希实际落库的快照设置、原始 ranges 文本、版本、账户，以及全部成本执行/成交的非 snapshot_id 字段与原始 payload 文本，结果完全相同。原始源文件哈希始终未变。

## 三轮中位数

以下均为同方法重测的服务端数据：

| 指标 | 真实原版 | 真实候选 | CHEN 扩展原版 | CHEN 扩展候选 | 20 品种压力原版 | 20 品种压力候选 |
|---|---:|---:|---:|---:|---:|---:|
| 服务端峰值，MiB | 198.31 | 197.95 | 238.73 | 236.42 | 328.86 | 285.92 |
| 请求后稳定 RSS，MiB | 198.28 | 197.92 | 238.69 | 236.38 | 283.53 | 283.27 |
| 加载、计算、发布，秒 | 0.3625 | 0.3636 | 0.6173 | 0.6037 | 1.7127 | 1.6665 |
| worker 至 HTTP 验证完成，秒 | 0.5515 | 0.5552 | 0.9352 | 0.9201 | 2.3905 | 2.3457 |
| 应用导入至验证完成，秒 | 1.3991 | 1.3974 | 1.7819 | 1.7731 | 3.2455 | 3.1891 |

宽持仓压力样本的服务端整链路峰值减少约 **43 MiB（13%）**，空闲占用基本不变，整段耗时减少约 2%。真实小样本与窄持仓扩展样本收益很小。本次价值在降低宽持仓历史组装时的峰值，不宣称常驻占用或页面响应有明显改善。

该样本独立验证客户端峰值约 140 MiB，两组基本相同，不计入服务端 RSS。服务端仍承担完整应用、数据库引擎、计算和 HTTP 服务的开销，没有关闭生产模块来换取结果。

## 检查与边界

相关核心 139 项通过；匹配 Axon main `7bbf3a25` 的发行层 160 项通过、1 项跳过；Ruff、C901≤15、修改生产文件的 BasedPyright 通过。

新增回归将复用后的冻结值与原模型 JSON 序列化契约逐项比较，覆盖微小浮点手续费的完整精度、真实零持仓、空集合和原始输入不被修改。实际落库文本对照同时验证字段顺序及完整数值。未来投影若加入非 JSON 原生 Python 对象，需要重新处理序列化契约，不能默认直接复用。

原始 JSON 读取、必要快照本身和应用导入基线仍存在。本次不进行存储迁移；未修改前端，未部署，未验证联机账户和完整线上生命周期。

## 复现

```bash
git worktree add --detach /ABS/BASELINE 85216de
PYTHONPATH=/ABS/CANDIDATE python -m scripts.benchmark_analysis_http /ABS/CANDIDATE /PRIVATE/CHEN_0033.db /NEW/PRIVATE/REAL --baseline /ABS/BASELINE --data-kind real
PYTHONPATH=/ABS/CANDIDATE python -m scripts.benchmark_analysis_http /ABS/CANDIDATE /PRIVATE/CHEN_REPEAT_2000.db /NEW/PRIVATE/REPEAT --baseline /ABS/BASELINE --data-kind chen-repeated
PYTHONPATH=/ABS/CANDIDATE python -m scripts.benchmark_analysis_http /ABS/CANDIDATE /PRIVATE/CHEN_0033.db /NEW/PRIVATE/SYNTHETIC --baseline /ABS/BASELINE --synthetic-records 5000 --symbols 20
```

使用同一个装有开发依赖的 Python；输入为已 checkpoint 的封存副本，输出目录须不存在。每个服务进程最长 60 秒；客户端验证和服务启动/关闭均有独立超时。脚本复用原有源文件保护、工作库复制、三轮对照及落库哈希工具；原 `benchmark_analysis_memory` 命令默认仍运行 ASGI 模式。

原始报告：`/tmp/axile-freeze-http-real-v1`、`/tmp/axile-freeze-http-chen-scale-v1`、`/tmp/axile-freeze-http-scale-v1`。ASGI 初测：`/tmp/axile-freeze-copy-probe`。输出包含私有工作库和日志，不应入库。
