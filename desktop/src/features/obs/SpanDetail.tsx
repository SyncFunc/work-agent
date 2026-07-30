import type { SpanLog, SpanNode } from '../../protocol/types'
import { AlertCircle, CheckCircle2, Clock, Info, Search, X } from 'lucide-react'
import { useMemo, useState } from 'react'

interface SpanDetailProps {
  span: SpanNode | null
  onClose: () => void
}

export function SpanDetail({ span, onClose }: SpanDetailProps) {
  const [logFilter, setLogFilter] = useState('')

  const filteredLogs = useMemo(() => {
    if (!span || !span.logs) return [] as SpanLog[]
    if (!logFilter.trim()) return span.logs
    const q = logFilter.toLowerCase()
    return span.logs.filter(
      (log: SpanLog) =>
        log.key.toLowerCase().includes(q) ||
        (typeof log.value === 'string' ? log.value.toLowerCase().includes(q) : false)
    )
  }, [span, logFilter])

  if (!span) {
    return (
      <div className="wa-spandetail">
        <div className="wa-spandetail__empty">点击 span 查看详情</div>
      </div>
    )
  }

  const duration =
    span.started_at && span.ended_at
      ? ((span.ended_at - span.started_at) * 1000).toFixed(1)
      : null

  const metaEntries = Object.entries(span.meta ?? {}).filter(
    ([k]) => k !== 'status'
  )

  const StatusIcon =
    span.status === 'error' ? AlertCircle : span.status === 'open' ? Clock : CheckCircle2

  return (
    <div className={`wa-spandetail ${span ? 'wa-spandetail--open' : ''}`}>
      <div className="wa-spandetail__header">
        <span className="wa-spandetail__header-icon">
          <Info size={14} />
        </span>
        <span className="wa-spandetail__header-name">{span.name}</span>
        <button className="wa-spandetail__close" onClick={onClose}>
          <X size={14} />
        </button>
      </div>

      {/* 基本信息 */}
      <div className="wa-spandetail__section">
        <div className="wa-spandetail__section-title">Info</div>
        <div className="wa-spandetail__field">
          <span className="wa-spandetail__field-key">span_id</span>
          <span className="wa-spandetail__field-val wa-spandetail__field-val--mono">{span.span_id}</span>
        </div>
        <div className="wa-spandetail__field">
          <span className="wa-spandetail__field-key">kind</span>
          <span className="wa-spandetail__field-val">{span.kind}</span>
        </div>
        <div className="wa-spandetail__field">
          <span className="wa-spandetail__field-key">status</span>
          <span className="wa-spandetail__field-val" style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
            <span style={{ display: 'flex', color: span.status === 'error' ? 'var(--wa-danger)' : span.status === 'open' ? 'var(--wa-warn)' : 'var(--wa-success)' }}>
              <StatusIcon size={12} />
            </span>
            {span.status}
          </span>
        </div>
        {span.parent_id && (
          <div className="wa-spandetail__field">
            <span className="wa-spandetail__field-key">parent_id</span>
            <span className="wa-spandetail__field-val wa-spandetail__field-val--mono">{span.parent_id}</span>
          </div>
        )}
        <div className="wa-spandetail__field">
          <span className="wa-spandetail__field-key">started</span>
          <span className="wa-spandetail__field-val wa-spandetail__field-val--mono">
            {new Date(span.started_at * 1000).toLocaleTimeString('zh-CN', { hour12: false })}
          </span>
        </div>
        {span.ended_at != null && (
          <div className="wa-spandetail__field">
            <span className="wa-spandetail__field-key">ended</span>
            <span className="wa-spandetail__field-val wa-spandetail__field-val--mono">
              {new Date(span.ended_at * 1000).toLocaleTimeString('zh-CN', { hour12: false })}
            </span>
          </div>
        )}
        {duration !== null && (
          <div className="wa-spandetail__field">
            <span className="wa-spandetail__field-key">duration</span>
            <span className="wa-spandetail__field-val wa-spandetail__field-val--mono">
              {parseFloat(duration) >= 1000 ? `${(parseFloat(duration) / 1000).toFixed(2)}s` : `${duration}ms`}
            </span>
          </div>
        )}
      </div>

      {/* Meta */}
      <div className="wa-spandetail__section">
        <div className="wa-spandetail__section-title">Meta ({metaEntries.length})</div>
        {metaEntries.length === 0 ? (
          <div className="wa-spandetail__field">
            <span className="wa-spandetail__field-val" style={{ color: 'var(--wa-text-faint)' }}>无 meta 数据</span>
          </div>
        ) : (
          metaEntries.map(([key, val]) => (
            <div key={key} className="wa-spandetail__meta-row">
              <span className="wa-spandetail__meta-key">{key}</span>
              <span className="wa-spandetail__meta-val">
                {typeof val === 'object' ? JSON.stringify(val, null, 1) : String(val)}
              </span>
            </div>
          ))
        )}
      </div>

      {/* Logs */}
      <div className="wa-spandetail__section" style={{ borderBottom: 'none' }}>
        <div className="wa-spandetail__section-title">Logs ({span.logs?.length ?? 0})</div>
        <div className="wa-spandetail__log-search">
          <Search size={12} />
          <input
            placeholder="过滤日志…"
            value={logFilter}
            onChange={(e) => setLogFilter(e.target.value)}
          />
        </div>
        {filteredLogs.length === 0 ? (
          <div className="wa-spandetail__field">
            <span className="wa-spandetail__field-val" style={{ color: 'var(--wa-text-faint)' }}>
              {logFilter ? '无匹配日志' : '无日志记录'}
            </span>
          </div>
        ) : (
          filteredLogs.map((log: SpanLog, i: number) => (
            <div key={`${log.ts}-${i}`} className="wa-spandetail__log-row">
              <span className="wa-spandetail__log-time">
                {new Date(log.ts * 1000).toLocaleTimeString('zh-CN', { hour12: false })}
              </span>
              <span className={`wa-spandetail__log-level wa-spandetail__log-level--${(log.level ?? 'info').toLowerCase()}`}>
                {log.level?.toUpperCase() ?? 'INFO'}
              </span>
              <span className="wa-spandetail__log-msg">
                {log.key}{typeof log.value === 'string' ? `=${log.value}` : ''}
              </span>
            </div>
          ))
        )}
      </div>
    </div>
  )
}
