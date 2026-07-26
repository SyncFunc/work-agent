import type { ReactElement } from 'react'
import type { PlanStepView } from '../../protocol/types'
import { CheckCircle2, Circle, Loader2, MinusCircle, XCircle } from 'lucide-react'

/** 状态 -> 图标（统一用图标而非字符渲染，跨平台一致）。 */
function StatusIcon({ status }: { status: string }): ReactElement {
  switch (status) {
    case 'done':
      return <CheckCircle2 size={14} className="wa-plan-ic wa-plan-ic--done" />
    case 'in_progress':
      return <Loader2 size={14} className="wa-plan-ic wa-plan-ic--active" />
    case 'blocked':
      return <XCircle size={14} className="wa-plan-ic wa-plan-ic--blocked" />
    case 'skipped':
      return <MinusCircle size={14} className="wa-plan-ic wa-plan-ic--skipped" />
    case 'pending':
    default:
      return <Circle size={14} className="wa-plan-ic wa-plan-ic--pending" />
  }
}

export const STATUS_LABEL: Record<string, string> = {
  pending: '待办',
  in_progress: '进行中',
  done: '已完成',
  blocked: '阻塞',
  skipped: '跳过',
}

/** 计划步骤列表（完整计划展示）。highlightId 用于高亮本次更新的步骤。 */
export function PlanStepList({
  steps,
  highlightId,
}: {
  steps: PlanStepView[]
  highlightId?: string
}) {
  return (
    <ul className="wa-plan-steps">
      {steps.map((s) => (
        <li
          key={s.id}
          className={
            'wa-plan-step wa-plan-step--' +
            s.status +
            (highlightId === s.id ? ' wa-plan-step--hl' : '')
          }
        >
          <StatusIcon status={s.status} />
          <span className="wa-plan-step__id">{s.id}</span>
          <span className="wa-plan-step__title">{s.title}</span>
        </li>
      ))}
    </ul>
  )
}
