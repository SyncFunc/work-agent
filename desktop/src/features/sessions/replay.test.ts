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

  it('按父子流游标去重，流内按 seq 排序且不同流相同 seq 均保留', () => {
    const buf = new ReplayBuffer()
    const parent1 = { ...ev(1), ts: 1, session_id: 'p', text: 'p1' }
    const parent2 = { ...ev(2), ts: 4, session_id: 'p', text: 'p2' }
    const child0 = {
      ...ev(0),
      ts: 3,
      session_id: 'p',
      subsession_id: 'p/sub_a',
      text: 'c0',
    }
    const child1 = {
      ...ev(1),
      ts: 2,
      session_id: 'p',
      subsession_id: 'p/sub_a',
      text: 'c1',
    }

    buf.start()
    buf.push(parent2)
    buf.push(child1)
    buf.push(parent1)
    buf.push({ ...parent1, text: 'duplicate' })
    buf.push(child0)

    const out = buf.end()
    expect(out.map((event) => event.text)).toEqual(['p1', 'c0', 'c1', 'p2'])
    expect(out.filter((event) => event.seq === 1)).toHaveLength(2)
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
