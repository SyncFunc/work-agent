import React from 'react'
import type { SessionTab } from './sessionMachine'
import { IconButton } from '../../components'
import { X } from 'lucide-react'

interface Props {
  tabs: SessionTab[]
  activeId: string | null
  onSwitch: (id: string) => void
  onClose: (id: string) => void
}

/** 多会话标签页：每个 tab = 一个 session_id + project_root。 */
export function SessionTabs({ tabs, activeId, onSwitch, onClose }: Props): React.ReactElement {
  if (tabs.length === 0) {
    return <div className="wa-session-tabs__empty">（无打开的会话）</div>
  }
  return (
    <div className="wa-session-tabs">
      {tabs.map((t) => {
        const active = t.id === activeId
        return (
          <div
            key={t.id}
            className={`wa-session-tab${active ? ' wa-session-tab--active' : ''}`}
            onClick={() => onSwitch(t.id)}
          >
            <span className="wa-session-tab__name">{t.name}</span>
            <IconButton
              icon={<X size={14} />}
              label="关闭（detach）"
              size="sm"
              className="wa-session-tab__close"
              onClick={(e) => {
                e.stopPropagation()
                onClose(t.id)
              }}
            />
          </div>
        )
      })}
    </div>
  )
}
