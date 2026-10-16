import { defineConfig } from '@playwright/test'
export default defineConfig({
  testDir: './e2e', testMatch: 'futures-close.e2e.ts', workers: 1, timeout: 30000,
  use: { baseURL: 'http://127.0.0.1:1458', headless: true, trace: 'retain-on-failure' },
  webServer: [
    { command: 'uv run --project .. uvicorn tests.futures_close_app:app --app-dir .. --host 127.0.0.1 --port 1459', url: 'http://127.0.0.1:1459/docs', reuseExistingServer: true },
    { command: 'bun run dev --port 1458 --strictPort', url: 'http://127.0.0.1:1458/e2e/futures-close.html', env: { AXILE_API_TARGET: 'http://127.0.0.1:1459' }, reuseExistingServer: true },
  ],
})
