import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './e2e', testMatch: 'performance-chart.e2e.ts', workers: 1, timeout: 10000,
  use: { baseURL: 'http://127.0.0.1:15382', headless: true, trace: 'retain-on-failure' },
  webServer: {
    command: 'bun run dev --port 15382 --strictPort',
    wait: { stdout: /http:\/\/127\.0\.0\.1:15382\// },
    timeout: 20000,
  },
})
