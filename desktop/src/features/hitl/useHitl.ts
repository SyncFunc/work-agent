// useHitl：连接 DaemonClient 的 HITL 消息到纯 reducer（hitlMachine），并暴露回传方法。
// 阻塞语义：任意 HITL 请求在 queue 中时，调用方应禁用输入（App 处理）。

import { useCallback, useEffect, useMemo, useReducer } from 'react'
import { DaemonClient } from '../../protocol/client'
import type { Question } from '../../protocol/types'
import type { ActionInfo, HitlRequest, PlanStep } from './hitlMachine'
import { hitlReducer, initialState } from './hitlMachine'

function asPlanSteps(v: unknown): PlanStep[] {
  if (!Array.isArray(v)) return []
  return v
    .filter((s): s is Record<string, unknown> => !!s && typeof s === 'object')
    .map((s) => ({
      id: String(s.id ?? ''),
      title: String(s.title ?? ''),
      status: String(s.status ?? 'pending'),
    }))
}

export interface UseHitl {
  requests: HitlRequest[]
  /** 是否有未决 HITL（用于禁用输入）。 */
  pending: boolean
  resolveAsk: (id: string, text: string) => void
  resolvePlan: (id: string, confirmed: boolean) => void
  resolveApprove: (id: string, approved: boolean) => void
}

export function useHitl(client: DaemonClient | null): UseHitl {
  const [state, dispatch] = useReducer(hitlReducer, undefined, initialState)

  useEffect(() => {
    if (!client) return
    const offAsk = client.onMessage('ask', (env) => {
      const id = env.payload['id'] as string
      const question = env.payload['question'] as Question
      dispatch({ type: 'ask', id, question })
    })
    const offShowPlan = client.onMessage('show_plan', (env) => {
      dispatch({
        type: 'show_plan',
        plan: (env.payload['plan'] as string) ?? '',
        planPath: (env.payload['plan_path'] as string | null) ?? null,
        planSteps: asPlanSteps(env.payload['plan_steps']),
      })
    })
    const offConfirmPlan = client.onMessage('confirm_plan', (env) => {
      const id = env.payload['id'] as string
      dispatch({ type: 'confirm_plan', id })
    })
    const offApprove = client.onMessage('approve', (env) => {
      const id = env.payload['id'] as string
      const action = env.payload['action'] as ActionInfo
      dispatch({ type: 'approve', id, action })
    })
    const offEvent = client.onEvent((ev) => {
      if (ev.type === 'plan_progress' && ev.plan_update) {
        dispatch({
          type: 'plan_progress',
          stepId: ev.plan_update.step_id,
          status: ev.plan_update.status,
          note: ev.plan_update.note ?? null,
        })
      }
    })
    return () => {
      offAsk()
      offShowPlan()
      offConfirmPlan()
      offApprove()
      offEvent()
    }
  }, [client])

  const resolveAsk = useCallback(
    (id: string, text: string) => {
      client?.answer(id, text)
      dispatch({ type: 'resolve_ask', id })
    },
    [client],
  )
  const resolvePlan = useCallback(
    (id: string, confirmed: boolean) => {
      client?.confirmPlan(id, confirmed)
      dispatch({ type: 'resolve_plan', id })
    },
    [client],
  )
  const resolveApprove = useCallback(
    (id: string, approved: boolean) => {
      client?.approve(id, approved)
      dispatch({ type: 'resolve_approve', id })
    },
    [client],
  )

  const requests = useMemo(() => state.queue, [state.queue])
  return { requests, pending: state.queue.length > 0, resolveAsk, resolvePlan, resolveApprove }
}
