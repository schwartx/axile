# React + TypeScript + Vite

This template provides a minimal setup to get React working in Vite with HMR and some Oxlint rules.

Currently, two official plugins are available:

- [@vitejs/plugin-react](https://github.com/vitejs/vite-plugin-react/blob/main/packages/plugin-react) uses [Oxc](https://oxc.rs)
- [@vitejs/plugin-react-swc](https://github.com/vitejs/vite-plugin-react/blob/main/packages/plugin-react-swc) uses [SWC](https://swc.rs/)

## Type Checking And Build

Run `bun install --frozen-lockfile` to install the pinned dependencies.
`bun run typecheck` uses [tsrs](https://github.com/oxc-project/tsrs) in build
mode to check the app, Vite configuration, and tests through the project
references in `tsconfig.json`. `bun run build` runs the same check before
Vite produces the production assets; Vite remains responsible for bundling.

tsrs is pinned to an exact prerelease version in `package.json` and `bun.lock`.
The `typescript` dependency is retained for editor and tooling compatibility,
but the build and typecheck scripts no longer invoke `tsc`.
The pinned tsrs release provides native binaries for Linux x64/arm64 and
macOS arm64, not Windows or Intel macOS.

## React Compiler

The React Compiler is not enabled on this template because of its impact on dev & build performances. To add it, see [this documentation](https://react.dev/learn/react-compiler/installation).

## Expanding the Oxlint configuration

If you are developing a production application, we recommend enabling type-aware lint rules by installing `oxlint-tsgolint` and editing `.oxlintrc.json`:

```json
{
  "$schema": "./node_modules/oxlint/configuration_schema.json",
  "plugins": ["react", "typescript", "oxc"],
  "options": {
    "typeAware": true
  },
  "rules": {
    "react/rules-of-hooks": "error",
    "react/only-export-components": ["warn", { "allowConstantExport": true }]
  }
}
```

See the [Oxlint rules documentation](https://oxc.rs/docs/guide/usage/linter/rules) for the full list of rules and categories.

## 错误处理维护

错误归属、全局断连收口和只读恢复约定见 [错误展示与连接恢复](docs/error-handling.md)。
新增错误入口须声明操作语义；恢复回调只允许读取，不得重放写操作。
