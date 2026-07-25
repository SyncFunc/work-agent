// 计划确认模态：展示 plan 文件内容 + plan_steps 进度（PLAN_PROGRESS 实时刷新）；
// confirm_plan{id} 到达后方可「批准/拒绝」→ confirmPlan(id, confirmed)。

import React from 'react'
import type { PlanRequest } from './hitlMachine'

export function PlanModal({
  req,
  onConfirm,
}: {
  req: PlanRequest
  onConfirm: (id: string, confirmed: boolean) => void
}): React.ReactElement {
  const actionable = req.id !== null
  return (
    <div className="wa-modal">
      <div className="wa-modal-box" style={{ maxWidth: 640 }}>
        <h3 style={{ marginTop: 0 }}>执行计划需确认</h3>
        {req.planPath ? <div style={{ fontSize: 12, color: '#888' }}>{req.planPath}</div> : null}
        <pre style={{ background: '#fbfbfd', padding: 10, maxHeight: 240, overflow: 'auto', fontSize: 13, whiteSpace: 'pre-wrap' }}>
          {req.plan || '(无计划文本)'}
        </pre>
        {req.planSteps.length > 0 && (
          <ol style={{ fontSize: 13, paddingLeft: 20 }}>
            {req.planSteps.map((s) => (
              <li key={s.id} style={{ color: s.status === 'done' ? '#2e7d32' : s.status === 'failed' ? '#c0392b' : '#333' }}>
                {s.title} <span style={{ color: '#888' }}>[{s.status}]</span>
              </li>
            ))}
          </ol>
        )}
        <div style={{ marginTop: 12, textAlign: 'right', display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
          <button type="button" disabled={!actionable} onClick={() => req.id && onConfirm(req.id, false)}>
            拒绝
          </button>
          <button type="button" disabled={!actionable} onClick={() => req.id && onConfirm(req.id, true)}>
            批准并继续
          </button>
        </div>
        {!actionable && <p style={{ color: '#888', fontSize: 12 }}>等待 daemon 进入计划确认…</p>}
      </div>
    </div>
  )
}
