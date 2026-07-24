import { describe, it, expect } from 'vitest'
import { parseEvent, eventToDict } from './events'
import type { AgentEvent } from './types'

// 一组对齐 Python Event.to_dict() 的真实形态 fixture。
const fixtures: Record<string, unknown> = {
  decision: {
    seq: 0,
    type: 'decision',
    ts: 1.0,
    decision: { text: '我会处理', tool_calls: [{ id: 'c1', name: 'bash', arguments: { cmd: 'ls' } }] },
  },
  tool_result: {
    seq: 1,
    type: 'tool_result',
    ts: 2.0,
    tool_call_id: 'c1',
    tool_result: { ok: true, output: 'file', error: null },
  },
  text: {
    seq: 2,
    type: 'text',
    ts: 3.0,
    kind: 'content',
    text: '思考中',
  },
  clarify: {
    seq: 3,
    type: 'clarify',
    ts: 4.0,
    questions: [{ question: '选哪个?', options: ['A', 'B'], multiSelect: false }],
  },
  plan_progress: {
    seq: 4,
    type: 'plan_progress',
    ts: 5.0,
    plan_path: '.agent/plan.md',
    plan_update: { step_id: 'S1', status: 'done', note: '完成' },
  },
  tool_call_delta: {
    seq: 5,
    type: 'tool_call_delta',
    ts: 6.0,
    tc_index: 0,
    tc_name: 'write',
    tc_args: '{"content":"x"}',
  },
  error: {
    seq: 6,
    type: 'error',
    ts: 7.0,
    error: 'boom',
  },
  final: {
    seq: 7,
    type: 'final',
    ts: 8.0,
    decision: { text: '完成', tool_calls: [] },
  },
}

describe('parseEvent', () => {
  it('逐字段重建各类 Event 并与 Python to_dict 输入相等', () => {
    for (const [name, raw] of Object.entries(fixtures)) {
      const ev = parseEvent(raw)
      expect(ev.seq).toBe((raw as { seq: number }).seq)
      expect(ev.type).toBe((raw as { type: string }).type)
      // 双向：eventToDict(parsed) 应与原始 fixture 深度相等（仅含非 null）。
      expect(eventToDict(ev)).toEqual(raw)
    }
  })

  it('解析 decision 嵌套结构', () => {
    const ev = parseEvent(fixtures.decision)
    expect(ev.decision?.text).toBe('我会处理')
    expect(ev.decision?.tool_calls[0]).toEqual({ id: 'c1', name: 'bash', arguments: { cmd: 'ls' } })
  })

  it('解析 clarify 的 questions（含 options/multiSelect）', () => {
    const ev = parseEvent(fixtures.clarify)
    expect(ev.questions?.[0]).toEqual({ question: '选哪个?', options: ['A', 'B'], multiSelect: false })
  })

  it('解析 plan_progress 的 plan_update', () => {
    const ev = parseEvent(fixtures.plan_progress)
    expect(ev.plan_update).toEqual({ step_id: 'S1', status: 'done', note: '完成' })
    expect(ev.plan_path).toBe('.agent/plan.md')
  })

  it('非法输入抛 TypeError', () => {
    expect(() => parseEvent(null)).toThrow(TypeError)
    expect(() => parseEvent({})).toThrow(TypeError)
    expect(() => parseEvent({ seq: 1, type: 'bogus' })).toThrow(TypeError)
  })

  it('TS -> JSON -> parseEvent 往返稳定', () => {
    const ev: AgentEvent = {
      seq: 9,
      type: 'tool_use',
      ts: 9.0,
      tool_use: { id: 'c2', name: 'read', arguments: { path: 'a.txt' } },
    }
    const round = parseEvent(JSON.parse(JSON.stringify(eventToDict(ev))))
    expect(round).toEqual(ev)
  })
})
