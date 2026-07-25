// 主进程与渲染进程共享的类型（由 contextBridge 注入渲染进程的 daemon 连接配置）。
export interface DaemonConfig {
  /** WebSocket 地址，如 ws://127.0.0.1:18789 */
  wsUrl: string
  /** 本机回环鉴权 token（daemon 未配置时为空串，表示无需鉴权） */
  token: string
  /** 健康检查地址，如 http://127.0.0.1:18790/health */
  healthUrl: string
}
