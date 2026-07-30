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
      <label htmlFor={`${uid}-session`} className="wa-sessionbar__label">
        Session
      </label>
      <select
        id={`${uid}-session`}
        value={activeSessionId ?? ''}
        onChange={(e) => onChange(e.target.value)}
      >
        {sessions.length === 0 && <option value="">— 无数据 —</option>}
        {sessions.map((s) => (
          <option key={s.session_id} value={s.session_id}>
            {s.session_id.slice(0, 8)} · {s.trace_count} traces · {s.span_count} spans
          </option>
        ))}
      </select>
    </div>
  )
}
