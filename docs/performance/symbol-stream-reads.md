# 品种汇总按批读取执行记录

基线为 Axile main `4502383`。生产改动仅在 `account_schedule.py` 的 `/account/{account_id}/activity/symbols`：不读取未使用的 `raw_input`，将 `.all()` 改为每批 100 条的 `stream_scalars`，正常结束、异常或取消时关闭结果游标。

时间过滤、执行排序、成交转换、品种筛选、分组顺序、浮点累加顺序及原 `summarize` 保持不变。没有新增表、缓存或迁移，没有更改 API 返回或前端行为。概览仍不发布成交明细，展开某一品种仍返回其全部匹配成交。

## 完整 HTTP 对照

同一 macOS、Python 3.12.12、SQLite 3.50.4、SQLAlchemy 2.0.52 环境，原版和候选交替各三轮，使用全新进程、独立可写数据库副本和相同线程设置。完整导入应用，通过真实 FastAPI 路由及中间件发起 ASGI HTTP；不运行交易/调度生命周期，禁止出站连接。文件缓存为热缓存，不强制 GC。

请求组合覆盖完整时间窗的品种概览、逐个展开实际品种、固定时间范围及不匹配的品种/时间。逐项比较状态码、完整响应长度和 SHA256，没有剔除字段，全部相同。统计整组 HTTP 耗时；系统 `ru_maxrss` 包含应用导入和全部请求；稳定 RSS 为结束后 1 秒内采样中位数。SQLite JSON 解码字节数使用两组相同的计数包装统计。

真实 CHEN 来源保持全部原始内容，26 条执行、23 条非空输入。封存源文件 SHA256：

`27745d4b7722e5c5769ca56d9f502503503b0eba4edd0cefd6f7e0dbd7222a99`

规模案例复用上一轮的生成方式：保留完整原始数据库，从 CHEN 完整输入/结果的原始 SQLite 文本循环复制，改变新增执行的标识与时间，扩到 2,000 条执行。合成休市记录也保留在副本中，本接口不读取它们。**这是合成重复负载，不是真实 2,000 次交易或四渠道覆盖。** 生成文件 SHA256：

`d710564ade4c08e5f4e5f3b48af2d8a6a723eb63338de6c01a0adf6a4bbf048e`

三轮中位数：

| 指标 | 真实 CHEN 原版 | 真实 CHEN 候选 | 规模原版 | 规模候选 |
|---|---:|---:|---:|---:|
| 每轮 HTTP 请求数 | 7 | 7 | 8 | 8 |
| 全部请求耗时，秒 | 0.3917 | 0.3888 | 1.1699 | 1.0096 |
| 整进程峰值，MiB | 197.98 | 197.95 | 269.67 | 205.78 |
| 请求后稳定 RSS，MiB | 197.97 | 197.94 | 269.66 | 205.77 |
| SQLite 交给 Python 解码的 JSON 合计，MiB | 0.440 | 0.364 | 61.590 | 50.115 |

规模场景耗时减少约 14%，峰值与稳定占用减少约 64 MiB（24%）；真实小样本基本持平。分批读取减少原始执行对象同时驻留；跳过输入减少无用解析。原成交集合仍按现有契约保留，内存仍随成交数量增长，不是恒定内存方案。展开每个品种仍需读取时间窗内的结果，本次没有消除这些重复查询。该结果不能外推为整个线上服务的常驻内存收益。

## 验收证据

相关核心原有及新增测试合计 49 项通过；匹配 Axon main `7bbf3a25` 的发行层 160 项通过、1 项跳过。Ruff、C901≤15、修改生产文件的 BasedPyright 通过。

新回归跨越三个读取批次，对照完整成交顺序及原公式汇总；实测 205 条执行读取时会话同时持有的执行对象不超过 101 条。2 MiB 无关输入不进入 JSON 解码器。另覆盖空匹配、概览/展开行为，以及处理异常和取消时关闭游标。既有接口测试的异步会话替身同步适配流式读取。

未修改前端代码或部署线上服务。

## 复现

使用同一个已安装开发依赖的 Python，保留基线工作区：

```bash
git worktree add --detach /ABS/BASELINE 4502383
PYTHONPATH=/ABS/CANDIDATE python -m scripts.benchmark_symbol_reads /ABS/CANDIDATE /PRIVATE/CHEN_0033.db /NEW/PRIVATE/REAL --baseline /ABS/BASELINE --runs 3
PYTHONPATH=/ABS/CANDIDATE python -m scripts.benchmark_symbol_reads /ABS/CANDIDATE /PRIVATE/CHEN_0033.db /NEW/PRIVATE/SCALE --baseline /ABS/BASELINE --runs 3 --scale-records 2000
```

输入须为已 checkpoint 的封存副本，包含完整账户 2 执行，输出目录须不存在。规模生成复用 `benchmark_journal_pages.scale_copy`，只修改私有副本；源文件哈希在结束时重新核对。每个子进程最长 60 秒。输出包括工作库及私有日志，不应入库。

本次报告：`/tmp/axile-symbol-real-v1/summary.json`、`/tmp/axile-symbol-scale-v1/summary.json`。
