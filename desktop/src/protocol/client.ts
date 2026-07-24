// DaemonClient：所有功能面板（M9.3–M9.7）与 daemon 通信的唯一入口。
// 面向渲染进程（直接用浏览器原生 WebSocket 直连 daemon，符合 M9 qB 决策）；
// 也可在测试中注入 WebSocketImpl 做确定性验证。
//
// 协议信封与 agent/daemon/protocol.py 一致；重连指数退避上限 5s。

import { parseEvent } from './events'
import type {
  AgentEvent,
  Envelope,
  MsgType,
  TraceListResponse,
  TraceTreeResponse,
} from './types'

/** 浏览器/Node WebSocket 的结构化最小接口（屏蔽 websockets 版本差异，对齐 protocol.WsConnection）。 */
export interface WSLike {
  send(data: string): void
  close(): void
  onopen: (() => void) | null
  onmessage: ((ev: { data: string }) => void) | null
  onclose: ((ev: { code?: number; reason?: string }) => void) | null
  onerror: ((ev: unknown) => void) | null
  readonly readyState: number
}

export type WebSocketImpl = new (url: string) => WSLike

export interface DaemonClientOptions {
  token?: string
  /** 注入 WebSocket 实现（默认全局 WebSocket，浏览器/Node22 可用）。测试用假实现。 */
  WebSocketImpl?: WebSocketImpl
  /** 缺省 project_root；各 API 也可显式覆盖（M9.0 多项目感知）。 */
  projectRoot?: string
  /** 重连退避上限（默认 5000ms）。 */
  /** 重连退避上限（默认 5000ms）。 */
  reconnectMaxMs?: number
  /** 握手/重连超时（默认 10000ms），超时未 open 触发一次重连。 */
  connectTimeoutMs?: number
}

type MessageHandler = (env: Envelope) => void
type EventHandler = (ev: AgentEvent) => void
type CloseHandler = (info: { code?: number; reason?: string; manual: boolean }) => void

const DEFAULT_RECONNECT_MAX = 5000
const DEFAULT_CONNECT_TIMEOUT = 10000

export class DaemonClient {
  private readonly wsUrl: string
  private readonly token: string
  private readonly WebSocketImpl: WebSocketImpl
  private readonly projectRoot: string | undefined
  private readonly reconnectMax: number
  private readonly connectTimeout: number

  private ws: WSLike | null = null
  private manualClose = false
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null
  private connectTimer: ReturnType<typeof setTimeout> | null = null
  private reconnectAttempt = 0
  private currentSessionId: string | null = null
  private currentProjectRoot: string | undefined

  private readonly msgHandlers = new Map<MsgType, Set<MessageHandler>>()
  private readonly eventHandlers = new Set<EventHandler>()
  private readonly closeHandlers = new Set<CloseHandler>()

  constructor(wsUrl: string, opts: DaemonClientOptions = {}) {
    this.wsUrl = wsUrl
    this.token = opts.token ?? ''
    const impl = opts.WebSocketImpl ?? (globalThis.WebSocket as unknown as WebSocketImpl)
    if (!impl) {
      throw new Error('未提供 WebSocketImpl 且运行环境无全局 WebSocket')
    }
    this.WebSocketImpl = impl
    this.projectRoot = opts.projectRoot
    this.reconnectMax = opts.reconnectMaxMs ?? DEFAULT_RECONNECT_MAX
    this.connectTimeout = opts.connectTimeoutMs ?? DEFAULT_CONNECT_TIMEOUT
  }

  /** 建立连接；open 后自动发送 hello。返回 open 完成的 Promise。 */
  connect(): Promise<void> {
    this.manualClose = false
    return new Promise<void>((resolve, reject) => {
      const ws = new this.WebSocketImpl(this.wsUrl)
      this.ws = ws
      let settled = false

      const onOpen = (): void => {
        if (settled) return
        settled = true
        this.clearConnectTimer()
        this.reconnectAttempt = 0
        this.send('hello', { client_type: 'desktop', version: '0.1.0', token: this.token })
        // 重连后恢复会话订阅（M9.3 切换/断线恢复）。
        if (this.currentSessionId) {
          this.attach(this.currentSessionId, this.currentProjectRoot)
        }
        resolve()
      }
      const onError = (ev: unknown): void => {
        if (settled) return
        settled = true
        this.clearConnectTimer()
        reject(new Error(`daemon 连接失败: ${String(ev)}`))
      }
      const onClose = (ev: { code?: number; reason?: string }): void => {
        this.ws = null
        this.handleClose(ev)
        if (!this.manualClose) this.scheduleReconnect()
      }
      const onMessage = (ev: { data: string }): void => {
        this.dispatch(ev.data)
      }

      ws.onopen = onOpen
      ws.onerror = onError
      ws.onclose = onClose
      ws.onmessage = onMessage

      this.connectTimer = setTimeout(() => {
        if (settled) return
        settled = true
        try {
          ws.close()
        } catch {
          /* ignore */
        }
        reject(new Error('daemon 连接超时'))
      }, this.connectTimeout)
    })
  }

  /** 主动关闭并停止重连。 */
  close(): void {
    this.manualClose = true
    this.clearReconnectTimer()
    this.clearConnectTimer()
    if (this.ws) {
      try {
        this.ws.close()
      } catch {
        /* ignore */
      }
      this.ws = null
    }
  }

  private clearConnectTimer(): void {
    if (this.connectTimer !== null) {
      clearTimeout(this.connectTimer)
      this.connectTimer = null
    }
  }

  private clearReconnectTimer(): void {
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer)
      this.reconnectTimer = null
    }
  }

  private scheduleReconnect(): void {
    this.clearReconnectTimer()
    const delay = Math.min(500 * 2 ** this.reconnectAttempt, this.reconnectMax)
    this.reconnectAttempt += 1
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null
      void this.connect().catch(() => {
        /* 连接失败 -> connect() 内部 onClose 会再次 scheduleReconnect */
      })
    }, delay)
  }

  private handleClose(ev: { code?: number; reason?: string }): void {
    const info = { code: ev.code, reason: ev.reason ?? '', manual: this.manualClose }
    for (const h of this.closeHandlers) h(info)
  }

  // --------------------------------------------------------------------------- //
  // 订阅
  // --------------------------------------------------------------------------- //

  /** 订阅任意类型消息（HITL：ask/show_plan/show_skills/show_agents/notify/usage/close/session_list 等）。 */
  onMessage(type: MsgType, cb: MessageHandler): () => void {
    let set = this.msgHandlers.get(type)
    if (!set) {
      set = new Set()
      this.msgHandlers.set(type, set)
    }
    set.add(cb)
    return () => set?.delete(cb)
  }

  /** 订阅事件流（收到 event 消息后解析为 AgentEvent 派发）。 */
  onEvent(cb: EventHandler): () => void {
    this.eventHandlers.add(cb)
    return () => this.eventHandlers.delete(cb)
  }

  onClose(cb: CloseHandler): () => void {
    this.closeHandlers.add(cb)
    return () => this.closeHandlers.delete(cb)
  }

  // --------------------------------------------------------------------------- //
  // 收发
  // --------------------------------------------------------------------------- //

  private send(
    type: MsgType,
    payload: Record<string, unknown>,
    opts: { id?: string; session?: string } = {},
  ): void {
    if (!this.ws) {
      throw new Error('尚未连接 daemon，请先 connect()')
    }
    const env: Envelope = { type, payload }
    if (opts.id !== undefined) env.id = opts.id
    if (opts.session !== undefined) env.session = opts.session
    this.ws.send(JSON.stringify(env))
  }

  private dispatch(raw: string): void {
    let msg: Envelope
    try {
      msg = JSON.parse(raw) as Envelope
    } catch {
      return
    }
    if (typeof msg.type !== 'string' || !('payload' in msg)) return
    const type = msg.type as MsgType

    if (type === 'event') {
      const evRaw = msg.payload['event']
      if (evRaw !== undefined) {
        try {
          const ev = parseEvent(evRaw)
          for (const h of this.eventHandlers) h(ev)
        } catch {
          /* 非法 event 忽略 */
        }
      }
    }

    const set = this.msgHandlers.get(type)
    if (set) {
      for (const h of set) h(msg)
    }

    // 跟踪当前会话 id（供重连恢复）。
    if (type === 'session.created' || type === 'attached') {
      const sid = msg.payload['session_id']
      if (typeof sid === 'string') {
        this.currentSessionId = sid
        this.currentProjectRoot =
          (msg.payload['project_root'] as string | undefined) ?? this.currentProjectRoot
      }
    }
  }

  // --------------------------------------------------------------------------- //
  // 协议 API
  // --------------------------------------------------------------------------- //

  hello(token?: string): void {
    this.send('hello', { client_type: 'desktop', version: '0.1.0', token: token ?? this.token })
  }

  newSession(name?: string, projectRoot?: string): void {
    const pr = projectRoot ?? this.projectRoot ?? ''
    this.send('session.new', name ? { name, project_root: pr } : { project_root: pr })
  }

  attach(sessionId: string, projectRoot?: string): void {
    this.send('session.attach', {
      session_id: sessionId,
      project_root: projectRoot ?? this.projectRoot ?? '',
    })
  }

  switch(sessionId: string, projectRoot?: string): void {
    this.send('session.switch', {
      session_id: sessionId,
      project_root: projectRoot ?? this.projectRoot ?? '',
    })
  }

  listSessions(projectRoot?: string): void {
    this.send('session.list', { project_root: projectRoot ?? this.projectRoot ?? '' })
  }

  detach(): void {
    this.send('session.detach', {})
    this.currentSessionId = null
  }

  sendTask(text: string, opts: { yes?: boolean; plan?: boolean; projectRoot?: string } = {}): void {
    const payload: Record<string, unknown> = { text }
    if (opts.yes !== undefined) payload.yes = opts.yes
    if (opts.plan !== undefined) payload.plan = opts.plan
    if (opts.projectRoot !== undefined) payload.project_root = opts.projectRoot
    this.send('task.send', payload)
  }

  /** HITL 回传：answer(id, text) 对应 ask。 */
  answer(id: string, text: string): void {
    this.send('answer', { id, text }, { id })
  }

  confirmPlan(id: string, confirmed: boolean): void {
    this.send('confirm_plan', { id, confirmed }, { id })
  }

  approve(id: string, approved: boolean): void {
    this.send('approve', { id, approved }, { id })
  }

  command(name: string, args?: string | null): void {
    this.send('command', { name, args: args ?? null })
  }

  // --------------------------------------------------------------------------- //
  // M9.7 可观测面板：trace 查询（请求/响应按 envelope.id 配对）
  // --------------------------------------------------------------------------- //

  private requestResponse(respType: MsgType, payload: Record<string, unknown>): Promise<Envelope> {
    return new Promise<Envelope>((resolve, reject) => {
      const id = crypto.randomUUID()
      const off = this.onMessage(respType, (env) => {
        if (env.id === id) {
          off()
          resolve(env)
        }
      })
      // 若连接态丢失，及时 reject，避免 Promise 永久悬挂。
      const offClose = this.onClose(() => {
        off()
        offClose()
        reject(new Error('daemon 连接已断开'))
      })
      this.send(respType === 'trace_list' ? 'trace.list' : 'trace.get', payload, { id })
    })
  }

  /** 列出当前项目的 trace（按 session 聚合）。sessionId 指定则只查该会话。 */
  async listTraces(projectRoot?: string, sessionId?: string): Promise<TraceListResponse> {
    const payload: Record<string, unknown> = { project_root: projectRoot ?? this.projectRoot ?? '' }
    if (sessionId !== undefined) payload.session_id = sessionId
    const env = await this.requestResponse('trace_list', payload)
    return env.payload as TraceListResponse
  }

  /** 取单条 trace 的 span 树（traceId == session_id）。 */
  async getTrace(projectRoot: string | undefined, traceId: string): Promise<TraceTreeResponse> {
    const env = await this.requestResponse('trace_tree', {
      project_root: projectRoot ?? this.projectRoot ?? '',
      trace_id: traceId,
    })
    return env.payload as TraceTreeResponse
  }
}
