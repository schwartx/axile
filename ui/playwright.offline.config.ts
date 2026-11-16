import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './e2e',
  testMatch: 'editor-offline.e2e.ts',
  workers: 1,
  timeout: 30000,
  use: { baseURL: 'http://127.0.0.1:15480', headless: true, trace: 'retain-on-failure' },
  webServer: { command: 'bun run preview --port 15480 --strictPort', url: 'http://127.0.0.1:15480/editor', reuseExistingServer: false },
})
