// 工具审批模态：展示 action（工具名/风险档/参数摘要）→ approve(id, approved)。

import React from 'react'
import type { ApproveRequest } from './hitlMachine'
import { ApprovalBadge } from './ApprovalBadge'
import { Button, Modal } from '../../components'

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
    <Modal
      open
      title={
        <span>
          工具执行需审批 <ApprovalBadge risk={a.risk} />
        </span>
      }
      onClose={() => onApprove(req.id, false)}
      footer={
        <>
          <Button onClick={() => onApprove(req.id, false)}>拒绝</Button>
          <Button variant="primary" onClick={() => onApprove(req.id, true)}>
            批准
          </Button>
        </>
      }
    >
      <p>
        工具：<code>{a.tool}</code>
      </p>
      {a.description ? <p style={{ color: 'var(--wa-text-muted)' }}>{a.description}</p> : null}
      {a.approval_request ? (
        <p style={{ color: 'var(--wa-warn)' }}>请求理由：{a.approval_request}</p>
      ) : null}
      <pre className="wa-pre">{summarizeArgs(a.args)}</pre>
    </Modal>
  )
}
