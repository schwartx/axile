# Python 编辑器与离线草稿

组合函数、账户通知、系统通知和初始化向导共用本地 Python 编辑器。类型检查、补全、悬停、签名、源码跳转、引用、重命名、符号树、折叠、快速修复及格式化在 Dedicated Worker 内的 ty WASM 中运行，不再连接服务端 LSP。

## 使用

本地代码辅助默认关闭。点击“下载并开启”后，进度条合计显示 WASM 和类型资源包的实际读取字节；下载后初始化分析器，成功后自动开启。同一浏览器的编辑器共用组件缓存和开关偏好。关闭辅助仍可输入、高亮、搜索、撤销和保存草稿，静态检查显示“未启用”。

在线打开编辑器时自动准备离线页面及必要 JS/CSS，不预下载 WASM 或类型包。代码持续保存到本机 IndexedDB。状态栏的“已保存到本地”和页面的“已保存到服务器”分别表示两种保存结果。点击“本地草稿”进入 `/editor`，确认“可离线使用”后，即可断网刷新或重新打开工作台；离线代码辅助还需提前完成组件下载。

离线工作台复用现有代码区和问题面板，不启动账户查询、轮询或业务事件流。组合及通知编辑地址在断网刷新时，也可打开对应的本地草稿。保存到服务器和真实环境试跑仍需联网；不会排队或自动提交交易配置。

重新在线打开原页面时，只有服务端代码与草稿基线一致才自动恢复未提交内容。基线不同时由用户选择恢复本地代码、下载代码或放弃旧草稿。多标签页使用 IndexedDB 事务内的版本检查，冲突时停止覆盖并保留当前编辑内容。存储失败会显示错误，尚未落盘的修改在离开页面时触发浏览器确认。

草稿按站点、编辑类型和原页面地址隔离。浏览器清理站点数据会删除草稿与离线资源，可下载 `.py` 文件另存。离线导航需要 HTTPS 或 localhost 安全上下文；开发模式只提供本地语言分析，完整离线导航使用生产构建。

## 资源和版本

Service Worker 仅预缓存页面静态资源，不缓存业务 API 或写请求。页面版本等待旧页面关闭后生效，不强制刷新正在编辑的页面。WASM 和源码包使用独立的 Cache Storage 缓存，按资源 URL 中的内容哈希复用，下载不完整时不标记组件就绪。组件版本更新或缓存被清理后，需再次点击下载，原开关偏好保留。

标准库类型由 ty 内置。资源包另含当前 Axile 公共源码和构建脚本列出的 Python 依赖，不复制运行配置、账户数据或额外安装的私有渠道。未收录的导入会保留缺失依赖诊断。类型包随 Axile 发布，并记录构建时的依赖版本；单独升级服务器第三方依赖后应重建资源，服务端试跑与执行仍使用实际运行环境。

WASM 和源码包使用 zstd level 19 压缩，以 `.bin` 资源分发，由 Worker 内随包携带的 `fzstd` 流式解码器解压，不依赖浏览器原生 zstd 接口，也不生成 gzip 或 Brotli 备用包。解码器无需 CDN，离线仍可使用。两份资源合计约 9.33 MB，比原 gzip 版本的 13.75 MB 减少约 32%；类型、源码与编辑能力保持完整，源码包包含第三方许可声明。

## 构建和验证

`ui/scripts/build-editor.ts` 固定 Ruff/ty 源码提交 `36b92bb23ebca423672c37501299e06b836a060e`、下载校验和与 `rust/editor-wasm/Cargo.lock`。`rust/editor-wasm/axile.rs` 是同一上游 Workspace 中的最小 LSP 和入口契约扩展。源码快照与 Cargo target 放在系统临时目录，禁用 sccache 和增量构建。

编辑器和回放统一使用 wasm-bindgen **0.2.130**。构建前安装匹配 CLI 和 `wasm32-unknown-unknown` target，然后运行 `bun run build`。开发模式的 `bun run dev` 也准备 WASM；指纹未改变时复用已生成的编辑器产物。

定向单测：`bun test src/features/editor src/components/ui/pythonQuickFixRequests.test.ts src/components/ui/pythonLanguageFeatures.test.ts`。Python 和 WASM 共用 `tests/fixtures/editor_contract.json` 的冻结契约样例。

生产构建后运行 `bun x playwright test --config playwright.offline.config.ts`，覆盖真实 Service Worker 的断网刷新、重新打开、草稿恢复及本地检查。常规编辑器交互继续由 `e2e/editor.e2e.ts` 覆盖。发行验证检查 wheel 内的 Worker、压缩 WASM、类型包与 Service Worker。
