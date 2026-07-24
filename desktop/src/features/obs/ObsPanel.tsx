// ObsPanel：M9.7 可观测面板主容器——状态栏 + 可切换的 Trace / 日志 / 后台 视图。

import { useState } from 'react'
import { DaemonClient } from '../../protocol/client'
import { BackgroundAgents } from './BackgroundAgents'
import { LogView } from './LogView'
import { StatusBar } from './StatusBar'
import { TraceTree } from './TraceTree'
import { useObs } from './useObs'

interface Props {
  client: DaemonClient | null
  projectRoot: string
  sessionId: string | null
  contextWindow?: number
  onClose: () => void
}

type Tab = 'trace' | 'log' | 'bg'

const TABS: { id: Tab; label: string }[] = [
  { id: 'trace', label: 'Trace' },
  { id: 'log', label: '日志' },
  { id: 'bg', label: '后台' },
]

export function ObsPanel({ client, projectRoot, sessionId, contextWindow, onClose }: Props) {
  const [tab, setTab] = useState<Tab>('trace')
  const obs = useObs(client)

  return (
    <aside
      style={{
        width: 340,
        borderLeft: '1px solid #eee',
        display: 'flex',
        flexDirection: 'column',
        minWidth: 0,
      }}
    >
      <StatusBar
        projectRoot={projectRoot}
        sessionId={sessionId}
        usage={obs.usage}
        estimated={obs.estimated}
        mode={obs.mode}
        contextWindow={contextWindow}
      />
      <div style={{ display: 'flex', alignItems: 'center', borderBottom: '1px solid #eee' }}>
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            onClick={() => setTab(t.id)}
            style={{
              flex: 1,
              padding: '6px 0',
              fontSize: 13,
              border: 'none',
              borderBottom: tab === t.id ? '2px solid #1a73e8' : '2px solid transparent',
              background: 'transparent',
              cursor: 'pointer',
              color: tab === t.id ? '#1a73e8' : '#555',
            }}
          >
            {t.label}
          </button>
        ))}
        <button type="button" onClick={onClose} title="关闭可观测面板" style={{ fontSize: 14, padding: '0 8px' }}>
          ✕
        </button>
      </div>
      <div style={{ flex: 1, overflow: 'hidden', minHeight: 0 }}>
        {tab === 'trace' && <TraceTree client={client} projectRoot={projectRoot} sessionId={sessionId} />}
        {tab === 'log' && <LogView logs={obs.logs} onClear={obs.clearLogs} />}
        {tab === 'bg' && <BackgroundAgents client={client} />}
      </div>
    </aside>
  )
}
