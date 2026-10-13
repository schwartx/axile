import { defineConfig } from '@playwright/test'
export default defineConfig({
  testDir: './e2e', testMatch: 'control.e2e.ts', workers: 1, timeout: 20000,
  use: { baseURL: 'http://127.0.0.1:15381', headless: true, trace: 'retain-on-failure' },
  webServer: { command: 'bun run dev --port 15381 --strictPort', url: 'http://127.0.0.1:15381/e2e/control.html', timeout: 20000, reuseExistingServer: true },
})
