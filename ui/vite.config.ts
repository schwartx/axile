import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'node:path'
import { editorOffline } from './scripts/editorOffline.ts'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss(), editorOffline()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    // 仅绑定 IPv4 回环，避免默认 localhost 解析到 IPv6 ::1。
    host: '127.0.0.1',
    port: 1420,
    proxy: {
      // 后端 axile 服务（本机 loopback）。dev 下把 /api 透传过去。
      '/api': {
        target: process.env.AXILE_API_TARGET ?? 'http://127.0.0.1:1419',
        changeOrigin: true,
        ws: true,
        rewriteWsOrigin: true,
      },
    },
  },
  preview: {
    host: '127.0.0.1',
    port: 1420,
  },
  build: {
    // FastAPI 以 SPA fallback 同源托管此目录（见 README「启动」）。
    outDir: '../axile/server/_static',
    emptyOutDir: true,
    rolldownOptions: {
      output: {
        codeSplitting: {
          groups: [
            { name: 'react-vendor', test: /[\\/]node_modules[\\/](?:react|react-dom|scheduler|react-router)[\\/]/ },
            { name: 'lezer', test: /[\\/]node_modules[\\/]@lezer[\\/]/ },
            { name: 'codemirror', test: /[\\/]node_modules[\\/](?:@codemirror|@uiw)[\\/]/ },
          ],
        },
      },
    },
  },
})
