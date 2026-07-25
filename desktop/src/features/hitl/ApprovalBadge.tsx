// 风险档徽章：danger(红) / elevated(橙) / safe(绿)。供审批模态复用。

import React from 'react'
import { Badge } from '../../components'
import type { BadgeTone } from '../../components'

const RISK_TONE: Record<string, { tone: BadgeTone; label: string }> = {
  danger: { tone: 'danger', label: '高危' },
  elevated: { tone: 'warn', label: '需关注' },
  safe: { tone: 'success', label: '安全' },
}

export function ApprovalBadge({ risk }: { risk: string }): React.ReactElement {
  const r = RISK_TONE[risk] ?? { tone: 'neutral' as BadgeTone, label: risk || '未知' }
  return <Badge tone={r.tone}>{r.label}</Badge>
}
