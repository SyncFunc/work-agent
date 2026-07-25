import { defineConfig } from 'electron-vite'
import react from '@vitejs/plugin-react'

// electron-vite 约定：src/main（主进程）、src/preload（桥接）、src/renderer（React）。
// 主进程/预加载进程把 `electron` 标记为 external，运行时由 Electron 提供。
export default defineConfig({
  // 强制 main / preload 输出为 CJS（.cjs）。Electron 对 CJS preload 的支持最稳定，
  // ESM preload 在 31.x 下常因解析/加载问题导致 window.agentApi 未注入（白屏）。
  main: {
    build: {
      rollupOptions: {
        external: ['electron'],
        output: { format: 'cjs', entryFileNames: 'index.cjs' },
      },
    },
  },
  preload: {
    build: {
      rollupOptions: {
        external: ['electron'],
        output: { format: 'cjs', entryFileNames: 'index.cjs' },
      },
    },
  },
  renderer: {
    plugins: [react()],
  },
})
