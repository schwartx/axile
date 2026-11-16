import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './e2e',
  testMatch: '*.e2e.ts',
  testIgnore: ['editor-offline.e2e.ts', 'wasm-loading.e2e.ts', 'replay-restore.e2e.ts'],
  workers: 1,
  timeout: 30000,
  use: { baseURL: 'http://127.0.0.1:1438', headless: true, trace: 'retain-on-failure' },
  webServer: [
    { command: 'uv run --project .. uvicorn tests.editor_app:app --app-dir .. --host 127.0.0.1 --port 1439', url: 'http://127.0.0.1:1439/docs', reuseExistingServer: false },
    { command: 'bun run dev --port 1438 --strictPort', url: 'http://127.0.0.1:1438/e2e/editor.html', env: { AXILE_API_TARGET: 'http://127.0.0.1:1439' }, reuseExistingServer: false },
  ],
})
