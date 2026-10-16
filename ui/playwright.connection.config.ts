import { defineConfig } from '@playwright/test'
import config from './playwright.orders.config'

// 连接用例拦截全部业务接口，沿用现有 Vite 启动方式，不启动交易服务。
export default defineConfig(config, { testMatch: ['errors.e2e.ts', 'connection-validation.e2e.ts'] })
