# Axile 文档站

使用 VitePress 构建，目标地址为 **https://zengbin93.github.io/axile/**。合并前的 PR 只构建和检查链接，不部署站点。

## 本地运行

需要 Bun 1.4.0。进入独立 Axile 仓库的 `docs/`：

```bash
bun install --frozen-lockfile
bun run dev
```

构建并预览与 Pages 相同的产物：

```bash
bun run build
bun run preview
```

打开预览命令输出的本地地址，并带上 `/axile/` 前缀，例如 `http://127.0.0.1:4173/axile/`。

## 目录约定

| 路径 | 内容 |
| --- | --- |
| `site/` | 面向读者的 Markdown 页面 |
| `site/public/` | 图标和流程示意图 |
| `.vitepress/config.mts` | 导航、搜索、语言和 `/axile/` 部署前缀 |
| `.vitepress/theme/` | 文档站样式与可用键盘操作的截图放大 |
| `site/assets/screenshots/` | 原始截图，位于站点源目录内，供开发服务与构建统一读取 |
| `site/examples/` | 正文引用的 Python 与 CSV 示例 |
| `.vitepress/dist/` | 生成产物，不提交 |

站点从 `site/` 生成页面。Markdown 通过相对路径引用图片和示例代码，构建时打包实际引用的资源。

图片统一保存在 `site/assets/screenshots/`，每张图需要写清 alt 和图说。文档中的数值为示例，连接账户时填写自己的有效凭据。

## GitHub Pages 首次启用

首次发布前，由仓库管理员设置：

1. 进入仓库 **Settings → Pages**。
2. 将 **Build and deployment → Source** 设为 **GitHub Actions**。
3. 合并文档 PR 到 `main`，或手动运行 `Documentation` 工作流。

`.github/workflows/docs.yml` 在 PR 上使用只读权限构建，在 `main` 的文档变更或手动运行时上传产物，并通过单独的部署任务发布。PR 不申请 Pages 写权限，也不创建部署环境。

站点的 `base` 为 `/axile/`，页面地址保留 `.html`，可以在 GitHub Pages 上直接访问和刷新。改用自定义域名时，要同步修改 `base`、sitemap、图标路径和本页的网址。

## 提交前验证

运行 `bun run build` 检查构建和 Markdown 死链接。在 `bun run dev` 和 `bun run preview` 两种模式下检查全部章节的图片加载，再检查首页、章节导航、中文搜索、代码复制、截图放大与移动端布局。依赖使用 `package.json` 中的固定版本及 `bun.lock`，CI 使用冻结锁文件安装。
