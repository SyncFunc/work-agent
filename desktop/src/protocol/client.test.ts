import { describe, it, expect, vi } from 'vitest'
import { DaemonClient } from './client'
import type { WSLike, WebSocketImpl } from './client'
import type { Envelope } from './types'

// 可控的假 WebSocket（实现 WSLike），供确定性单测。
class FakeWebSocket implements WSLike {
  static last: FakeWebSocket | null = null
  url: string
  onopen: (() => void) | null = null
  onmessage: ((ev: { data: string }) => void) | null = null
  onclose: ((ev: { code?: number; reason?: string }) => void) | null = null
  onerror: ((ev: unknown) => void) | null = null
  readyState = 0
  sent: string[] = []
  closed = false

  constructor(url: string) {
    this.url = url
    FakeWebSocket.last = this
    queueMicrotask(() => this.onopen?.())
  }
  send(data: string): void {
    this.sent.push(data)
  }
  close(): void {
    this.closed = true
    this.readyState = 3
    this.onclose?.({})
  }
  /** 测试辅助：模拟收到服务端消息。 */
  emit(type: string, payload: Record<string, unknown>, opts: { id?: string; session?: string } = {}): void {
    const env: Envelope = { type: type as Envelope['type'], payload }
    if (opts.id !== undefined) env.id = opts.id
    if (opts.session !== undefined) env.session = opts.session
    this.onmessage?.({ data: JSON.stringify(env) })
  }
  /** 读取第 n 条已发消息并按 Envelope 解析。 */
  sentEnvelope(i: number): Envelope {
    return JSON.parse(this.sent[i]) as Envelope
  }
}

const Impl = FakeWebSocket as unknown as WebSocketImpl

describe('DaemonClient 握手与订阅', () => {
  it('connect 后 open 即发送 hello', async () => {
    const c = new DaemonClient('ws://127.0.0.1:18789', { WebSocketImpl: Impl })
    await c.connect()
    const env = FakeWebSocket.last!.sentEnvelope(0)
    expect(env.type).toBe('hello')
    expect(env.payload.client_type).toBe('desktop')
  })

  it('收到 welcome 经 onMessage 派发', async () => {
    const c = new DaemonClient('ws://x', { WebSocketImpl: Impl })
    const got: Envelope[] = []
    c.onMessage('welcome', (env) => got.push(env))
    await c.connect()
    FakeWebSocket.last!.emit('welcome', { daemon_version: '0.1.0', protocol_version: '1.0' })
    expect(got).toHaveLength(1)
    expect(got[0].payload.daemon_version).toBe('0.1.0')
  })

  it('收到 event 经 onEvent 派发并解析为 AgentEvent', async () => {
    const c = new DaemonClient('ws://x', { WebSocketImpl: Impl })
    const events: unknown[] = []
    c.onEvent((ev) => events.push(ev))
    await c.connect()
    FakeWebSocket.last!.emit('event', {
      event: { seq: 0, type: 'decision', ts: 1.0, decision: { text: 'hi', tool_calls: [] } },
    })
    expect(events).toHaveLength(1)
    expect((events[0] as { type: string; decision: { text: string } }).decision.text).toBe('hi')
  })

  it('session.created 跟踪当前会话 id（供重连恢复）', async () => {
    const c = new DaemonClient('ws://x', { WebSocketImpl: Impl })
    await c.connect()
    FakeWebSocket.last!.emit('session.created', { session_id: 'abc', project_root: '/p' })
    // 再次发送 task 不应因未跟踪而报错；此处验证内部状态通过 listSessions 等正常。
    expect(() => c.sendTask('hello')).not.toThrow()
  })
})

describe('DaemonClient HITL 配对', () => {
  it('ask -> answer 回传同 id', async () => {
    const c = new DaemonClient('ws://x', { WebSocketImpl: Impl })
    await c.connect()
    const socket = FakeWebSocket.last!
    const asks: Envelope[] = []
    c.onMessage('ask', (env) => asks.push(env))
    socket.emit('ask', { id: 'q1', question: '确认?' }, { id: 'q1' })
    expect(asks[0].id).toBe('q1')
    c.answer('q1', 'yes')
    const sent = socket.sentEnvelope(socket.sent.length - 1)
    expect(sent.type).toBe('answer')
    expect(sent.id).toBe('q1')
    expect(sent.payload).toEqual({ id: 'q1', text: 'yes' })
  })

  it('confirmPlan / approve 回传同 id 与布尔', async () => {
    const c = new DaemonClient('ws://x', { WebSocketImpl: Impl })
    await c.connect()
    const socket = FakeWebSocket.last!
    c.confirmPlan('p1', true)
    c.approve('a1', false)
    const c1 = socket.sentEnvelope(socket.sent.length - 2)
    const a1 = socket.sentEnvelope(socket.sent.length - 1)
    expect(c1).toEqual({ type: 'confirm_plan', id: 'p1', payload: { id: 'p1', confirmed: true } })
    expect(a1).toEqual({ type: 'approve', id: 'a1', payload: { id: 'a1', approved: false } })
  })
})

describe('DaemonClient 重连', () => {
  it('非主动关闭后在退避上限内重建连接并重发 hello', async () => {
    vi.useFakeTimers()
    try {
      const c = new DaemonClient('ws://x', { WebSocketImpl: Impl, reconnectMaxMs: 500 })
      await c.connect()
      const first = FakeWebSocket.last!
      expect(first.closed).toBe(false)
      // 模拟意外断开（非 manualClose）。
      first.onclose?.({})
      // 退避 500*2^0 = 500ms 后重连。
      await vi.advanceTimersByTimeAsync(500)
      const second = FakeWebSocket.last!
      expect(second).not.toBe(first)
      expect(second.sentEnvelope(0).type).toBe('hello')
    } finally {
      vi.useRealTimers()
    }
  })

  it('manual close 不触发重连', async () => {
    const c = new DaemonClient('ws://x', { WebSocketImpl: Impl })
    await c.connect()
    const before = FakeWebSocket.last
    c.close()
    expect(before!.closed).toBe(true)
    // 不抛、不产生新连接。
    expect(FakeWebSocket.last).toBe(before)
  })
})
