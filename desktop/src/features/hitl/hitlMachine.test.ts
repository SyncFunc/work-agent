import { describe, expect, it } from 'vitest'
import { hitlReducer, initialState, type HitlState } from './hitlMachine'

describe('hitlReducer', () => {
  it('ask 入队，resolve_ask 按 id 出队', () => {
    let s: HitlState = initialState()
    s = hitlReducer(s, { type: 'ask', id: 'a1', question: { question: 'q?' } })
    expect(s.queue).toHaveLength(1)
    expect(s.queue[0].kind).toBe('ask')
    s = hitlReducer(s, { type: 'resolve_ask', id: 'a1' })
    expect(s.queue).toHaveLength(0)
  })

  it('show_plan 入队 id 为空，confirm_plan 补填 id 后才可操作', () => {
    let s = hitlReducer(initialState(), {
      type: 'show_plan',
      plan: 'do x',
      planPath: '/p',
      planSteps: [{ id: 's1', title: 'step1', status: 'pending' }],
    })
    const plan = s.queue[0]
    expect(plan.kind).toBe('plan')
    if (plan.kind === 'plan') expect(plan.id).toBeNull()
    s = hitlReducer(s, { type: 'confirm_plan', id: 'p1' })
    const plan2 = s.queue[0]
    if (plan2.kind === 'plan') expect(plan2.id).toBe('p1')
    s = hitlReducer(s, { type: 'resolve_plan', id: 'p1' })
    expect(s.queue).toHaveLength(0)
  })

  it('approve 入队，resolve_approve 按 id 出队', () => {
    let s = initialState()
    s = hitlReducer(s, {
      type: 'approve',
      id: 'v1',
      action: { tool: 'bash', risk: 'danger', args: { cmd: 'rm' } },
    })
    expect(s.queue[0].kind).toBe('approve')
    s = hitlReducer(s, { type: 'resolve_approve', id: 'v1' })
    expect(s.queue).toHaveLength(0)
  })

  it('plan_progress 实时刷新计划步骤状态', () => {
    let s = hitlReducer(initialState(), {
      type: 'show_plan',
      plan: 'p',
      planPath: null,
      planSteps: [
        { id: 's1', title: 'A', status: 'pending' },
        { id: 's2', title: 'B', status: 'pending' },
      ],
    })
    s = hitlReducer(s, { type: 'plan_progress', stepId: 's1', status: 'done', note: 'ok' })
    const plan = s.queue[0]
    if (plan.kind === 'plan') {
      expect(plan.planSteps[0].status).toBe('done')
      expect(plan.planSteps[1].status).toBe('pending')
    } else {
      throw new Error('期望 plan')
    }
  })

  it('多并发 HITL 按 id 配对不串', () => {
    let s = initialState()
    s = hitlReducer(s, { type: 'ask', id: 'a1', question: { question: 'q1' } })
    s = hitlReducer(s, { type: 'approve', id: 'v1', action: { tool: 'bash', risk: 'safe', args: {} } })
    s = hitlReducer(s, { type: 'resolve_ask', id: 'a1' })
    // 仅 ask 出队，approve 仍在
    expect(s.queue).toHaveLength(1)
    expect(s.queue[0].kind).toBe('approve')
    expect((s.queue[0] as { id: string }).id).toBe('v1')
  })
})
