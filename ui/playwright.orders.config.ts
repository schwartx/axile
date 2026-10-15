import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './e2e', testMatch: ['orders.e2e.ts', 'holdings.e2e.ts'], workers: 1, timeout: 20000,
  use: { baseURL: 'http://127.0.0.1:15382', headless: true, trace: 'retain-on-failure' },
  // 等 Vite 宣告就绪，避免无监听端口的 HTTP 预探测在部分 WSL 网络环境中长时间挂起。
  webServer: { command: 'bun run dev --port 15382 --strictPort', wait: { stdout: /http:\/\/127\.0\.0\.1:15382\// }, timeout: 20000, reuseExistingServer: false },
})
