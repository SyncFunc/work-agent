// HITL 请求队列的纯 reducer（无 React/DOM，可单测）。
// 协议（对齐 agent/daemon/bridge.py）：
//   - ask{id, question} → 回传 answer{id, text}
//   - show_plan{plan, plan_path, plan_steps}（无 id，展示用）→ 随后 confirm_plan{id} 才带 id
//   - approve{id, action} → 回传 approve{id, approved}
//   - PLAN_PROGRESS 事件（plan_update{step_id,status,note}）实时刷新计划步骤状态。
// 同一请求按 id 闭环；多并发请求在 queue 中排队，互不串。

import type { Question } from '../../protocol/types'

export interface PlanStep {
  id: string
  title: string
  status: string
}

export interface ActionInfo {
  tool: string
  risk: string
  args: Record<string, unknown>
  description?: string | null
  approval_request?: string | null
}

export interface AskRequest {
  kind: 'ask'
  id: string
  question: Question
}
export interface PlanRequest {
  kind: 'plan'
  id: string | null // show_plan 不带 id，confirm_plan 到达后补填
  plan: string
  planPath: string | null
  planSteps: PlanStep[]
}
export interface ApproveRequest {
  kind: 'approve'
  id: string
  action: ActionInfo
}
export type HitlRequest = AskRequest | PlanRequest | ApproveRequest

export interface HitlState {
  queue: HitlRequest[]
}

export type HitlAction =
  | { type: 'ask'; id: string; question: Question }
  | { type: 'show_plan'; plan: string; planPath: string | null; planSteps: PlanStep[] }
  | { type: 'confirm_plan'; id: string }
  | { type: 'approve'; id: string; action: ActionInfo }
  | { type: 'plan_progress'; stepId: string; status: string; note: string | null }
  | { type: 'resolve_ask'; id: string }
  | { type: 'resolve_plan'; id: string }
  | { type: 'resolve_approve'; id: string }

export function initialState(): HitlState {
  return { queue: [] }
}

export function hitlReducer(state: HitlState, action: HitlAction): HitlState {
  switch (action.type) {
    case 'ask':
      return { queue: [...state.queue, { kind: 'ask', id: action.id, question: action.question }] }
    case 'show_plan':
      // 展示计划内容；id 留空，待 confirm_plan 补填后才可操作。
      return {
        queue: [
          ...state.queue,
          {
            kind: 'plan',
            id: null,
            plan: action.plan,
            planPath: action.planPath,
            planSteps: action.planSteps,
          },
        ],
      }
    case 'confirm_plan':
      // 把最近一个尚未配 id 的计划请求补上 confirm id。
      return {
        queue: state.queue.map((r) =>
          r.kind === 'plan' && r.id === null ? { ...r, id: action.id } : r,
        ),
      }
    case 'approve':
      return {
        queue: [...state.queue, { kind: 'approve', id: action.id, action: action.action }],
      }
    case 'plan_progress':
      return {
        queue: state.queue.map((r) => {
          if (r.kind !== 'plan') return r
          return {
            ...r,
            planSteps: r.planSteps.map((s) =>
              s.id === action.stepId ? { ...s, status: action.status, ...(action.note ? { note: action.note } : {}) } : s,
            ),
          }
        }),
      }
    case 'resolve_ask':
      return { queue: state.queue.filter((r) => !(r.kind === 'ask' && r.id === action.id)) }
    case 'resolve_plan':
      return { queue: state.queue.filter((r) => !(r.kind === 'plan' && r.id === action.id)) }
    case 'resolve_approve':
      return { queue: state.queue.filter((r) => !(r.kind === 'approve' && r.id === action.id)) }
    default:
      return state
  }
}
