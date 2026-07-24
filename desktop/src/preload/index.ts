import { contextBridge, ipcRenderer } from 'electron'
import type { DaemonConfig } from '../shared/daemon-config'

// 仅暴露只读的 daemon 连接配置给渲染进程（contextBridge），
// token 不出现在地址栏，也不暴露任何命令执行能力。
const api = {
  getDaemonConfig: (): Promise<DaemonConfig | null> =>
    ipcRenderer.invoke('daemon:config'),
}

contextBridge.exposeInMainWorld('agentApi', api)

export type AgentApi = typeof api
