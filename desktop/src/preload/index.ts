import { contextBridge, ipcRenderer } from 'electron'
import type { DaemonConfig } from '../shared/daemon-config'

// 仅暴露只读的 daemon 连接配置给渲染进程（contextBridge），
// token 不出现在地址栏。设置读写经 IPC 落到主进程 fs，渲染进程无 node 直接访问。
const api = {
  getDaemonConfig: (): Promise<DaemonConfig | null> =>
    ipcRenderer.invoke('daemon:config'),
  readSettings: (projectRoot: string): Promise<Record<string, unknown>> =>
    ipcRenderer.invoke('settings:read', projectRoot),
  writeSettings: (
    projectRoot: string,
    patch: Record<string, unknown>,
  ): Promise<Record<string, unknown>> =>
    ipcRenderer.invoke('settings:write', projectRoot, patch),
}

contextBridge.exposeInMainWorld('agentApi', api)

export type AgentApi = typeof api
