import type { AgentApi } from '../preload'

// 渲染进程通过 contextBridge 注入的只读 API（见 src/preload/index.ts）。
declare global {
  interface Window {
    agentApi: AgentApi
  }
}

export {}
