// 通知宿主：右下角堆叠 toast，展示 daemon 的 notify/show_skills/show_agents 反馈。

import React from 'react'
import type { Notice } from './useNotices'

export function NoticeHost({ notices }: { notices: Notice[] }): React.ReactElement | null {
  if (notices.length === 0) return null
  return (
    <div style={{ position: 'fixed', right: 16, bottom: 16, display: 'flex', flexDirection: 'column', gap: 8, zIndex: 2000 }}>
      {notices.map((n) => (
        <div
          key={n.id}
          style={{
            background: '#222',
            color: '#fff',
            padding: '8px 12px',
            borderRadius: 8,
            fontSize: 13,
            maxWidth: 320,
            boxShadow: '0 4px 16px rgba(0,0,0,0.25)',
          }}
        >
          {n.text}
        </div>
      ))}
    </div>
  )
}
