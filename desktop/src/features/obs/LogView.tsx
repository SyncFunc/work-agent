// LogView：滚动日志视图（消费 notify），带搜索、自动滚底与清空。

import { useEffect, useRef, useState } from 'react'
import type { ObsLog } from './useObs'
import { Button } from '../../components'
import { Search } from 'lucide-react'

interface Props {
  logs: ObsLog[]
  onClear: () => void
}

function fmtTime(ts: number): string {
  return new Date(ts).toLocaleTimeString()
}

export function LogView({ logs, onClear }: Props) {
  const [q, setQ] = useState('')
  const bodyRef = useRef<HTMLDivElement>(null)
  const filtered = q.trim() ? logs.filter((l) => l.message.toLowerCase().includes(q.trim().toLowerCase())) : logs

  useEffect(() => {
    const el = bodyRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [filtered.length])

  return (
    <div className="wa-logview">
      <div className="wa-logview__bar">
        <div className="wa-logview__search">
          <Search size={14} />
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="搜索日志…"
            aria-label="搜索日志"
          />
        </div>
        <span className="wa-logview__count">{logs.length}</span>
        <Button size="sm" variant="ghost" onClick={onClear}>
          清空
        </Button>
      </div>
      <div className="wa-logview__body" ref={bodyRef}>
        {filtered.length === 0 ? (
          <p className="wa-logview__empty">{logs.length === 0 ? '暂无日志' : '无匹配'}</p>
        ) : (
          filtered.map((l) => (
            <div key={l.id} className="wa-logview__row">
              <span className="wa-logview__time">{fmtTime(l.ts)}</span>
              <span className="wa-logview__msg">{l.message}</span>
            </div>
          ))
        )}
      </div>
    </div>
  )
}
