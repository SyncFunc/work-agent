/// <reference types="vite/client" />

import type { DaemonConfig } from '../shared/daemon-config'

declare global {
  interface Window {
    agentApi: {
      getDaemonConfig: () => Promise<DaemonConfig | null>
    }
  }
}

export {}
