// Replay 缓冲：消费 daemon 的 replay_start → 批量 event → replay_end 区间，
// 重建历史供渲染。瞬时事件（tool_call_delta 等）daemon 侧根本不进 event_buffer，
// 故这里只会收到持久化事件，天然满足「瞬时事件不重复渲染」。

import type { AgentEvent, Envelope } from '../../protocol/types'

export class ReplayBuffer {
  private active = false
  private buffer: AgentEvent[] = []

  get isActive(): boolean {
    return this.active
  }

  start(): void {
    this.active = true
    this.buffer = []
  }

  push(ev: AgentEvent): void {
    if (this.active) this.buffer.push(ev)
  }

  /** 结束回放，返回累积的历史事件（并复位）。 */
  end(): AgentEvent[] {
    this.active = false
    const out = this.buffer
    this.buffer = []
    return out
  }
}

/** 判断某条消息是否为 replay 标记（replay_start / replay_end）。 */
export function isReplayStart(msg: Envelope): boolean {
  return msg.type === 'replay_start'
}

export function isReplayEnd(msg: Envelope): boolean {
  return msg.type === 'replay_end'
}
