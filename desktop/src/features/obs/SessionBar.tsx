import { ChevronDown, Layers } from 'lucide-react'
import { useId } from 'react'

interface SessionBarProps {
  sessions: { session_id: string; trace_count: number; span_count: number }[]
  activeSessionId: string | null
  onChange: (sessionId: string) => void
}

export function SessionBar({ sessions, activeSessionId, onChange }: SessionBarProps) {
  const uid = useId()
  return (
    <div className="wa-sessionbar">
      <span className="wa-sessionbar__icon">
        <Layers size={14} />
      </span>
      <span className="wa-sessionbar__label">Session</span>
      <div className="wa-sessionbar__select-wrap">
        <select
          id={`${uid}-session`}
          value={activeSessionId ?? ''}
          onChange={(e) => onChange(e.target.value)}
        >
          {sessions.length === 0 && <option value="">— 无数据 —</option>}
          {sessions.map((s) => (
            <option key={s.session_id} value={s.session_id}>
              #{s.session_id.slice(0, 8)} · {s.trace_count} 操作 · {s.span_count} 步
            </option>
          ))}
        </select>
        <span className="wa-sessionbar__chevron">
          <ChevronDown size={12} />
        </span>
      </div>
    </div>
  )
}
