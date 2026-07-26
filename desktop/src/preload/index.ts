import { contextBridge, ipcRenderer } from 'electron'
import type { DaemonConfig, DaemonStage } from '../shared/daemon-config'
import type { SkillInfo } from '../main/skills'

// 仅暴露只读的 daemon 连接配置给渲染进程（contextBridge），
// token 不出现在地址栏。设置读写经 IPC 落到主进程 fs，渲染进程无 node 直接访问。
const api = {
  getDaemonConfig: (): Promise<DaemonConfig | null> =>
    ipcRenderer.invoke('daemon:config'),
  // 启动遮罩：取当前后台启动阶段 / 订阅阶段推送（连接进度展示）。
  getDaemonStage: (): Promise<DaemonStage | null> =>
    ipcRenderer.invoke('daemon:progress-stage'),
  onDaemonProgress: (cb: (stage: DaemonStage) => void): (() => void) => {
    const handler = (_e: unknown, stage: DaemonStage): void => cb(stage)
    ipcRenderer.on('daemon:progress', handler)
    return () => ipcRenderer.removeListener('daemon:progress', handler)
  },
  readSettings: (projectRoot: string): Promise<Record<string, unknown>> =>
    ipcRenderer.invoke('settings:read', projectRoot),
  writeSettings: (
    projectRoot: string,
    patch: Record<string, unknown>,
  ): Promise<Record<string, unknown>> =>
    ipcRenderer.invoke('settings:write', projectRoot, patch),
  // M9.9：用户级 / 项目级作用域设置读写。
  readSettingsScoped: (
    projectRoot: string,
  ): Promise<{ user: Record<string, unknown>; project: Record<string, unknown> }> =>
    ipcRenderer.invoke('settings:readScopes', projectRoot),
  writeSettingsScoped: (
    projectRoot: string,
    patch: Record<string, unknown>,
    scope: 'user' | 'project',
  ): Promise<Record<string, unknown>> =>
    ipcRenderer.invoke('settings:writeScope', projectRoot, patch, scope),
  // M9.9：命令候选框可用技能列表。
  listSkills: (projectRoot: string): Promise<SkillInfo[]> =>
    ipcRenderer.invoke('skills:list', projectRoot),
  // M9.9：自绘顶栏窗口控制（frameless 窗口无系统按钮）。
  minimizeWindow: (): void => {
    void ipcRenderer.invoke('window:minimize')
  },
  toggleMaximizeWindow: (): void => {
    void ipcRenderer.invoke('window:toggleMaximize')
  },
  closeWindow: (): void => {
    void ipcRenderer.invoke('window:close')
  },
  reloadWindow: (): void => {
    void ipcRenderer.invoke('window:reload')
  },
  quitApp: (): void => {
    void ipcRenderer.invoke('app:quit')
  },
  openFolder: (): void => {
    void ipcRenderer.invoke('window:openFolder')
  },
  // 主进程菜单「打开项目…」选目录后推送当前项目根（见 main/index.ts 的 project:open）。
  onProjectOpen: (cb: (root: string) => void): (() => void) => {
    const handler = (_e: unknown, root: string): void => cb(root)
    ipcRenderer.on('project:open', handler)
    return () => ipcRenderer.removeListener('project:open', handler)
  },
}

contextBridge.exposeInMainWorld('agentApi', api)

export type AgentApi = typeof api
