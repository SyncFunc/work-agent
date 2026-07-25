// 风险档徽章：danger(红) / elevated(橙) / safe(绿)。供审批模态与工具块复用。

import React from 'react'

const RISK_STYLE: Record<string, { bg: string; fg: string; label: string }> = {
  danger: { bg: '#fdecea', fg: '#c0392b', label: '高危' },
  elevated: { bg: '#fff4e5', fg: '#b9770e', label: '需关注' },
  safe: { bg: '#e8f5e9', fg: '#2e7d32', label: '安全' },
}

export function ApprovalBadge({ risk }: { risk: string }): React.ReactElement {
  const r = RISK_STYLE[risk] ?? { bg: '#eee', fg: '#555', label: risk || '未知' }
  return (
    <span
      style={{
        background: r.bg,
        color: r.fg,
        borderRadius: 10,
        padding: '1px 8px',
        fontSize: 12,
        fontWeight: 600,
        marginLeft: 6,
      }}
    >
      {r.label}
    </span>
  )
}
