import React from 'react'
import type { SessionTab } from './sessionMachine'

interface Props {
  tabs: SessionTab[]
  activeId: string | null
  onSwitch: (id: string) => void
  onClose: (id: string) => void
}

/** 多会话标签页：每个 tab = 一个 session_id + project_root。 */
export function SessionTabs({ tabs, activeId, onSwitch, onClose }: Props): React.ReactElement {
  if (tabs.length === 0) {
    return <div style={{ padding: '6px 12px', color: '#999', fontSize: 13 }}>（无打开的会话）</div>
  }
  return (
    <div style={{ display: 'flex', gap: 4, padding: '4px 8px', overflowX: 'auto' }}>
      {tabs.map((t) => {
        const active = t.id === activeId
        return (
          <div
            key={t.id}
            onClick={() => onSwitch(t.id)}
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 6,
              padding: '4px 8px',
              borderRadius: 6,
              cursor: 'pointer',
              background: active ? '#e8f0fe' : '#f1f1f1',
              border: active ? '1px solid #4285f4' : '1px solid transparent',
              whiteSpace: 'nowrap',
            }}
          >
            <span>{t.name}</span>
            <span
              onClick={(e) => {
                e.stopPropagation()
                onClose(t.id)
              }}
              style={{ color: '#999', fontWeight: 700 }}
              title="关闭（detach）"
            >
              ×
            </span>
          </div>
        )
      })}
    </div>
  )
}
