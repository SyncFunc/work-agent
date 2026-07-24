// LogView：滚动日志视图（消费 notify），带清空按钮。

import type { ObsLog } from './useObs'

interface Props {
  logs: ObsLog[]
  onClear: () => void
}

function fmtTime(ts: number): string {
  const d = new Date(ts)
  return d.toLocaleTimeString()
}

export function LogView({ logs, onClear }: Props) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '4px 8px' }}>
        <span style={{ fontSize: 12, color: '#666' }}>日志 ({logs.length})</span>
        <button type="button" onClick={onClear} style={{ fontSize: 12 }}>
          清空
        </button>
      </div>
      <div
        style={{
          flex: 1,
          overflowY: 'auto',
          padding: '0 8px 8px',
          fontFamily: 'monospace',
          fontSize: 12,
          lineHeight: 1.5,
        }}
      >
        {logs.length === 0 ? (
          <p style={{ color: '#999' }}>暂无日志</p>
        ) : (
          logs.map((l) => (
            <div key={l.id} style={{ borderBottom: '1px solid #f0f0f0', padding: '2px 0' }}>
              <span style={{ color: '#999', marginRight: 6 }}>{fmtTime(l.ts)}</span>
              <span>{l.message}</span>
            </div>
          ))
        )}
      </div>
    </div>
  )
}
