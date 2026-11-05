import { defineConfig } from '@playwright/test'
export default defineConfig({
  testDir: './e2e', testMatch: 'replay-heat.e2e.ts', workers: 1, timeout: 15000,
  use: { baseURL: 'http://127.0.0.1:15383', headless: true, trace: 'retain-on-failure' },
  webServer: { command: 'bunx --no-install vite --port 15383 --strictPort', wait: { stdout: /http:\/\/127\.0\.0\.1:15383\// }, timeout: 20000 },
})
