# 联机异常的核心表征探针

这些探针用于复核 #108—#112 的代码归因，不属于交易运行入口，也不代表柜台验收通过。原生 API 使用 Mock，目录来源与 worker IPC 使用桩，数据库仅使用内存 SQLite；不读取真实凭据，不发送真实交易请求。

在 Axile 仓库根目录、安装开发依赖及 `axile[ctp]` 后运行：

```bash
PYTHONPATH=. uv run python 飞泰CTP测试/diagnostics/termination.py
PYTHONPATH=. uv run python 飞泰CTP测试/diagnostics/quota_recovery.py
PYTHONPATH=. uv run python 飞泰CTP测试/diagnostics/boundaries.py
```

`quota_recovery.py` 复用公开仓库的账户测试构造器，需要源码中的 `tests/`。这些文件不纳入运行 wheel，也不作为“正确行为”的回归门禁；断言用于确认排查时的缺陷表征。核心修复后，应改写成解决方案文档列出的正向回归，旧探针输出/断言发生变化属于预期。

- `termination.py`：真实 CTP executor/session/guard 在终止后的查询门禁和共享节流上中断清理，原生调用计数为 0；不包含飞泰测试算法。
- `quota_recovery.py`：日限额 1，模拟旧 worker 未刷盘而退出；重建 guard 后又能本地放行一次。这里只记录本地放行，不冒充真实报单或成交。
- `boundaries.py`：目录负查询错误码被聚合丢失、组合函数失败主动丢弃 worker、零目标合约参与阻塞行情、日期桶选择使覆盖降级，以及 100 个上下文上限。

实测环境、证据强度、方案与未解决部分见同目录上级的 `问题排查与解决方案.md`。请先读归因边界，勿将所有被保护阻断或资源上限直接解释为产品 bug。
