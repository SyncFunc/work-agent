/// <reference types="vite/client" />

import type { AgentApi } from '../preload'

declare global {
  interface Window {
    agentApi: AgentApi
  }
}

export {}
