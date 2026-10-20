# 成本明细接口按字段读取

基线为 Axile main `66feb7a`。本次仅缩小成本接口的读取投影：

- 快照读取仅返回版本、data_until及所选区间的baseline/end，不再向Python发送整个settings/ranges。
- 成交页仅返回关联执行的标识字段，不再逐条传输关联执行payload。

SQLite表结构、存储、聚合表达式、筛选、排序、游标、版本约束和返回契约均不改变。使用已有json_extract/json_type，多路径提取返回JSON文本，由Python解析，避免把false变成整数、损失历史数字词法的精度。字段类型标记区分缺失和显式null，缺失字段保持异常，不制造null或零。

## 对照方式

同一Python 3.12.12环境，SQLite 3.50.4、SQLAlchemy 2.0.52。每组为全新进程、独立可写数据库副本；包含完整FastAPI应用/路由装配及真实ASGI成本HTTP响应。两组均不启动交易/调度生命周期，不允许出站连接。基线和候选交替各三轮，OS文件缓存为热缓存，不强制GC；数据源哈希始终未变。

覆盖三个区间、四个维度、两种排序，以及真实record_id、symbol/search、day、时间边界、buy/sell/none、空匹配、百分号字面量搜索。比较首页、下一页、尾页，以及游标条件变更400和快照不存在410。每次响应的状态码、完整body字节哈希及长度逐项相同，不排除任何业务字段或快照标识。

进程峰值取系统ru_maxrss；稳定RSS为请求结束后1秒内中位数。另记录实际交给SQLite JSON反序列化器的字节长度，两组使用同样的计数包装和json.loads。以下结果为三轮中位数。

## 数据与结果

真实性参考保留完整CHEN来源事实：26条CTP执行、23条非空raw_input。两份冻结快照由真实v0.22.0写入器在其一致副本上生成，包含26条成本执行及5条成交；不能冒充CHEN原始冻结历史。

参考文件SHA256：`1486c17817cbe27e8390f262e0e4d8f2b20d8f7deaae1f3bbe3b0674add1efec`。

规模文件是在CHEN副本上循环保留完整执行输入/结果结构、改变标识和发生时间，扩到2000条执行，再由原写入器生成冻结结果；包含385条成交。它是合成重复负载，不是真实2000次历史，也不代表四渠道压力覆盖。

规模文件SHA256：`bc6bdab2d2c099033a8ecb1fdcb531f095604a3a552d7bb5eae58070718998e5`。

| 指标 | CHEN参考原版 | CHEN参考候选 | 规模原版 | 规模候选 |
|---|---:|---:|---:|---:|
| 每轮HTTP请求数 | 53 | 53 | 77 | 77 |
| 请求组合耗时，秒 | 0.7403 | 0.7320 | 1.8564 | 1.2381 |
| 进程峰值，MiB | 199.88 | 199.64 | 429.30 | 206.06 |
| 请求后稳定RSS，MiB | 199.86 | 199.63 | 429.27 | 206.05 |
| SQLite交给Python解码的JSON合计，MiB | 2.107 | 0.374 | 109.256 | 0.529 |
| 单次最大JSON解码，字节 | 38329 | 4261 | 1835628 | 4261 |

规模样本中请求组合约快33%，峰值减少约223MiB（52%）。收益来自成本浏览场景避免反复创建整份快照对象；小样本耗时/内存改善很小。SQLite仍需扫描原JSON，不能把Python传输/解码减少比例当作磁盘I/O下降比例，也不能推断整个在线服务常驻内存减半。该测量不覆盖其他路由或长期服务生命周期。

## 其他验收

核心相关136项测试通过；配套Axon main `7bbf3a25` 的发行层160项通过、1项跳过；Ruff、C901≤15和修改生产文件的BasedPyright通过。原权重表基准脚本复用采样器后，默认流程也完成一次原版/候选冒烟对照，响应一致。新增测试实测2MiB无关快照字段和1MiB无关执行字段不进入Python JSON解码器，并检查null、false、0、负零和非规范历史数值词法的完整浮点精度。未修改前端、未部署。

## 复现

先保留基线工作区，使用同一装有开发依赖的Python：

```bash
git worktree add --detach /ABS/BASELINE 66feb7a
PYTHONPATH=/ABS/CANDIDATE python -m scripts.benchmark_cost_reads /ABS/CANDIDATE FROZEN_0033.db /NEW/PRIVATE/OUTPUT --baseline /ABS/BASELINE --runs 3
```

输入须是已checkpoint的封存副本；输出目录必须不存在。每个子进程限时60秒，输出包含私有工作库和日志；原始数据库及业务内容不入库。报告/日志仅记指标、哈希、筛选标识，不输出原始执行载荷；仍应保存在私有目录。

本次原始报告：`/tmp/axile-cost-reads-real-v1/summary.json`、`/tmp/axile-cost-reads-scale-v1/summary.json`。
