import { defineConfig } from 'electron-vite'
import react from '@vitejs/plugin-react'

// electron-vite 约定：src/main（主进程）、src/preload（桥接）、src/renderer（React）。
// 主进程/预加载进程把 `electron` 标记为 external，运行时由 Electron 提供。
export default defineConfig({
  main: {
    build: {
      rollupOptions: {
        external: ['electron'],
      },
    },
  },
  preload: {
    build: {
      rollupOptions: {
        external: ['electron'],
      },
    },
  },
  renderer: {
    plugins: [react()],
  },
})
