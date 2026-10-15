import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './e2e', testMatch: ['orders.e2e.ts', 'holdings.e2e.ts'], workers: 1, timeout: 20000,
  use: { baseURL: 'http://127.0.0.1:1448', headless: true, trace: 'retain-on-failure' },
  webServer: [
    { command: '../../.venv/bin/python -m uvicorn tests.orders_app:app --app-dir .. --host 127.0.0.1 --port 1449', url: 'http://127.0.0.1:1449/docs', reuseExistingServer: true },
    { command: 'bun run dev --port 1448 --strictPort', url: 'http://127.0.0.1:1448/e2e/orders.html', env: { AXILE_API_TARGET: 'http://127.0.0.1:1449' }, reuseExistingServer: true },
  ],
})
