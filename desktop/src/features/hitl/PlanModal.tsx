// 计划确认模态：展示 plan 文件内容 + plan_steps 进度（PLAN_PROGRESS 实时刷新）；
// confirm_plan{id} 到达后方可「批准/拒绝」→ confirmPlan(id, confirmed)。

import React from 'react'
import type { PlanRequest } from './hitlMachine'
import { Button, Modal } from '../../components'

export function PlanModal({
  req,
  onConfirm,
}: {
  req: PlanRequest
  onConfirm: (id: string, confirmed: boolean) => void
}): React.ReactElement {
  const actionable = req.id !== null
  return (
    <Modal
      open
      width={640}
      title="执行计划需确认"
      onClose={() => req.id && onConfirm(req.id, false)}
      footer={
        <>
          <Button onClick={() => req.id && onConfirm(req.id, false)} disabled={!actionable}>
            拒绝
          </Button>
          <Button variant="primary" onClick={() => req.id && onConfirm(req.id, true)} disabled={!actionable}>
            批准并继续
          </Button>
        </>
      }
    >
      {req.planPath ? (
        <div style={{ fontSize: 'var(--wa-f-sm)', color: 'var(--wa-text-muted)' }}>{req.planPath}</div>
      ) : null}
      <pre className="wa-pre">{req.plan || '(无计划文本)'}</pre>
      {req.planSteps.length > 0 && (
        <ol style={{ fontSize: 'var(--wa-f-md)', paddingLeft: 'var(--wa-s4)' }}>
          {req.planSteps.map((s) => (
            <li
              key={s.id}
              style={{
                color:
                  s.status === 'done'
                    ? 'var(--wa-success)'
                    : s.status === 'failed'
                      ? 'var(--wa-danger)'
                      : 'var(--wa-text)',
              }}
            >
              {s.title} <span style={{ color: 'var(--wa-text-faint)' }}>[{s.status}]</span>
            </li>
          ))}
        </ol>
      )}
      {!actionable && <p style={{ color: 'var(--wa-text-faint)', fontSize: 'var(--wa-f-sm)' }}>等待 daemon 进入计划确认…</p>}
    </Modal>
  )
}
