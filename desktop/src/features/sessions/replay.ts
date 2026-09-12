// Replay 缓冲：消费 daemon 的 replay_start → 批量 event → replay_end 区间，
// 重建历史供渲染。瞬时事件（tool_call_delta 等）daemon 侧根本不进 event_buffer，
// 故这里只会收到持久化事件，天然满足「瞬时事件不重复渲染」。

import type { AgentEvent, Envelope } from '../../protocol/types'

export class ReplayBuffer {
  private active = false
  private buffer: Array<{ event: AgentEvent; arrival: number }> = []
  private seen = new Set<string>()
  private arrival = 0

  get isActive(): boolean {
    return this.active
  }

  start(): void {
    this.active = true
    this.buffer = []
    this.seen.clear()
    this.arrival = 0
  }

  push(ev: AgentEvent): void {
    if (!this.active) return
    const key = this.cursorKey(ev)
    if (key !== null) {
      if (this.seen.has(key)) return
      this.seen.add(key)
    }
    this.buffer.push({ event: ev, arrival: this.arrival++ })
  }

  /** 结束回放：父/子流内按 seq 排序、跨流按 ts 合并，并返回去重后的事件。 */
  end(): AgentEvent[] {
    this.active = false
    const out = this.orderedEvents()
    this.buffer = []
    this.seen.clear()
    this.arrival = 0
    return out
  }

  private streamId(ev: AgentEvent): string {
    // 子会话拥有独立 EventStream，seq 可与父会话重复；必须把来源流纳入游标。
    return ev.subsession_id ?? ev.session_id ?? ''
  }

  private cursorKey(ev: AgentEvent): string | null {
    if (!Number.isInteger(ev.seq) || ev.seq < 0) return null
    return `${this.streamId(ev)}\u0000${ev.seq}`
  }

  private orderedEvents(): AgentEvent[] {
    const streams = new Map<string, Array<{ event: AgentEvent; arrival: number }>>()
    for (const item of this.buffer) {
      const stream = this.streamId(item.event)
      const group = streams.get(stream) ?? []
      group.push(item)
      streams.set(stream, group)
    }
    for (const group of streams.values()) {
      group.sort((a, b) => {
        const aSeq = Number.isInteger(a.event.seq) && a.event.seq >= 0
        const bSeq = Number.isInteger(b.event.seq) && b.event.seq >= 0
        if (aSeq && bSeq && a.event.seq !== b.event.seq) return a.event.seq - b.event.seq
        return a.arrival - b.arrival
      })
    }

    // 对各流做 k-way merge：流内严格保持 seq，流间用各自队首的 ts 近似时间顺序。
    const out: AgentEvent[] = []
    while (streams.size > 0) {
      let selected: string | null = null
      let selectedHead: { event: AgentEvent; arrival: number } | null = null
      for (const [stream, group] of streams) {
        const head = group[0]
        if (
          selectedHead === null ||
          head.event.ts < selectedHead.event.ts ||
          (head.event.ts === selectedHead.event.ts && head.arrival < selectedHead.arrival)
        ) {
          selected = stream
          selectedHead = head
        }
      }
      if (selected === null || selectedHead === null) break
      out.push(selectedHead.event)
      const group = streams.get(selected)!
      group.shift()
      if (group.length === 0) streams.delete(selected)
    }
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
