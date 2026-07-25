// 工具审批模态：展示 action（工具名/风险档/参数摘要）→ approve(id, approved)。

import React from 'react'
import type { ApproveRequest } from './hitlMachine'
import { ApprovalBadge } from './ApprovalBadge'

function summarizeArgs(args: Record<string, unknown>): string {
  const parts = Object.entries(args).map(([k, v]) => {
    const s = typeof v === 'string' ? v : JSON.stringify(v)
    return `${k}=${s.length > 80 ? s.slice(0, 80) + '…' : s}`
  })
  return parts.join('\n') || '(无参数)'
}

export function ApproveModal({
  req,
  onApprove,
}: {
  req: ApproveRequest
  onApprove: (id: string, approved: boolean) => void
}): React.ReactElement {
  const a = req.action
  return (
    <div className="wa-modal">
      <div className="wa-modal-box">
        <h3 style={{ marginTop: 0 }}>
          工具执行需审批 <ApprovalBadge risk={a.risk} />
        </h3>
        <p>
          工具：<code>{a.tool}</code>
        </p>
        {a.description ? <p style={{ color: '#555' }}>{a.description}</p> : null}
        {a.approval_request ? (
          <p style={{ color: '#b9770e' }}>请求理由：{a.approval_request}</p>
        ) : null}
        <pre style={{ background: '#fbfbfd', padding: 10, maxHeight: 200, overflow: 'auto', fontSize: 13, whiteSpace: 'pre-wrap' }}>
          {summarizeArgs(a.args)}
        </pre>
        <div style={{ marginTop: 12, textAlign: 'right', display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
          <button type="button" onClick={() => onApprove(req.id, false)}>
            拒绝
          </button>
          <button type="button" onClick={() => onApprove(req.id, true)}>
            批准
          </button>
        </div>
      </div>
    </div>
  )
}
