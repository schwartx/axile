import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './e2e', testMatch: 'orders.e2e.ts', workers: 1, timeout: 20000,
  use: { baseURL: 'http://127.0.0.1:15382', headless: true, trace: 'retain-on-failure' },
  webServer: { command: 'bun run dev --port 15382 --strictPort', url: 'http://127.0.0.1:15382/e2e/orders.html', timeout: 20000, reuseExistingServer: false },
})
