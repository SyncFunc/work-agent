import { describe, it, expect } from 'vitest'
import { ReplayBuffer, isReplayStart, isReplayEnd } from './replay'
import type { AgentEvent, Envelope } from '../../protocol/types'

function ev(seq: number): AgentEvent {
  return { seq, type: 'text', ts: seq }
}

describe('ReplayBuffer', () => {
  it('start/push/end 累积历史并在 end 复位', () => {
    const buf = new ReplayBuffer()
    expect(buf.isActive).toBe(false)
    buf.start()
    expect(buf.isActive).toBe(true)
    buf.push(ev(0))
    buf.push(ev(1))
    const out = buf.end()
    expect(out).toEqual([ev(0), ev(1)])
    expect(buf.isActive).toBe(false)
    // 结束后再次 push 不入缓冲（active=false）
    buf.push(ev(2))
    expect(buf.end()).toEqual([])
  })

  it('replay 标记判定', () => {
    const start: Envelope = { type: 'replay_start', payload: {} }
    const end: Envelope = { type: 'replay_end', payload: {} }
    const other: Envelope = { type: 'event', payload: {} }
    expect(isReplayStart(start)).toBe(true)
    expect(isReplayEnd(end)).toBe(true)
    expect(isReplayStart(other)).toBe(false)
    expect(isReplayEnd(other)).toBe(false)
  })
})
