import type { TraceInfo } from '../../protocol/types'
import { AlertCircle, CheckCircle2, ChevronLeft, ChevronRight, Clock, Minus } from 'lucide-react'

interface TraceListProps {
  traces: TraceInfo[]
  totalPages: number
  page: number
  activeTraceId: string | null
  onSelect: (traceId: string) => void
  onPageChange: (page: number) => void
  getIconType: (trace: TraceInfo) => 'ok' | 'error' | 'open' | 'muted'
}

function formatDuration(t: TraceInfo): string {
  if (!t.first_ts || !t.last_ts) return '-'
  const ms = (t.last_ts - t.first_ts) * 1000
  if (ms >= 1000) return `${(ms / 1000).toFixed(1)}s`
  return `${ms.toFixed(0)}ms`
}

function TraceRowIcon({ type }: { type: string }) {
  if (type === 'ok') return <CheckCircle2 size={14} />
  if (type === 'error') return <AlertCircle size={14} />
  if (type === 'open') return <Clock size={14} />
  return <Minus size={14} style={{ color: 'var(--wa-text-faint)' }} />
}

export function TraceList({ traces, totalPages, page, activeTraceId, onSelect, onPageChange, getIconType }: TraceListProps) {
  if (traces.length === 0) {
    return <div className="wa-tracelist__empty">该会话暂无 trace 数据</div>
  }

  return (
    <div className="wa-tracelist">
      {traces.map((t) => {
        const iconType = getIconType(t)
        return (
          <div
            key={t.trace_id}
            className={`wa-trace-row ${t.trace_id === activeTraceId ? 'wa-trace-row--active' : ''}`}
            onClick={() => onSelect(t.trace_id)}
          >
            <span className={`wa-trace-row__icon wa-trace-row__icon--${iconType}`}>
              <TraceRowIcon type={iconType} />
            </span>
            <span className="wa-trace-row__id">{t.trace_id.slice(0, 8)}</span>
            <span className="wa-trace-row__spans">{t.span_count} spans</span>
            <span className="wa-trace-row__duration">{formatDuration(t)}</span>
            <span className="wa-trace-row__text">
              {t.session_id.slice(0, 8)} — {new Date((t.last_ts ?? t.first_ts ?? 0) * 1000).toLocaleTimeString()}
            </span>
          </div>
        )
      })}

      {totalPages > 1 && (
        <div className="wa-tracelist__pagination">
          <button
            className="wa-tracelist__page-btn"
            disabled={page <= 1}
            onClick={() => onPageChange(page - 1)}
          >
            <ChevronLeft size={12} />
          </button>

          {Array.from({ length: Math.min(totalPages, 7) }, (_, i) => {
            let pageNum: number
            if (totalPages <= 7) {
              pageNum = i + 1
            } else if (page <= 4) {
              pageNum = i + 1
            } else if (page >= totalPages - 3) {
              pageNum = totalPages - 6 + i
            } else {
              pageNum = page - 3 + i
            }
            return (
              <button
                key={pageNum}
                className={`wa-tracelist__page-btn ${pageNum === page ? 'wa-tracelist__page-btn--active' : ''}`}
                onClick={() => onPageChange(pageNum)}
              >
                {pageNum}
              </button>
            )
          })}

          <button
            className="wa-tracelist__page-btn"
            disabled={page >= totalPages}
            onClick={() => onPageChange(page + 1)}
          >
            <ChevronRight size={12} />
          </button>

          <span className="wa-tracelist__page-info">
            {page} / {totalPages}
          </span>
        </div>
      )}
    </div>
  )
}
