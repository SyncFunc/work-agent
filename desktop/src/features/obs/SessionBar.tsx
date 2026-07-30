import { ChevronDown, Layers } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'

interface SessionItem {
  session_id: string
  trace_count: number
  span_count: number
}

interface SessionBarProps {
  sessions: SessionItem[]
  activeSessionId: string | null
  onChange: (sessionId: string) => void
}

export function SessionBar({ sessions, activeSessionId, onChange }: SessionBarProps) {
  const [open, setOpen] = useState(false)
  const wrapRef = useRef<HTMLDivElement | null>(null)
  const active = sessions.find((s) => s.session_id === activeSessionId)

  useEffect(() => {
    if (!open) return
    const handler = (e: MouseEvent): void => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [open])

  return (
    <div className="wa-sessionbar" ref={wrapRef}>
      <span className="wa-sessionbar__icon">
        <Layers size={14} />
      </span>
      <span className="wa-sessionbar__label">Session</span>

      <button
        type="button"
        className={`wa-sessionbar__trigger ${open ? 'is-open' : ''}`}
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="listbox"
        aria-expanded={open}
      >
        <span className="wa-sessionbar__trigger-id">
          {active ? `#${active.session_id.slice(0, 8)}` : '— 无数据 —'}
        </span>
        <span className="wa-sessionbar__trigger-meta">
          {active ? `${active.trace_count} 操作 · ${active.span_count} 步` : ''}
        </span>
        <span className="wa-sessionbar__chevron">
          <ChevronDown size={12} />
        </span>
      </button>

      {open && (
        <ul className="wa-sessionbar__menu" role="listbox">
          {sessions.length === 0 && (
            <li className="wa-sessionbar__empty">当前项目暂无会话</li>
          )}
          {sessions.map((s) => {
            const isActive = s.session_id === activeSessionId
            return (
              <li
                key={s.session_id}
                className={`wa-sessionbar__item ${isActive ? 'is-active' : ''}`}
                onClick={() => {
                  onChange(s.session_id)
                  setOpen(false)
                }}
                role="option"
                aria-selected={isActive}
              >
                <span className="wa-sessionbar__item-id">#{s.session_id.slice(0, 8)}</span>
                <span className="wa-sessionbar__item-meta">
                  {s.trace_count} 操作 · {s.span_count} 步
                </span>
                {isActive && <span className="wa-sessionbar__item-check">✓</span>}
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}
