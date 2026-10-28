import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './e2e', testMatch: 'browser-analysis.e2e.ts', workers: 1, timeout: 30000,
  use: { baseURL: 'http://127.0.0.1:15384', headless: true, trace: 'retain-on-failure' },
  webServer: { command: './node_modules/.bin/vite --port 15384 --strictPort', url: 'http://127.0.0.1:15384/e2e/browser-analysis.html', timeout: 20000, reuseExistingServer: true },
})
