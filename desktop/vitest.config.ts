import { defineConfig } from 'vitest/config'

// 协议库为纯数据 + 网络逻辑，单测在 node 环境跑（不依赖 Electron/DOM）。
// DaemonClient 通过注入 WebSocketImpl 做确定性测试。
export default defineConfig({
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts'],
  },
})
