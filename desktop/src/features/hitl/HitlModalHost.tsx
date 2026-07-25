// 全局 HITL 模态宿主：按 queue 顺序渲染 ask / plan / approve 模态。
// 多并发请求各自成模态（按 id 配对，互不串）；调用方应在 pending 时禁用输入。

import React from 'react'
import './hitl.css'
import type { HitlRequest } from './hitlMachine'
import { AskModal } from './AskModal'
import { PlanModal } from './PlanModal'
import { ApproveModal } from './ApproveModal'

export function HitlModalHost({
  requests,
  onAnswer,
  onConfirm,
  onApprove,
}: {
  requests: HitlRequest[]
  onAnswer: (id: string, text: string) => void
  onConfirm: (id: string, confirmed: boolean) => void
  onApprove: (id: string, approved: boolean) => void
}): React.ReactElement | null {
  if (requests.length === 0) return null
  return (
    <>
      {requests.map((req) => {
        if (req.kind === 'ask') {
          return <AskModal key={req.id} req={req} onAnswer={onAnswer} />
        }
        if (req.kind === 'plan') {
          return <PlanModal key={`plan-${req.id ?? 'pending'}`} req={req} onConfirm={onConfirm} />
        }
        return <ApproveModal key={req.id} req={req} onApprove={onApprove} />
      })}
    </>
  )
}
